"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time

from . import __version__
from .config import DEFAULT_CONFIG_PATHS, Config

# picframe3 is not on PyPI, so `pip install picframe3[...]` is a 404 and was
# the wrong half of every repair hint doctor printed.  Installing from the
# repository is what works, and the definitions live in picframe3.install so
# that every module reporting a missing extra -- the web server, MQTT, evdev,
# GPIO -- names the same command.
from .install import install_hint as _install_hint
from .install import pip_command as _pip

_log = logging.getLogger("picframe3")

#: Exit status for the `quit` action. 128 + SIGTERM, the conventional "this
#: was told to stop", and what `RestartPreventExitStatus=143` in
#: packaging/picframe3.service keys off so systemd leaves the frame stopped.
QUIT_EXIT_CODE = 143



# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def setup_logging(level: str = "INFO", file: str = "", journald: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    under_systemd = bool(os.environ.get("JOURNAL_STREAM") or os.environ.get("INVOCATION_ID"))
    if under_systemd and journald:
        # systemd already timestamps every line; repeating it wastes the journal.
        fmt = "%(name)s: %(message)s"
    else:
        fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    root.addHandler(stream)

    if file:
        from logging.handlers import RotatingFileHandler

        path = os.path.expanduser(file)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        rotating = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3)
        rotating.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(rotating)

    for noisy in ("PIL", "uvicorn", "uvicorn.error", "asyncio", "aiomqtt"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_run(args) -> int:
    from .app import PicFrame

    config = Config.load(args.config)
    if args.backend:
        config.display.backend = args.backend
    if args.folder:
        config.library.picture_folders = list(args.folder)
    if args.no_http:
        config.http.enabled = False
    if args.interval:
        config.slideshow.interval = args.interval
    setup_logging(args.log_level or config.logging.level,
                  config.logging.file, config.logging.journald)
    _log.info("picframe3 %s starting", __version__)

    frame = PicFrame(config)

    async def main() -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, frame.request_stop)
            except NotImplementedError:  # pragma: no cover
                pass
        return await frame.run()

    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        return 0
    except Exception:
        _log.exception("picframe3 stopped with an error")
        return 1

    # Checked before the restart flag because the two are exclusive and this
    # one is the more final: a frame asked to shut down must not be brought
    # back by the restart path.
    if getattr(frame, "power_off_requested", False):
        return _power_off()
    if frame.restart_requested:
        return _restart(frame)
    # `quit` means stop, and under systemd a clean exit does not stop anything:
    # the unit has Restart=always, so ESC, Q, the `quit` API action and the
    # MQTT command all exited 0 and had the frame back on screen five seconds
    # later. Exiting 143 instead is what RestartPreventExitStatus=143 in the
    # unit keys off. Started by hand there is no supervisor and the status is
    # simply what the shell reports.
    if getattr(frame, "quit_requested", False):
        _log.info("quit requested; exiting %d so the frame stays stopped",
                  QUIT_EXIT_CODE)
        return QUIT_EXIT_CODE
    return code


def _power_off() -> int:
    """Power the Pi off, now that the screen has been handed back.

    The frame asks systemd rather than calling ``poweroff`` itself: the unit
    runs as an ordinary user with ``NoNewPrivileges=yes``, so the permission
    comes from one narrow polkit rule -- logind's ``power-off`` action for this
    user and nothing else -- which `picframe3 setup` installs as
    /etc/polkit-1/rules.d/55-picframe3-power.rules.

    A refusal leaves the Pi running, and this returns 1 rather than 143 for
    that case: a fault is what ``Restart=always`` turns back into a picture on
    the wall, whereas 143 would leave a dark screen and nothing explaining it
    except the journal.
    """
    import shutil
    import subprocess

    if shutil.which("systemctl") is None:
        _log.error("no systemctl here, so nothing to ask for a power-off; "
                   "the frame stays up")
        return 1
    _log.info("shutdown requested; asking systemd to power the Pi off")
    try:
        result = subprocess.run(["systemctl", "poweroff"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.error("could not power off (%s); the frame will come back", exc)
        return 1
    if result.returncode != 0:
        _log.error("could not power off (%s); the frame will come back. Run "
                   "'picframe3 setup' to install the polkit rule that permits "
                   "it.", (result.stderr or "").strip() or result.returncode)
        return 1
    # The job is queued; systemd stops this unit as part of the shutdown.
    # Exiting 143 keeps it from being restarted in the seconds in between.
    return QUIT_EXIT_CODE


def _restart(frame) -> int:
    """Come back up after a restart request.

    Under systemd, exiting cleanly is the whole job: ``Restart=always`` starts
    a fresh unit a few seconds later, in a new cgroup, with the DRM device and
    every GL and GStreamer resource released by the kernel rather than by code
    unwinding itself while it still owns the screen.

    Started by hand there is no supervisor, so the process re-executes itself
    with the arguments it was given.  This happens after the event loop has
    finished and ``shutdown()`` has run, so nothing is still holding the card.
    """
    if frame.under_systemd():
        _log.info("exiting for restart; systemd will start picframe3 again")
        return 0
    executable = sys.argv[0]
    _log.info("restarting: %s %s", executable, " ".join(sys.argv[1:]))
    try:
        os.execv(executable, sys.argv)
    except OSError as exc:      # pragma: no cover - depends on how it was started
        _log.error("could not restart automatically (%s); start picframe3 again "
                   "by hand", exc)
        return 1
    return 0                    # unreachable: execv does not return


def cmd_scan(args) -> int:
    from .library.db import Library
    from .library.scanner import Scanner
    from .media.geocode import Geocoder

    config = Config.load(args.config)
    setup_logging(args.log_level or "INFO", "", False)
    library = Library(config.library.database)
    geocoder = None
    if config.geo.enabled:
        geocoder = Geocoder(os.path.expanduser(config.geo.cache),
                            contact=config.geo.contact, enabled=True,
                            key_order=config.geo.key_order,
                            suppress=config.geo.suppress,
                            language=config.geo.language)
    scanner = Scanner(
        library,
        args.folder or config.library.picture_folders,
        follow_links=config.library.follow_links,
        geocoder=geocoder,
        include_videos=config.library.include_videos,
        ignore_hidden=config.library.ignore_hidden,
        exclude=config.library.exclude,
        prune_max_fraction=config.library.prune_max_fraction,
    )

    def progress(n: int, path: str) -> None:
        print(f"\r  {n:>7} files… {os.path.basename(path)[:48]:<48}", end="", flush=True)

    result = scanner.scan(prune=not args.no_prune, progress=progress)
    print("\r" + " " * 72 + "\r", end="")
    print(result.summary())

    if geocoder is not None:
        if args.regeocode:
            cleared = library.clear_locations()
            print(f"  cleared {cleared} cached place names")
        missing = library.count_locations_missing()
        if missing:
            print(f"  resolving {missing} place names "
                  f"(about one a second, as Nominatim requires)…")
            done = 0

            def show(_n: int, place: str) -> None:
                print(f"\r    {done + _n:>6} · {place[:44]:<44}", end="", flush=True)

            while True:
                batch = scanner.backfill_locations(limit=25, progress=show)
                done += batch
                if batch == 0:
                    break
            print("\r" + " " * 72 + "\r", end="")
            print(f"  {done} place names resolved, "
                  f"{library.count_locations_missing()} could not be identified")
    elif library.count_locations_missing():
        print(f"  {library.count_locations_missing()} photos have GPS coordinates; "
              f"set geo.enabled and geo.contact to turn them into place names")
    if result.errors:
        print(f"  {len(result.errors)} files could not be read")
        for path in result.errors[:10]:
            print(f"    {path}")
    stats = library.stats()
    print(f"  index now holds {stats['files']} files "
          f"({stats['videos']} videos) across {stats['folders']} folders")
    scanner.close()
    library.close()
    return 0


def cmd_doctor(args) -> int:
    """Check everything the frame needs, and say what to do about each gap."""
    setup_logging(args.log_level or "WARNING", "", False)
    ok = True
    print(f"picframe3 {__version__}  ·  python {sys.version.split()[0]}\n")

    def check(label: str, good: bool, detail: str = "", fix: str = "") -> bool:
        mark = "✓" if good else "✗"
        print(f" {mark} {label}" + (f"  {detail}" if detail else ""))
        if not good and fix:
            print(f"     → {fix}")
        return good

    # -- configuration
    config_path = args.config
    try:
        config = Config.load(config_path)
        loaded = config.source_path and os.path.exists(os.path.expanduser(config.source_path))
        check("configuration", True,
              config.source_path if loaded
              else f"built-in defaults (no file at {config.source_path})")
        if not loaded:
            print("     → optional: picframe3 config --init")
    except Exception as exc:
        check("configuration", False, str(exc),
              "create one with: picframe3 config --init")
        config = Config()
        ok = False

    # -- the language dates come out in
    wanted_locale = (config.viewer.locale or "").strip()
    if wanted_locale:
        from .config import set_time_locale

        # set_time_locale says so in the log when it cannot; here the ✗ line
        # and its fix say it better, so keep the warning out of the report.
        logging.disable(logging.WARNING)
        try:
            applied = set_time_locale(wanted_locale)
        finally:
            logging.disable(logging.NOTSET)
        if applied:
            check("date language", True,
                  f"{wanted_locale} — {time.strftime(config.viewer.date_format)}")
        else:
            ok &= check(
                "date language", False,
                f"{wanted_locale} is not generated on this system",
                "sudo dpkg-reconfigure locales   (tick it, then restart the frame)",
            )

    # -- graphics
    try:
        from .gfx import drm

        wanted_mode = (config.display.mode or "").strip()
        outputs = list(drm.list_outputs(mode=wanted_mode))
        if outputs:
            for card, out in outputs:
                print(f"   {card}: {out.name} {out.width}x{out.height}"
                      f"@{out.mode.refresh_hz:.2f}Hz")
            check("DRM display", True, f"{len(outputs)} connected output(s)")
            if wanted_mode:
                asked = drm.parse_mode(wanted_mode)
                if asked is None:
                    ok &= check("display.mode", False,
                                f"“{wanted_mode}” is not a mode",
                                "write it as 3840x2160, or 3840x2160@30")
                else:
                    width, height, hz = asked
                    using = [out for _, out in outputs
                             if (out.mode.hdisplay, out.mode.vdisplay) == (width, height)
                             and (hz is None or abs(out.mode.refresh_hz - hz) <= 0.5)]
                    if using:
                        check("display.mode", True, drm.mode_name(using[0].mode))
                    else:
                        # The whole point of the check: a mode the screen does
                        # not offer is silently ignored at startup, and the
                        # frame then runs at a resolution nobody chose.
                        offered = ", ".join(dict.fromkeys(
                            name for _, out in outputs for name in out.modes))
                        ok &= check("display.mode", False,
                                    f"“{wanted_mode}” is not offered by this screen",
                                    f"this screen offers: {offered}")
        else:
            ok &= check("DRM display", False, "no connected outputs",
                        "check the HDMI cable, or run with --backend headless to test")
    except Exception as exc:
        ok &= check("DRM display", False, str(exc),
                    "install libdrm2 and add this user to the 'video' group")

    for path in ("/dev/dri/card0", "/dev/dri/card1", "/dev/dri/renderD128"):
        if os.path.exists(path):
            readable = os.access(path, os.R_OK | os.W_OK)
            if not readable:
                ok &= check(f"access to {path}", False, "permission denied",
                            "sudo usermod -aG video,render $USER   (then log out and back in)")
            else:
                check(f"access to {path}", True)

    try:
        from .gfx import create_backend, gl

        backend = create_backend("headless", width=64, height=64)
        backend.make_current()
        version = gl.get_string(gl.VERSION)
        renderer = gl.get_string(gl.RENDERER)
        if gl.es_version_ok(version):
            check("OpenGL ES", True, f"{version} on {renderer}")
        else:
            major, minor = gl.REQUIRED_ES_VERSION
            ok &= check(
                "OpenGL ES", False, f"{version} on {renderer}",
                f"picframe3 needs OpenGL ES {major}.{minor}. The Raspberry Pi 4, "
                f"400 and 5 provide it; the Pi 2, 3 and Zero 2 W do not.",
            )
        backend.close()
    except Exception as exc:
        ok &= check("OpenGL ES", False, str(exc),
                    "install libgles2 and mesa drivers: sudo apt install libgles2 libgbm1")

    # -- media
    try:
        from PIL import Image, features

        bits = [f"Pillow {Image.__version__}"]
        for feature in ("libjpeg_turbo", "webp", "raqm"):
            if features.check(feature):
                bits.append(feature)
        check("image decoding", True, ", ".join(bits))
        if not features.check("raqm"):
            print("     → optional: sudo apt install libraqm0  (shaping for "
                  "Arabic/Hebrew/Indic captions)")
    except Exception as exc:
        ok &= check("image decoding", False, str(exc), f"{_pip()} install Pillow")

    try:
        import pillow_heif  # noqa: F401

        check("HEIC/HEIF support", True)
    except ImportError:
        check("HEIC/HEIF support", False, "not installed",
              f"{_install_hint('heif')}   (needed for iPhone photos)")

    from .media import video

    if video.available():
        check("video playback", True, "GStreamer")
    else:
        check("video playback", False, "GStreamer bindings missing",
              "sudo apt install python3-gi gstreamer1.0-plugins-good "
              "gstreamer1.0-libav gir1.2-gst-plugins-base-1.0")

    from .gfx import textstyle

    check("fonts", textstyle.font_available(), "",
          "sudo apt install fonts-dejavu-core")

    # -- library
    for folder in config.library.picture_folders:
        path = os.path.expanduser(folder)
        exists = os.path.isdir(path)
        ok &= check(f"picture folder {folder}", exists,
                    "" if exists else "missing",
                    f"mkdir -p {path}   (or change library.picture_folders)")
    try:
        from .library.db import Library

        library = Library(config.library.database)
        stats = library.stats()
        check("photo index", True,
              f"{stats['files']} files, {stats['db_bytes'] // 1024} KiB at {stats['db_path']}")
        if stats["files"] == 0:
            print("     → run: picframe3 scan")
        library.close()
    except Exception as exc:
        ok &= check("photo index", False, str(exc))

    # -- the Pi itself
    #
    # Two things kill a frame on a wall: heat and a power supply that cannot
    # hold five volts.  Both are silent -- the picture looks perfect while the
    # card is being corrupted -- so commissioning is exactly when to look.
    from . import health as health_module

    folders = config.library.picture_folders
    disk_path = config.health.disk_path or (folders[0] if folders else "")
    reading = health_module.sample(disk_path)

    temperature = reading["cpu_temp"]
    if temperature is None:
        print("   note: this machine does not report a CPU temperature")
    else:
        check("CPU temperature", temperature < 80.0, f"{temperature:.1f} °C",
              "the Pi throttles from 80 °C. Give it air, or fit a heatsink or "
              "the active cooler; a frame in a sealed box cooks itself.")
        ok &= temperature < 80.0

    power = reading["power"]
    if power is None:
        print("   note: power supply flags unavailable "
              "(vcgencmd missing, or this user is not in the 'video' group)")
    elif power.get("undervoltage") or power.get("undervoltage_since_boot"):
        ok &= check(
            "power supply", False,
            "undervoltage now" if power.get("undervoltage")
            else "undervoltage recorded since boot",
            "use the official supply (5 V 3 A for the Pi 4, 5 V 5 A for the Pi 5) "
            "and a short, thick cable. Undervoltage corrupts SD cards.")
    else:
        check("power supply", True, "no undervoltage recorded")

    free = reading["disk"]
    if free:
        roomy = free["free"] > 512 * 1024 * 1024
        ok &= check(f"free space on {free['path']}", roomy,
                    f"{free['free'] / 1024 ** 3:.1f} GiB free "
                    f"({free['used_percent']:.0f}% used)",
                    "make room: thumbnails, the index and the journal all need "
                    "somewhere to go.")

    # -- the network the frame hangs on
    #
    # Commissioning is the moment to find out whether the watchdog will be able
    # to do anything when it matters.  A frame whose gateway cannot be found,
    # or whose polkit rule never got installed, still reports outages -- but it
    # will sit through them, and nobody discovers that until the first one.
    if config.network.enabled:
        from . import network as network_module

        route = network_module.default_route()
        if route is None:
            ok &= check("network", False, "no default route",
                        "the frame cannot see a router. Check the Wi-Fi "
                        "credentials, or wire it up.")
        else:
            gateway, iface = route
            check("network", True, f"{gateway} via {iface}")
            if config.network.repair:
                check("may mend its own connection", *_repair_permission())

    # -- the Shutdown button
    #
    # Powering the Pi off needs a polkit rule in the same way mending the
    # network does, and the moment to find out that it is missing is here --
    # not from the sofa, when the button reports that it could not.
    if _has("systemctl"):
        check("may power the Pi off", *_power_off_permission())

    # -- Syncthing, when the frame is meant to be running it
    #
    # A frame whose pictures arrive over Syncthing has a second thing that can
    # be quietly broken: the service is off, or it is running but has no
    # folder, and the photographs simply stop arriving. Nothing on the wall
    # says so -- the last picture looks perfect -- so doctor asks.
    if config.sync.enabled:
        from . import sync as sync_module

        state = sync_module.status(config)
        if not state["installed"]:
            ok &= check("Syncthing", False, "switched on, but not installed",
                        "picframe3 sync on")
        elif not state["running"]:
            ok &= check("Syncthing", False, "installed, but not running",
                        f"sudo systemctl start {state['unit']}")
        elif state["error"]:
            ok &= check("Syncthing", False, state["error"])
        elif state["folder"] is None:
            ok &= check("Syncthing", False, "running, but keeping no folder "
                        "for the frame", "picframe3 sync folder")
        else:
            folder = state["folder"]
            check("Syncthing", True,
                  f"{state['version'] or 'running'} · {folder['path']} · "
                  f"{folder['files']} files · "
                  f"{len(state['devices'])} paired machine(s)")
            if folder["need_bytes"]:
                print(f"     → {folder['need_bytes'] / 1024 ** 2:.0f} MiB still "
                      f"on its way in")
            if state["pending"]:
                print(f"     → {len(state['pending'])} machine(s) waiting to be "
                      f"paired; accept them on the settings page")

    # -- the keyboard and the touchscreen
    #
    # Group membership is necessary and nowhere near sufficient. Raspberry Pi
    # OS Lite runs no seat manager for a service nobody logged into, so the
    # uaccess rules never fire and /dev/input/event* stays root-owned at 0600
    # until packaging/99-picframe3.rules is installed. Actually opening a node
    # is the only test that distinguishes the two, which is why asking "is
    # this user in the 'input' group?" -- and they always were -- was such a
    # misleading thing for doctor to say on its own.
    import glob

    nodes = sorted(glob.glob("/dev/input/event*"))
    if not nodes:
        print("   note: no /dev/input/event* devices — nothing is plugged in, "
              "which is normal for a frame driven only from its web page")
    else:
        readable = []
        for node in nodes:
            try:
                os.close(os.open(node, os.O_RDONLY | os.O_NONBLOCK))
                readable.append(node)
            except OSError:
                pass
        ok &= check("keyboard and touch devices", bool(readable),
                    f"{len(readable)} of {len(nodes)} readable",
                    "if you have just installed picframe3, reboot: the 'input' "
                    "group only applies to a fresh login. Otherwise run "
                    "'picframe3 setup', which installs "
                    "/etc/udev/rules.d/99-picframe3.rules — the rule that makes "
                    "that group membership mean anything.")

    # -- optional services
    for module, label, extra in (
        ("fastapi", "web interface", "web"),
        ("aiomqtt", "MQTT / Home Assistant", "mqtt"),
        ("evdev", "keyboard & touch input", "input"),
        ("gpiozero", "GPIO buttons", "gpio"),
    ):
        try:
            __import__(module)
            check(label, True)
        except ImportError:
            check(label, False, "not installed", _install_hint(extra))

    # -- who am I
    #
    # SUDO_USER first, then this process's own uid. os.getlogin() was neither:
    # it reports the owner of the controlling terminal and raises outright
    # when there is none, which is every `ssh host picframe3 doctor` and every
    # run from a service -- so the whole group check was quietly skipped in
    # exactly the situations where it mattered.
    import pwd

    from .wizard import missing_groups

    user = os.environ.get("SUDO_USER") or pwd.getpwuid(os.geteuid()).pw_name
    for needed in missing_groups(user):
        print(f"   note: {user} is not in the '{needed}' group")

    print("\n" + ("All good." if ok else "Some checks failed — see the arrows above."))
    return 0 if ok else 1


def _has(program: str) -> bool:
    import shutil

    return shutil.which(program) is not None


def _polkit_knows(action_id: str) -> bool:
    """Whether polkit itself has heard of the action a rule is written for.

    A rule file on disk says nothing about whether anything will ever read it:
    on a system with no polkit daemon -- which a minimal image may well be --
    the file sits there forever while every request is refused.
    """
    import subprocess

    if not _has("pkaction"):
        return False
    try:
        return subprocess.run(["pkaction", "--action-id", action_id],
                              capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _power_off_permission() -> tuple[bool, str, str]:
    """Can the frame really power the Pi off when the button is pressed?"""
    rule = "/etc/polkit-1/rules.d/55-picframe3-power.rules"
    if os.path.exists(rule) and _polkit_knows("org.freedesktop.login1.power-off"):
        return True, "polkit rule installed and polkit knows the action", ""
    if not os.path.exists(rule):
        return (False, "polkit rule missing",
                "run 'picframe3 setup' to install it. Without it the Shutdown "
                "button reports that it could not power the Pi off.")
    return (False, "polkit is not available, so the rule is never read",
            "install polkitd; the frame can then power itself off.")


def _repair_permission() -> tuple[bool, str, str]:
    """Can the frame really mend its own network connection?

    Two things have to be true and only one of them was checked. The rule file
    being on disk says nothing about whether anything will ever read it, which
    is what ``_polkit_knows`` asks about; doctor used to report a tick for a
    rule nothing would ever read.
    """
    rule = "/etc/polkit-1/rules.d/50-picframe3-network.rules"
    have_rule = os.path.exists(rule)
    have_polkit = _polkit_knows("org.freedesktop.NetworkManager.network-control")

    if have_rule and have_polkit:
        return True, "polkit rule installed and polkit knows the action", ""
    if not have_rule:
        return (False, "polkit rule missing",
                "run 'picframe3 setup' to install it, or set "
                "network.repair: false to watch without mending.")
    return (False, "polkit is not available, so the rule is never read",
            "install polkitd and NetworkManager, or set network.repair: false "
            "so the frame reports outages without trying to mend them.")


def cmd_config(args) -> int:
    setup_logging(args.log_level or "WARNING", "", False)
    if args.init:
        target = args.config or DEFAULT_CONFIG_PATHS[0]
        path = os.path.expanduser(target)
        if os.path.exists(path) and not args.force:
            print(f"{path} already exists; pass --force to overwrite")
            return 1
        config = Config()
        config.save(path)
        print(f"wrote a commented default configuration to {path}")
        return 0
    config = Config.load(args.config)
    if args.get:
        print(config.get(args.get))
        return 0
    if args.set:
        key, _, value = args.set.partition("=")
        config.set(key.strip(), _literal(value.strip()))
        config.save()
        print(f"{key.strip()} = {config.get(key.strip())}")
        return 0
    import yaml

    print(yaml.safe_dump(config.as_dict(), sort_keys=False, allow_unicode=True))
    return 0


def _literal(text: str):
    import yaml

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def cmd_sync(args) -> int:
    """Syncthing from a terminal: where it stands, on, off, or re-align it.

    The settings page does all of this, and this exists for the two occasions
    it cannot: a frame whose web interface is switched off, and somebody on
    the end of an SSH session who would rather see it written down.
    """
    setup_logging(args.log_level or "WARNING", "", False)
    from . import sync as sync_module

    config = Config.load(args.config)
    action = args.action or "status"

    try:
        if action == "on":
            config.sync.enabled = True
            config.save()
            print("switching Syncthing on — installing it if it is missing, "
                  "which takes a minute or two…")
            sync_module.apply(config, switch=True)
        elif action == "off":
            config.sync.enabled = False
            config.save()
            sync_module.apply(config, switch=False)
            print("Syncthing stopped. Its folders, pairings and photographs "
                  "are untouched.")
            return 0
        elif action == "folder":
            sync_module.apply(config)
    except sync_module.SyncError as exc:
        print(f"✗ {exc}")
        return 1

    state = sync_module.status(config)
    print(f"configured   {'on' if state['configured'] else 'off'}")
    print(f"installed    {'yes' if state['installed'] else 'no'}")
    print(f"running      {'yes' if state['running'] else 'no'}"
          f"{' (starts with the Pi)' if state['enabled_at_boot'] else ''}")
    if state["device_id"]:
        print(f"this frame   {state['device_id']}")
    if state["folder"]:
        folder = state["folder"]
        print(f"folder       {folder['path']}  ·  {folder['type']}  ·  "
              f"{folder['files']} files  ·  {folder['state'] or 'idle'}")
    elif state["installed"]:
        print("folder       not set up yet — run: picframe3 sync folder")
    for device in state["devices"]:
        print(f"paired with  {device['name']}  "
              f"{'connected' if device['connected'] else 'offline'}"
              f"{'' if device['shares_the_pictures'] else '  (not sharing the pictures)'}")
    for device in state["pending"]:
        print(f"waiting      {device['name'] or device['device_id'][:7]} wants to "
              f"pair — accept it on the settings page, or in Syncthing itself")
    if state["error"]:
        print(f"note         {state['error']}")
    return 0


def cmd_demo(args) -> int:
    """Render sample frames offscreen -- useful before any hardware exists."""
    setup_logging(args.log_level or "INFO", "", False)
    from .demo import render_demo

    out = render_demo(args.output, args.folder, width=args.width, height=args.height,
                      transition=args.transition)
    print(f"wrote {out}")
    return 0


def cmd_setup(args) -> int:
    setup_logging(args.log_level or "WARNING", "", False)
    from .wizard import run as run_wizard

    return run_wizard(args.config, venv_bin=args.venv_bin, assume_yes=args.yes)


#: Where `picframe3 setup` and the installer put the shim on PATH.
SHIM = "/usr/local/bin/picframe3"


def cmd_uninstall(args) -> int:
    """Undo everything setup put on the system, and nothing else.

    An uninstall that removes only the unit is not an uninstall. What was left
    behind was a polkit rule granting network control to a user who may not
    even exist any more, a udev rule, a Samba share still offering the picture
    folder to the whole network, and a /usr/local/bin/picframe3 pointing into
    a virtual environment that is about to be deleted -- so the next thing the
    owner typed was `picframe3`, and got "No such file or directory".

    Pictures, configuration and index are deliberately kept: they are the part
    that took work, and this command is also how people move a frame to
    another SD card.
    """
    setup_logging(args.log_level or "WARNING", "", False)
    import getpass
    import shutil

    from .wizard import (
        POLKIT_RULE,
        POWER_POLKIT_RULE,
        SAMBA_BEGIN,
        SAMBA_CONF,
        SAMBA_END,
        SERVICE_NAME,
        SERVICE_UNIT,
        SYNC_HELPER,
        SYNC_OFF_UNIT,
        SYNC_ON_UNIT,
        SYNC_POLKIT_RULE,
        UDEV_RULE,
        run_root,
        say,
        write_as_root,
    )

    user = os.environ.get("SUDO_USER") or getpass.getuser()
    name = SERVICE_NAME.format(user=user)
    run_root(["systemctl", "disable", "--now", name])
    run_root(["rm", "-f", SERVICE_UNIT])
    run_root(["systemctl", "daemon-reload"])
    say(f"stopped and removed {name}")

    if os.path.exists(POLKIT_RULE):
        run_root(["rm", "-f", POLKIT_RULE])
        say(f"removed {POLKIT_RULE} (permission to mend the network)")
    if os.path.exists(POWER_POLKIT_RULE):
        run_root(["rm", "-f", POWER_POLKIT_RULE])
        say(f"removed {POWER_POLKIT_RULE} (permission to power the Pi off)")
    if os.path.exists(UDEV_RULE):
        run_root(["rm", "-f", UDEV_RULE])
        if shutil.which("udevadm"):
            run_root(["udevadm", "control", "--reload"])
        say(f"removed {UDEV_RULE} (input and DRM device permissions)")

    # The Syncthing switch, but never Syncthing itself: it may well be
    # keeping folders that have nothing to do with the frame, and a package
    # this uninstaller did not necessarily install is not its to remove.
    leftovers = [path for path in (SYNC_POLKIT_RULE, SYNC_ON_UNIT,
                                  SYNC_OFF_UNIT, SYNC_HELPER)
                 if os.path.exists(path)]
    if leftovers:
        run_root(["rm", "-f", *leftovers])
        run_root(["systemctl", "daemon-reload"])
        say("removed the Syncthing switch (the units, the helper and the "
            "polkit rule). Syncthing itself, its folders and its pairings "
            "were left alone.")

    # Only our own shim. If something else put a picframe3 there -- a distro
    # package, a hand-written script -- removing it would be vandalism, and a
    # symlink that no longer resolves is exactly what we are cleaning up.
    if os.path.islink(SHIM) or os.path.isfile(SHIM):
        target = os.path.realpath(SHIM)
        if "picframe3" in target:
            run_root(["rm", "-f", SHIM])
            say(f"removed {SHIM}")
        else:
            say(f"left {SHIM} alone: it points at {target}, which is not ours")

    _remove_samba_block(SAMBA_CONF, SAMBA_BEGIN, SAMBA_END, run_root, say,
                        write_as_root)

    say("Your configuration, index and pictures were left alone.")
    say(f"  config  {Config.load(args.config).source_path}")
    say("The virtual environment is still at "
        "~/.local/share/picframe3/venv; delete that folder to reclaim the space.")
    return 0


def _remove_samba_block(conf, begin, end, run_root, say, write_as_root) -> None:
    """Take the frame's share back out of smb.conf, leaving the rest intact.

    The wizard writes the block between two markers precisely so it can be
    lifted out again without a parser and without touching a line anybody else
    put in the file.
    """
    try:
        with open(conf, encoding="utf-8") as handle:
            existing = handle.read()
    except OSError:
        return
    if begin not in existing or end not in existing:
        return
    head, _, rest = existing.partition(begin)
    _, _, tail = rest.partition(end)
    merged = head.rstrip("\n") + "\n" + tail.lstrip("\n")
    if write_as_root(merged, conf):
        say(f"removed the picframe3 share from {conf}")
        run_root(["systemctl", "restart", "smbd"])
    else:
        say(f"could not edit {conf}; the picframe3 share is still in it")


def cmd_migrate(args) -> int:
    setup_logging(args.log_level or "WARNING", "", False)
    from .migrate import migrate

    target = args.output or (args.config or DEFAULT_CONFIG_PATHS[0])
    target = os.path.expanduser(target)
    if os.path.exists(target) and not args.force:
        print(f"{target} already exists; pass --force to overwrite")
        return 1
    config, notes = migrate(args.source, new_path=target)
    print(f"wrote {target}\n")
    print("Carried over:")
    print(f"  pictures     {', '.join(config.library.picture_folders)}")
    print(f"  interval     {config.slideshow.interval:g}s"
          f"  (transition {config.slideshow.transition}, "
          f"{config.slideshow.transition_time:g}s)")
    print(f"  fit          {config.viewer.fit}"
          + (f" / mat {config.viewer.mat_style}" if config.viewer.fit in ("auto", "mat") else ""))
    print(f"  captions     {', '.join(config.viewer.show_text) or 'none'}")
    print(f"  MQTT         {'on -> ' + config.mqtt.host if config.mqtt.enabled else 'off'}")
    print(f"  web          {'on, port ' + str(config.http.port) if config.http.enabled else 'off'}")
    if notes:
        print("\nNotes:")
        for note in notes:
            print(f"  · {note}")
    print("\nRun `picframe3 scan` to build the new index, then `picframe3 doctor`.")
    return 0


def cmd_transitions(args) -> int:
    from .gfx import transitions

    for name in transitions.names():
        print(name)
    return 0


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="picframe3",
        description="A digital picture frame for current Raspberry Pi OS.",
    )
    parser.add_argument("--version", action="version", version=f"picframe3 {__version__}")
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument("-v", "--log-level", help="DEBUG, INFO, WARNING, ERROR")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="start the frame (default)")
    run.add_argument("--backend", choices=("auto", "kms", "headless"))
    run.add_argument("--folder", action="append", help="override the picture folder")
    run.add_argument("--interval", type=float, help="seconds per picture")
    run.add_argument("--no-http", action="store_true", help="do not start the web interface")
    run.set_defaults(func=cmd_run)

    scan = sub.add_parser("scan", help="index the picture folders and exit")
    scan.add_argument("--folder", action="append")
    scan.add_argument("--no-prune", action="store_true",
                      help="keep index entries whose files have gone")
    scan.add_argument("--regeocode", action="store_true",
                      help="discard the place names already resolved and look them up again")
    scan.set_defaults(func=cmd_scan)

    doctor = sub.add_parser("doctor", help="check the installation and the hardware")
    doctor.set_defaults(func=cmd_doctor)

    conf = sub.add_parser("config", help="show or change settings")
    conf.add_argument("--init", action="store_true", help="write a default config file")
    conf.add_argument("--force", action="store_true")
    conf.add_argument("--get", metavar="KEY")
    conf.add_argument("--set", metavar="KEY=VALUE")
    conf.set_defaults(func=cmd_config)

    syncp = sub.add_parser("sync", help="Syncthing: status, on, off, folder")
    syncp.add_argument("action", nargs="?", default="status",
                       choices=["status", "on", "off", "folder"],
                       help="status (default), on, off, or folder to create "
                            "or re-align the frame's folder")
    syncp.set_defaults(func=cmd_sync)

    demo = sub.add_parser("demo", help="render sample frames to a PNG, offscreen")
    demo.add_argument("-o", "--output", default="picframe3-demo.png")
    demo.add_argument("--folder", help="folder of real photos to use")
    demo.add_argument("--width", type=int, default=960)
    demo.add_argument("--height", type=int, default=540)
    demo.add_argument("--transition", default="fade")
    demo.set_defaults(func=cmd_demo)

    wiz = sub.add_parser("setup", help="interactive first-run setup")
    wiz.add_argument("--yes", action="store_true",
                     help="take every default without asking")
    wiz.add_argument("--venv-bin", help="bin/ directory of the virtual environment, "
                                        "used in the systemd unit")
    wiz.set_defaults(func=cmd_setup)

    sub.add_parser("uninstall", help="stop and remove the systemd service") \
        .set_defaults(func=cmd_uninstall)

    mig = sub.add_parser("migrate", help="convert a picframe configuration.yaml")
    mig.add_argument("source", help="path to the old configuration.yaml")
    mig.add_argument("-o", "--output", help="where to write the new config")
    mig.add_argument("--force", action="store_true")
    mig.set_defaults(func=cmd_migrate)

    sub.add_parser("transitions", help="list the available transitions") \
        .set_defaults(func=cmd_transitions)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args((argv or []) + ["run"])
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
