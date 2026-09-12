"""``picframe3 setup`` — the interactive first-run wizard.

The measure of a picture frame's installer is how many things the person has to
get right by hand.  picframe's current guide asks for a compositor, an X
compatibility layer, console autologin, three files created in an editor and a
Samba configuration pasted in.  None of that is needed here: the frame owns the
screen, so there is nothing to log into and nothing to autostart inside a
session.  What is left is a handful of questions.

Everything the wizard does is idempotent and re-runnable, and every answer has
a default that works, so holding down Return is a valid way to use it.
"""

from __future__ import annotations

import getpass
import grp
import logging
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from . import __version__
from .config import DEFAULT_CONFIG_PATHS, Config

_log = logging.getLogger(__name__)

SERVICE_NAME = "picframe3@{user}.service"
SERVICE_UNIT = "/etc/systemd/system/picframe3@.service"
POLKIT_RULE = "/etc/polkit-1/rules.d/50-picframe3-network.rules"
UDEV_RULE = "/etc/udev/rules.d/99-picframe3.rules"
SAMBA_CONF = "/etc/samba/smb.conf"
SAMBA_BEGIN = "# >>> picframe3 >>>"
SAMBA_END = "# <<< picframe3 <<<"

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

_TERMINAL: object = None


def _terminal():
    """A stream that reaches the person, even when stdin is a pipe.

    The recommended install is ``curl … | bash``, which makes stdin the script
    itself: ``sys.stdin.isatty()`` is False although somebody is sitting right
    there watching.  Opening ``/dev/tty`` reaches the controlling terminal
    regardless, which is the difference between asking the eight questions and
    silently taking every default.
    """
    global _TERMINAL
    if _TERMINAL is None:
        try:
            # Read-only: "r+" on a character device raises "not seekable",
            # because Python's buffered random access needs to seek.  The
            # prompt goes to stdout, which in the curl|bash case is still the
            # terminal — only stdin was taken by the pipe.
            _TERMINAL = open("/dev/tty")  # noqa: SIM115 - lives for the whole run
        except OSError:
            _TERMINAL = False
    return _TERMINAL or None


def _tty() -> bool:
    if sys.stdin.isatty() and sys.stdout.isatty():
        return True
    return _terminal() is not None


def _read(prompt: str) -> str:
    """Read one line from the person, from the terminal rather than stdin."""
    if sys.stdin.isatty():
        return input(prompt)
    stream = _terminal()
    if stream is None:
        raise EOFError("no terminal to read from")
    print(prompt, end="", flush=True)
    line = stream.readline()
    if not line:
        raise EOFError("terminal closed")
    return line.rstrip("\n")


def say(text: str = "") -> None:
    print(text)


def heading(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")


def note(text: str) -> None:
    print(f"{DIM}{textwrap.fill(text, 76, subsequent_indent='  ')}{RESET}")


def ask(question: str, default: str = "", *, secret: bool = False) -> str:
    if secret:
        return ask_secret(question)
    suffix = f" [{default}]" if default else ""
    while True:
        value = _read(f"{question}{suffix}: ").strip()
        if value:
            return value
        if default or default == "":
            return default


def ask_secret(question: str, *, attempts: int = 2) -> str:
    """Read a password, and say how much of one arrived.

    Hidden input is the right default -- somebody may be looking over your
    shoulder -- but it means a paste that landed and a paste that did not look
    exactly the same, which is how a wrong broker password gets saved and then
    blamed on the broker. So the character count is echoed back, and if the
    hidden read comes up empty (some terminals refuse to paste into a no-echo
    prompt at all) it falls back to a visible one rather than looping.
    """
    for _ in range(attempts):
        try:
            value = getpass.getpass(f"{question}: ").strip()
        except (EOFError, OSError):
            break
        if value:
            say(f"     {DIM}{len(value)} characters received{RESET}")
            return value
        say("     Nothing arrived. Paste again, or press Return to type it "
            "where you can see it.")
    say(f"{YELLOW}     Switching to a visible prompt — it will be shown on "
        f"screen.{RESET}")
    try:
        return _read(f"{question} (visible): ").strip()
    except EOFError:
        return ""


def confirm(question: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = _read(f"{question} {suffix}: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


# --------------------------------------------------------------------------
# System facts
# --------------------------------------------------------------------------

def pi_model() -> str:
    for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            with open(path, "rb") as fh:
                return fh.read().decode(errors="replace").strip("\x00 \n")
        except OSError:
            continue
    return ""


def os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip().strip('"')
    except OSError:
        pass
    return values


def have_sudo() -> bool:
    return shutil.which("sudo") is not None and os.geteuid() != 0 or os.geteuid() == 0


def run_root(command: list[str], *, check: bool = False, input_text: str | None = None):
    """Run a command as root, via sudo when we are not already."""
    if os.geteuid() != 0:
        command = ["sudo", *command]
    return subprocess.run(command, check=check, text=True, input=input_text,
                          capture_output=True)


def packaged(name: str) -> str:
    """Read one of the files in ``packaging/``.

    Those files are the single definition of the unit, the polkit rule and the
    udev rule.  Keeping a second copy of the same text here as a Python string
    is how the two drifted apart: only the copy in this module was ever
    installed, so the one in ``packaging/`` could grow an ``ExecReload`` for a
    signal nothing handles and nobody found out.  They ship inside the wheel
    (see ``[tool.hatch.build.targets.wheel.force-include]``) so this works on
    a Pi with no source tree.
    """
    try:
        from importlib.resources import files

        return (files("picframe3") / "data" / name).read_text(encoding="utf-8")
    except (AttributeError, FileNotFoundError, ModuleNotFoundError, OSError):
        # An editable install has no package data, so fall back to the source
        # tree this module was imported from -- that is a developer running
        # `picframe3 setup` out of a checkout, and it should still work.
        return (Path(__file__).resolve().parents[2] / "packaging" / name) \
            .read_text(encoding="utf-8")


def write_as_root(text: str, destination: str, *, mode: str | None = "0644") -> bool:
    """Put ``text`` at ``destination``, with root's permission.

    The wizard runs as the owner, and everything it writes lives under /etc or
    /boot, so the content has to travel through a file root can read.  It used
    to travel through a predictable name in a world-writable directory
    (``/tmp/picframe3@.service`` and four siblings), which is the textbook way
    to lose that race: anyone else on the machine can create that path first,
    as a symbolic link, and have root copy our content wherever the link
    points.  ``mkstemp`` picks a name nobody can guess and creates it 0600.

    ``mode=None`` copies instead of installing, for /boot/firmware: it is a FAT
    filesystem, where ``install -m`` attempts a chmod the filesystem cannot
    represent.
    """
    fd, tmp = tempfile.mkstemp(prefix="picframe3-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        if mode is None:
            command = ["cp", tmp, destination]
        else:
            command = ["install", "-D", "-m", mode, tmp, destination]
        return run_root(command).returncode == 0
    finally:
        Path(tmp).unlink(missing_ok=True)


def missing_groups(user: str) -> list[str]:
    try:
        member_of = {g.gr_name for g in grp.getgrall() if user in g.gr_mem}
        member_of.add(grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name)
    except KeyError:
        return []
    return [g for g in ("video", "render", "input") if g not in member_of]


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def check_hardware() -> bool:
    """Report what the frame will be running on, and warn about known limits."""
    heading("1. This machine")
    model = pi_model()
    release = os_release()
    say(f"   {model or 'unknown hardware'}")
    say(f"   {release.get('PRETTY_NAME', 'unknown OS')}   ·   python {sys.version.split()[0]}")

    ok = True
    if model:
        lowered = model.lower()
        if any(x in lowered for x in ("raspberry pi 2", "raspberry pi 3", "zero")):
            say(f"{YELLOW}   ! picframe3 does not run on this model.{RESET}")
            note("It renders with OpenGL ES 3, which the Raspberry Pi 4, 400 and 5 "
                 "provide through Mesa's v3d driver. This board has VideoCore IV, "
                 "which stops at ES 2.0. Setup will finish and your settings will "
                 "be saved, but the frame will not start until it is on a Pi 4 "
                 "or newer.")
            ok = False

    from .gfx import drm

    try:
        outputs = list(drm.list_outputs())
    except Exception as exc:
        outputs = []
        say(f"{YELLOW}   ! Could not read the display: {exc}{RESET}")
    if outputs:
        for card, out in outputs:
            say(f"   {GREEN}✓{RESET} {out.name} {out.width}×{out.height}"
                f"@{out.mode.refresh_hz:.0f}Hz on {card}")
    else:
        say(f"{YELLOW}   ! No connected display found.{RESET}")
        note("Check the HDMI cable — on a Pi 4/5 use the socket nearest the USB-C "
             "power connector — and switch the screen on before the Pi. You can "
             "carry on with the setup and fix this afterwards.")
    return ok


def choose_pictures(config: Config) -> None:
    heading("2. Where your pictures are")
    default = config.library.picture_folders[0] if config.library.picture_folders else "~/Pictures"
    folder = ask("   Picture folder", default)
    path = Path(folder).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    config.library.picture_folders = [str(folder)]

    count = 0
    for _root, _dirs, files in os.walk(path):
        count += len(files)
        if count > 5000:
            break
    say(f"   {GREEN}✓{RESET} {path} ({count or 'no'} files so far)")
    if count == 0:
        note("Empty for now — the frame shows a placeholder until you add some, "
             "and picks up new files within seconds without a restart.")


def setup_samba(config: Config, user: str) -> bool:
    """Optional: share the picture folder so photos can be dropped over the network."""
    heading("3. Copying pictures over the network")
    note("A Windows/macOS file share makes the frame appear in Finder or Explorer "
         "so you can drag photographs straight onto it. This is optional.")
    if not confirm("   Set up a network share?", default=True):
        return False

    if shutil.which("smbd") is None:
        say("   Installing Samba…")
        result = run_root(["apt-get", "install", "-y", "-q", "samba", "samba-common-bin"])
        if result.returncode != 0:
            say(f"{YELLOW}   ! Could not install Samba: {result.stderr.strip()[:200]}{RESET}")
            return False

    share_name = ask("   Share name", "Pictures")
    folder = str(Path(config.library.picture_folders[0]).expanduser())

    note("An open share needs no password: the frame appears in Finder and you "
         "drag photographs onto it. Anyone on your network can then add, change "
         "or delete them — which is usually what you want at home, and not what "
         "you want on a shared or office network.")
    open_share = confirm("   Open share, no password?", default=True)

    # The folder must exist and belong to the user Samba acts as.
    run_root(["mkdir", "-p", folder])
    run_root(["chown", "-R", f"{user}:{user}", folder])
    run_root(["chmod", "775", folder])

    block = share_config(share_name, folder, user, open_share=open_share)

    try:
        existing = Path(SAMBA_CONF).read_text(encoding="utf-8")
    except OSError:
        existing = ""
    if SAMBA_BEGIN in existing:
        head, _, rest = existing.partition(SAMBA_BEGIN)
        _, _, tail = rest.partition(SAMBA_END)
        merged = head + block + tail.lstrip("\n")
    else:
        merged = existing.rstrip() + "\n\n" + block

    if not write_as_root(merged, SAMBA_CONF):
        say(f"{YELLOW}   ! Could not write {SAMBA_CONF}{RESET}")
        return False

    if not open_share:
        say(f"   Set a password for the share (user “{user}”). It is separate "
            f"from the login password.")
        while True:
            password = ask("   Share password", secret=True)
            again = ask("   Again", secret=True)
            if password and password == again:
                break
            say("   They did not match; try again.")
        run_root(["smbpasswd", "-a", "-s", user], input_text=f"{password}\n{password}\n")
        run_root(["smbpasswd", "-e", user])
        accounts = run_root(["pdbedit", "-L"]).stdout or ""
        if user not in accounts:
            say(f"{YELLOW}   ! The Samba account for {user} was not created; "
                f"run 'sudo smbpasswd -a {user}' by hand.{RESET}")

    # Ask Samba's own parser rather than hoping: a share nobody can write to
    # looks identical to a working one until you drag a file onto it.
    check = run_root(["testparm", "-s"])
    if check.returncode != 0:
        detail = (check.stderr or check.stdout).strip().splitlines()
        say(f"{YELLOW}   ! Samba rejected the configuration:{RESET}")
        say("     " + (detail[-1][:160] if detail else "see testparm"))
        return False

    run_root(["systemctl", "enable", "smbd"])
    run_root(["systemctl", "restart", "smbd"])
    advertise_share()

    host = socket.gethostname()
    say(f"   {GREEN}✓{RESET} Share ready:")
    say(f"       macOS    smb://{host}.local/{share_name}")
    say(f"       Windows  \\\\{host}\\{share_name}")
    if open_share:
        note("No password: connect as Guest and drag photographs straight in. "
             "If Finder still says read-only, eject the share and reconnect — "
             "it caches the old session.")
    else:
        note(f"Sign in as “{user}” with the password you just set.")
    return True


def share_config(share_name: str, folder: str, user: str, *,
                 open_share: bool) -> str:
    """The smb.conf block for the picture share.

    Two shapes, and the difference is the whole bug report:

    *open* -- `guest only` skips authentication and `force user` makes every
    write land as the frame's own user. No password, anyone on the network can
    drop photographs in.

    *private* -- guest must be switched **off**, not merely unused. Left on,
    Debian's `map to guest = bad user` turns an anonymous connection into the
    `nobody` account without ever prompting, so macOS quietly connects as Guest
    and the share opens read-only with nothing to explain why.
    """
    if open_share:
        access = [
            "   read only = no",
            "   guest ok = yes",
            "   guest only = yes",
            f"   force user = {user}",
            f"   force group = {user}",
        ]
        guest_policy = "   map to guest = bad user"
    else:
        access = [
            "   read only = no",
            "   guest ok = no",
            f"   valid users = {user}",
            f"   force user = {user}",
            f"   force group = {user}",
        ]
        guest_policy = "   map to guest = never"

    lines = [
        SAMBA_BEGIN,
        "[global]",
        guest_policy,
        "   server min protocol = SMB2",
        "",
        f"[{share_name}]",
        "   comment = picframe3 pictures",
        f"   path = {folder}",
        "   browseable = yes",
        *access,
        "   create mask = 0664",
        "   force create mode = 0664",
        "   directory mask = 0775",
        "   force directory mode = 0775",
        "   veto files = /._*/.DS_Store/.Spotlight-V100/.TemporaryItems/.Trashes/",
        "   delete veto files = yes",
        SAMBA_END,
        "",
    ]
    return "\n".join(lines)


def advertise_share() -> bool:
    """Announce the share over mDNS so it appears in Finder's sidebar.

    Samba on Debian serves SMB but does not advertise it over Bonjour, and
    macOS populates its Network list from mDNS.  Without this the share works
    perfectly and is completely invisible — you have to know to type the
    address.  The ``_device-info`` record only picks the icon Finder draws.
    """
    if shutil.which("avahi-daemon") is None:
        result = run_root(["apt-get", "install", "-y", "-q", "avahi-daemon"])
        if result.returncode != 0:
            say(f"{YELLOW}   ! Could not install avahi-daemon; the share will work "
                f"but will not appear by itself in Finder.{RESET}")
            return False

    service = textwrap.dedent("""\
        <?xml version="1.0" standalone='no'?>
        <!DOCTYPE service-group SYSTEM "avahi-service.dtd">
        <service-group>
          <name replace-wildcards="yes">%h</name>
          <service>
            <type>_smb._tcp</type>
            <port>445</port>
          </service>
          <service>
            <type>_device-info._tcp</type>
            <port>0</port>
            <txt-record>model=RackMac</txt-record>
          </service>
        </service-group>
        """)
    if not write_as_root(service, "/etc/avahi/services/picframe3.service"):
        say(f"{YELLOW}   ! Could not write the mDNS announcement.{RESET}")
        return False
    run_root(["systemctl", "enable", "avahi-daemon"])
    run_root(["systemctl", "restart", "avahi-daemon"])
    say(f"   {GREEN}✓{RESET} announced on the network — it will show up in Finder")
    return True


def setup_web(config: Config) -> None:
    heading("4. Controlling the frame from your phone")
    config.http.enabled = confirm("   Enable the web interface?", default=True)
    if not config.http.enabled:
        return
    port = ask("   Port", str(config.http.port))
    try:
        config.http.port = int(port)
    except ValueError:
        pass
    if confirm("   Require a username and password for it?", default=False):
        config.http.auth_user = ask("   Username", "frame")
        config.http.auth_password = ask("   Password", secret=True)
    say(f"   {GREEN}✓{RESET} http://{socket.gethostname()}.local:{config.http.port}/")


def setup_mqtt(config: Config) -> None:
    heading("5. Home Assistant")
    note("If you run Home Assistant, the frame can appear there automatically as a "
         "light, a pause switch, buttons and sensors — no YAML on that side.")
    config.mqtt.enabled = confirm("   Connect to an MQTT broker?", default=False)
    if not config.mqtt.enabled:
        return
    config.mqtt.host = ask("   Broker host", config.mqtt.host or "homeassistant.local")
    config.mqtt.port = int(ask("   Port", str(config.mqtt.port)) or config.mqtt.port)
    config.mqtt.username = ask("   Username", config.mqtt.username)
    if config.mqtt.username:
        config.mqtt.password = ask("   Password", secret=True)
    config.mqtt.device_name = ask("   Name to show in Home Assistant", "Picture Frame")


def setup_look(config: Config) -> None:
    heading("6. How it should look")
    seconds = ask("   Seconds per picture", str(int(config.slideshow.interval)))
    try:
        config.slideshow.interval = float(seconds)
    except ValueError:
        pass

    from .gfx import transitions

    say(f"   Transitions: {', '.join(transitions.names())}")
    config.slideshow.transition = ask("   Transition (or 'random')",
                                      config.slideshow.transition)
    config.viewer.fit = "auto" if confirm(
        "   Frame portrait photos in a mat instead of cropping them?", default=True
    ) else "cover"
    config.slideshow.kenburns = confirm("   Slow pan and zoom (Ken Burns)?", default=False)
    config.viewer.show_clock = confirm("   Show a clock?", default=False)

    if confirm("   Turn the screen off overnight?", default=True):
        off = ask("   Off from", "23:00")
        on = ask("   Back on at", "07:00")
        config.power.schedule = {"all": [f"{off}-{on}"]}


def setup_geo(config: Config) -> None:
    heading("7. Place names")
    note("Photographs with GPS coordinates can show where they were taken. The "
         "lookup uses OpenStreetMap, whose terms ask for a contact address so "
         "they can get in touch if something misbehaves. Nothing but the "
         "coordinates is sent.")
    if not confirm("   Turn place names on?", default=False):
        return
    config.geo.enabled = True
    config.geo.contact = ask("   Your email address", config.geo.contact)
    if not config.geo.contact:
        config.geo.enabled = False
        say(f"{YELLOW}   Skipped — an address is required.{RESET}")


def install_service(user: str, venv_bin: Path | None) -> bool:
    heading("8. Starting automatically")
    if shutil.which("systemctl") is None:
        say(f"{YELLOW}   ! systemd not found; start the frame with 'picframe3 run'.{RESET}")
        return False
    if not confirm("   Start the frame automatically when the Pi powers on?", default=True):
        return False

    if not write_service_unit(user, venv_bin):
        return False
    name = SERVICE_NAME.format(user=user)
    if run_root(["systemctl", "enable", name]).returncode != 0:
        say(f"{YELLOW}   ! Could not enable {name}{RESET}")
        return False
    say(f"   {GREEN}✓{RESET} {name} enabled")
    note("No console autologin and no desktop session are involved: the frame "
         "takes the screen directly, so it starts before anyone logs in.")
    install_udev_rules()
    install_network_rule(user)
    return True


def write_service_unit(user: str, venv_bin: Path | None) -> bool:
    """Write /etc/systemd/system/picframe3@.service and reload systemd."""
    if not write_as_root(_service_unit(user, venv_bin), SERVICE_UNIT):
        say(f"{YELLOW}   ! Could not write {SERVICE_UNIT}{RESET}")
        return False
    run_root(["systemctl", "daemon-reload"])
    return True


def install_udev_rules() -> bool:
    """Make the keyboard, the touchscreen and the screen readable by the frame.

    Raspberry Pi OS Lite runs no seat manager for a service nobody logged into,
    so the ``uaccess`` rules that hand a desktop user their own input devices
    never fire here: ``/dev/input/event*`` stays root-owned at 0600, and being
    in the ``input`` group buys exactly nothing.  This rule is what makes the
    group membership mean something.

    It shipped in ``packaging/`` from the start and was never installed by
    anything, so the only hint anyone got was ``picframe3 doctor`` asking
    whether the user was in the ``input`` group -- which they always were.
    """
    if not os.path.isdir("/etc/udev/rules.d"):
        return False                       # no udev here; nothing to install
    if not write_as_root(packaged("99-picframe3.rules"), UDEV_RULE):
        say(f"{YELLOW}   ! Could not write {UDEV_RULE}; the keyboard and "
            f"touchscreen may stay unreadable.{RESET}")
        return False
    run_root(["udevadm", "control", "--reload"])
    # --reload only affects devices that appear from now on.  Re-triggering
    # applies the rule to the keyboard that is already plugged in, so this
    # works without a reboot.
    run_root(["udevadm", "trigger", "--subsystem-match=input",
              "--subsystem-match=drm"])
    say(f"   {GREEN}✓{RESET} keyboard, touchscreen and screen readable by the frame")
    return True


def install_network_rule(user: str) -> bool:
    """Let the frame reconnect its own Wi-Fi, and nothing else.

    The watchdog can always *see* that the network is gone; mending it needs
    permission.  The unit runs with NoNewPrivileges=yes, so sudo is not an
    option even if it were a good one -- polkit is, and a rule can be narrow in
    a way that sudo cannot: this user, NetworkManager's own device control, and
    restarting the one unit.  Without the rule the frame still watches and
    still reports; only the repair goes missing, which is why a failure here is
    a note rather than an error.
    """
    if shutil.which("pkaction") is None and not os.path.isdir("/etc/polkit-1"):
        return False                       # not a polkit system; nothing to do
    ok = write_as_root(_network_rule(user), POLKIT_RULE)
    if ok:
        say(f"   {GREEN}✓{RESET} may reconnect its own Wi-Fi when it drops")
    else:
        say(f"{YELLOW}   ! Could not write {POLKIT_RULE}; the frame will report "
            f"network outages but not mend them.{RESET}")
    return ok


def _network_rule(user: str) -> str:
    """The polkit rule from ``packaging/``, with this frame's user in it."""
    return "".join(
        line if line.lstrip().startswith("//") else line.replace("@USER@", user)
        for line in packaged("50-picframe3-network.rules").splitlines(keepends=True)
    )


def _service_unit(user: str, venv_bin: Path | None) -> str:
    """The unit from ``packaging/picframe3.service``, pointed at this install.

    Only the executable is substituted.  Everything else -- ``WorkingDirectory``
    (``~``, systemd's own shorthand for the home of ``User=``, so a home
    outside /home works), the ordering, the restart policy -- is whatever that
    file says, which is the point of reading it rather than repeating it.
    """
    executable = str((venv_bin / "picframe3") if venv_bin else Path(sys.argv[0]).resolve())
    if not os.path.exists(executable):
        executable = shutil.which("picframe3") or executable
    # Only the ExecStart line, not the whole file: the comments above it name
    # the placeholder, and a blanket replace would rewrite those too and leave
    # an explanation that no longer explains anything.
    return "".join(
        line.replace("@EXEC@", executable) if line.startswith("ExecStart=") else line
        for line in packaged("picframe3.service").splitlines(keepends=True)
    )


#: Kernel parameters a picture frame wants: never blank the console, no
#: raspberry logos, no blinking cursor, and no boot messages -- all four are
#: things that otherwise appear over the picture.
CMDLINE_WANTED = ("consoleblank=0", "logo.nologo", "vt.global_cursor_default=0", "quiet")


def tidy_boot() -> None:
    """Stop the console blanking or printing over the picture.

    cmdline.txt is a single line by definition: the bootloader hands the first
    line to the kernel and ignores everything after it.  The previous version
    read the file, called ``.strip()`` -- which removes whitespace at the two
    ends and nothing in between -- and wrote the result back with the missing
    parameters appended.  A file that had somehow acquired a second line
    therefore came back still split in two, with the frame's parameters
    stranded on a line the kernel never reads, and in the worst case a Pi that
    does not boot.  This runs in the ``--yes`` path, which is to say on every
    single update, so one bad write is permanent.

    Hence the three rules below: keep a copy of what was there, refuse outright
    anything that is not exactly one line, and check that what we are about to
    write is still one line before writing it.
    """
    for candidate in ("/boot/firmware/cmdline.txt", "/boot/cmdline.txt"):
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            say(f"{YELLOW}   ! Could not read {candidate}: {exc}{RESET}")
            return

        lines = [line for line in raw.splitlines() if line.strip()]
        if len(lines) != 1:
            say(f"{YELLOW}   ! {candidate} holds {len(lines)} lines, not one; "
                f"leaving it alone.{RESET}")
            note("Only the first line of cmdline.txt reaches the kernel, so "
                 "there is no safe way to guess which one the parameters "
                 "belong on. Put the kernel command line back on a single "
                 "line and run 'picframe3 setup' again, or append these to it "
                 "by hand: " + " ".join(CMDLINE_WANTED) + ".")
            return

        line = lines[0].strip()
        missing = [want for want in CMDLINE_WANTED if want not in line.split()]
        if not missing:
            return
        new_line = line + " " + " ".join(missing)
        if len(new_line.splitlines()) != 1:     # cannot happen; cheap to prove
            say(f"{YELLOW}   ! Refusing to write a multi-line {candidate}.{RESET}")
            return

        backup = candidate + ".picframe3.bak"
        if run_root(["cp", candidate, backup]).returncode != 0:
            say(f"{YELLOW}   ! Could not back {candidate} up to {backup}; "
                f"leaving the boot options alone.{RESET}")
            return
        # mode=None: /boot/firmware is FAT, where `install -m` would attempt a
        # chmod the filesystem cannot represent.
        if write_as_root(new_line + "\n", candidate, mode=None):
            say(f"   {GREEN}✓{RESET} boot options tidied ({', '.join(missing)})")
            note(f"The previous {Path(candidate).name} is kept as "
                 f"{Path(backup).name}, next to it.")
        else:
            say(f"{YELLOW}   ! Could not write {candidate}.{RESET}")
        return


def fix_groups(user: str) -> bool:
    absent = missing_groups(user)
    if not absent:
        return True
    say(f"   Adding {user} to: {', '.join(absent)}")
    result = run_root(["usermod", "-aG", ",".join(absent), user])
    if result.returncode != 0:
        say(f"{YELLOW}   ! Could not add the groups: {result.stderr.strip()[:160]}{RESET}")
        return False
    return False        # membership only applies after a fresh login


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run(config_path: str | None = None, *, venv_bin: str | None = None,
        assume_yes: bool = False) -> int:
    user = os.environ.get("SUDO_USER") or getpass.getuser()
    target = os.path.expanduser(config_path or DEFAULT_CONFIG_PATHS[0])

    say(f"{BOLD}picframe3 {__version__} — setup{RESET}")
    note("Every question has a sensible default in brackets; press Return to take "
         "it. You can run 'picframe3 setup' again at any time, and nothing here "
         "is destructive.")

    if not _tty() or assume_yes:
        say("\nTaking every default (no interactive terminal, or --yes).")
        config = Config.load(target) if os.path.exists(target) else Config()
        for folder in config.library.picture_folders:
            Path(folder).expanduser().mkdir(parents=True, exist_ok=True)
        config.save(target)
        say(f"   config          {target}")
        say(f"   pictures        {', '.join(config.library.picture_folders)}")
        fix_groups(user)
        tidy_boot()
        # The update path goes through here (install.sh runs `setup --yes`), so
        # anything installed outside the config file has to be refreshed here
        # too -- otherwise a fix to the unit or to a rule only ever reaches
        # people who run the wizard interactively.
        install_udev_rules()
        if shutil.which("systemctl") and Path("/run/systemd/system").exists():
            if write_service_unit(user, Path(venv_bin) if venv_bin else None):
                run_root(["systemctl", "enable", SERVICE_NAME.format(user=user)])
                say(f"   service         {SERVICE_NAME.format(user=user)} enabled")
        say("   Run 'picframe3 setup' on a terminal to change any of this.")
        return 0

    hardware_ok = check_hardware()

    config = Config.load(target) if os.path.exists(target) else Config()
    if os.path.exists(target):
        note(f"Starting from your existing {target}.")

    choose_pictures(config)
    shared = setup_samba(config, user)
    setup_web(config)
    setup_mqtt(config)
    setup_look(config)
    setup_geo(config)

    config.save(target)
    say(f"\n   {GREEN}✓{RESET} configuration written to {target}")

    groups_ready = fix_groups(user)
    tidy_boot()
    service = install_service(user, Path(venv_bin) if venv_bin else None)

    # ---------------------------------------------------------------- finish
    heading("Done")
    host = socket.gethostname()
    steps: list[str] = []
    if not groups_ready:
        steps.append("Reboot (or log out and back in) so the new group membership "
                     "takes effect:  sudo reboot")
    steps.append("Index your pictures:  picframe3 scan")
    if service:
        steps.append(f"Start it now:  sudo systemctl start picframe3@{user}")
    else:
        steps.append("Start it:  picframe3 run")
    if config.http.enabled:
        steps.append(f"Open the frame's page:  http://{host}.local:{config.http.port}/")
    if shared:
        steps.append(f"Drop photographs on it:  smb://{host}.local/ (macOS) "
                     f"or \\\\{host}\\ (Windows)")
    steps.append("If anything looks wrong:  picframe3 doctor")

    for index, step in enumerate(steps, 1):
        say(f"   {index}. {step}")
    if not hardware_ok:
        say(f"\n{YELLOW}   Note: this Pi model is below the recommended "
            f"hardware (see above).{RESET}")
    say("")
    return 0
