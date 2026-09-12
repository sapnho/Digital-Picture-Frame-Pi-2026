"""Syncthing: photographs that arrive by themselves.

A picture frame is only as good as the road the photographs take to it.  The
Samba share in :mod:`picframe3.wizard` is the short road -- drag a folder onto
the frame in Finder -- and it stops at the front door: it needs both machines
on the same network, it needs somebody to remember, and nothing on a phone
speaks it.  Syncthing is the other road.  It runs on the Pi, it pairs with a
phone, a Mac, a PC or a NAS once, and from then on a photograph taken on
Sunday is on the wall on Sunday, from anywhere.

What this module owns is the *frame's* side of that: is Syncthing installed,
is it running, which folder is it keeping in step, who is it paired with, and
the handful of changes the settings page is allowed to make.  Syncthing's own
web interface remains the place for everything else, and the frame links to it
rather than trying to reimplement it.

Three things need root and nothing else does -- installing the package,
enabling the service at boot, and stopping it again.  Those go through two
fixed systemd units that the setup installs (``picframe3-syncthing-on@`` and
``picframe3-syncthing-off@``) and a polkit rule that lets this one user start
those two and ``syncthing@`` itself, in the same narrow shape as the network
watchdog's rule.  The frame never runs a package manager itself, and the units
take no arguments beyond the user name, so "the web interface can switch
Syncthing on" cannot become "the web interface can run anything".

Everything else is Syncthing's REST API on localhost, authenticated with the
API key out of its own config file -- which is readable here because Syncthing
runs as the same user as the frame.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from xml.etree import ElementTree

_log = logging.getLogger(__name__)

#: The systemd template Debian and Raspberry Pi OS ship with the package.
SERVICE = "syncthing@{user}.service"

#: The two fixed units that do the parts needing root.  Installed by
#: ``picframe3 setup`` from ``packaging/``; see the module docstring.
ON_UNIT = "picframe3-syncthing-on@{user}.service"
OFF_UNIT = "picframe3-syncthing-off@{user}.service"

#: Where the helper script lands.  Named here because the unit files, the
#: installer and the uninstaller all have to agree about it.
HELPER = "/usr/local/lib/picframe3/syncthing-helper"

POLKIT_RULE = "/etc/polkit-1/rules.d/60-picframe3-syncthing.rules"
DROPIN = "/etc/systemd/system/syncthing@.service.d/picframe3.conf"

#: Syncthing moved its state out of ``~/.config`` in 1.27; both are still in
#: the field, so both are looked at, newest first.
CONFIG_PATHS = (
    "~/.local/state/syncthing/config.xml",
    "~/.config/syncthing/config.xml",
)

#: A Syncthing device ID: 8 groups of 7 characters, dashes optional, and the
#: alphabet is base32 without the digits that look like letters.
DEVICE_ID = re.compile(r"^[A-Z2-7]{7}(-?[A-Z2-7]{7}){7}$")

#: How long to wait for Syncthing's API after the service is started.  First
#: start writes a config file and generates a certificate, which on a Pi 4 is
#: a few seconds.
API_TIMEOUT = 30.0

FOLDER_TYPES = ("sendreceive", "receiveonly", "sendonly")


class SyncError(RuntimeError):
    """Something Syncthing-shaped went wrong, worded for the settings page."""


# --------------------------------------------------------------------------
# Where things are
# --------------------------------------------------------------------------

def user_name() -> str:
    """The user the frame -- and therefore Syncthing -- runs as."""
    return os.environ.get("SUDO_USER") or getpass.getuser()


def unit(user: str | None = None) -> str:
    return SERVICE.format(user=user or user_name())


def on_unit(user: str | None = None) -> str:
    return ON_UNIT.format(user=user or user_name())


def off_unit(user: str | None = None) -> str:
    return OFF_UNIT.format(user=user or user_name())


def installed() -> bool:
    return shutil.which("syncthing") is not None


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], text=True, capture_output=True)


def is_active(name: str) -> bool:
    if shutil.which("systemctl") is None:
        return False
    return _systemctl("is-active", "--quiet", name).returncode == 0


def is_enabled(name: str) -> bool:
    if shutil.which("systemctl") is None:
        return False
    return _systemctl("is-enabled", "--quiet", name).returncode == 0


def config_file() -> str:
    """Syncthing's own config file, or "" if it has never been started."""
    for candidate in (os.environ.get("SYNCTHING_HOME"), *CONFIG_PATHS):
        if not candidate:
            continue
        path = os.path.expanduser(candidate)
        if os.path.isdir(path):
            path = os.path.join(path, "config.xml")
        if os.path.exists(path):
            return path
    return ""


def local_api() -> tuple[str, str]:
    """``(base_url, api_key)`` read from Syncthing's config file.

    The address in the file is what Syncthing *listens* on, which may be
    ``0.0.0.0:8384``.  That is not a thing to connect to, so anything
    unspecified becomes 127.0.0.1: this module only ever talks to the copy of
    Syncthing on this machine.
    """
    path = config_file()
    if not path:
        return "", ""
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        _log.debug("cannot read %s: %s", path, exc)
        return "", ""
    gui = root.find("gui")
    if gui is None:
        return "", ""
    key = (gui.findtext("apikey") or "").strip()
    address = (gui.findtext("address") or "127.0.0.1:8384").strip()
    scheme = "https" if (gui.get("tls", "false") or "").lower() == "true" else "http"
    host, _, port = address.rpartition(":")
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    return f"{scheme}://{host}:{port or '8384'}", key


def listen_address(config) -> str:
    """What Syncthing's own web interface should listen on, per our settings."""
    port = int(getattr(config.sync, "gui_port", 8384) or 8384)
    return f"{'0.0.0.0' if config.sync.gui_lan else '127.0.0.1'}:{port}"


def folder_path(config) -> str:
    """The folder Syncthing keeps in step: the setting, or the first library."""
    chosen = (config.sync.folder_path or "").strip()
    if not chosen:
        folders = config.library.picture_folders or []
        chosen = folders[0] if folders else "~/Pictures"
    return os.path.expanduser(chosen)


# --------------------------------------------------------------------------
# Talking to it
# --------------------------------------------------------------------------

class Client:
    """The smallest possible REST client for the Syncthing on this machine."""

    def __init__(self, base: str = "", key: str = "", timeout: float = 5.0) -> None:
        if not base or not key:
            base, key = local_api()
        self.base = base.rstrip("/")
        self.key = key
        self.timeout = timeout

    def __bool__(self) -> bool:
        return bool(self.base and self.key)

    def request(self, path: str, method: str = "GET", body: Any = None) -> Any:
        if not self:
            raise SyncError("Syncthing has not written its configuration yet")
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"X-API-Key": self.key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()[:200]
            raise SyncError(f"Syncthing refused {method} {path}: "
                            f"{exc.code} {detail or exc.reason}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SyncError(f"Syncthing is not answering on {self.base}: "
                            f"{getattr(exc, 'reason', exc)}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode("utf-8", "replace")

    # Convenience wrappers, so call sites read as sentences.
    def get(self, path: str) -> Any:
        return self.request(path)

    def post(self, path: str, body: Any = None) -> Any:
        return self.request(path, "POST", body)

    def put(self, path: str, body: Any = None) -> Any:
        return self.request(path, "PUT", body)

    def delete(self, path: str) -> Any:
        return self.request(path, "DELETE")


def wait_for_api(timeout: float = API_TIMEOUT) -> Client:
    """Poll until Syncthing answers, because a fresh start is not instant."""
    import time

    deadline = time.monotonic() + timeout
    last = "Syncthing did not start"
    while time.monotonic() < deadline:
        client = Client()
        if client:
            try:
                client.get("/rest/system/status")
                return client
            except SyncError as exc:
                last = str(exc)
        time.sleep(1.0)
    raise SyncError(last)


# --------------------------------------------------------------------------
# What the settings page asks for
# --------------------------------------------------------------------------

def folder_payload(config, device_ids: list[str] | None = None) -> dict[str, Any]:
    """The folder object Syncthing is asked to keep, built from our settings.

    Deliberately explicit about versioning.  The frame's *Remove* button moves
    a photograph out of the picture folder, and on a two-way folder Syncthing
    reads that as a deletion and passes it on to the phone that sent it.  A
    trash can on the frame's side is what makes that recoverable rather than
    final, which is why it is on by default and why turning it off is a
    setting somebody has to go and change.
    """
    days = int(getattr(config.sync, "versioning_days", 0) or 0)
    versioning: dict[str, Any] = (
        {"type": "trashcan", "params": {"cleanoutDays": str(days)},
         "cleanupIntervalS": 3600}
        if days > 0 else {"type": "", "params": {}}
    )
    folder_type = config.sync.folder_type
    if folder_type not in FOLDER_TYPES:
        folder_type = "sendreceive"
    return {
        "id": config.sync.folder_id or "picframe3",
        "label": config.sync.folder_label or "Picture Frame",
        "path": folder_path(config),
        "type": folder_type,
        "devices": [{"deviceID": device} for device in (device_ids or [])],
        "versioning": versioning,
        # The frame indexes new files from inotify, and so does Syncthing;
        # an hourly walk behind it is the same belt-and-braces the library
        # keeps, for the times inotify misses something on a network mount.
        "fsWatcherEnabled": True,
        "rescanIntervalS": 3600,
        # A phone's photographs arrive with whatever permissions the phone
        # felt like; the frame only ever reads them.
        "ignorePerms": True,
    }


def ensure_folder(config, client: Client | None = None) -> dict[str, Any]:
    """Create the frame's folder in Syncthing, or bring it back into line.

    Devices already sharing the folder are kept: the folder is ours, the
    pairing is the owner's, and rewriting the folder must not quietly
    unshare it from the phone that has been feeding it for a year.
    """
    client = client or Client()
    folders = client.get("/rest/config/folders") or []
    wanted = config.sync.folder_id or "picframe3"
    existing = next((f for f in folders if f.get("id") == wanted), None)
    devices = [d.get("deviceID") for d in (existing or {}).get("devices", [])
               if d.get("deviceID")]
    # A folder always includes the machine it is on. Syncthing's own interface
    # puts it there when you create a folder by hand, and a folder created
    # without it is a folder this frame is not in.
    me = (client.get("/rest/system/status") or {}).get("myID", "")
    if me and me not in devices:
        devices.insert(0, me)
    payload = folder_payload(config, devices)
    if existing:
        merged = {**existing, **payload}
        client.put(f"/rest/config/folders/{wanted}", merged)
        return merged
    client.post("/rest/config/folders", payload)
    return payload


def ensure_gui(config, client: Client | None = None) -> str:
    """Make Syncthing's own page reachable (or not) as the settings ask.

    Syncthing binds to 127.0.0.1 out of the box, which on a headless Pi means
    a web interface nobody can open -- there is no browser on the frame.  This
    is the one piece of its configuration the frame insists on, because
    without it the "Open Syncthing" link on the settings page leads nowhere.
    """
    client = client or Client()
    gui = client.get("/rest/config/gui") or {}
    address = listen_address(config)
    if gui.get("address") == address:
        return address
    client.put("/rest/config/gui", {**gui, "address": address})
    return address


def add_device(device_id: str, name: str = "", config=None,
               client: Client | None = None) -> dict[str, Any]:
    """Pair with another machine and share the picture folder with it."""
    device_id = normalise_device_id(device_id)
    client = client or Client()
    devices = client.get("/rest/config/devices") or []
    known = next((d for d in devices if d.get("deviceID") == device_id), None)
    if known is None:
        client.post("/rest/config/devices", {
            "deviceID": device_id,
            "name": name or device_id[:7],
            "addresses": ["dynamic"],
            "autoAcceptFolders": False,
        })
    if config is not None:
        folder_id = config.sync.folder_id or "picframe3"
        folders = client.get("/rest/config/folders") or []
        folder = next((f for f in folders if f.get("id") == folder_id), None)
        if folder is None:
            folder = ensure_folder(config, client)
        shared = {d.get("deviceID") for d in folder.get("devices", [])}
        if device_id not in shared:
            folder = {**folder, "devices": [*folder.get("devices", []),
                                            {"deviceID": device_id}]}
            client.put(f"/rest/config/folders/{folder_id}", folder)
    return {"device_id": device_id, "name": name}


def remove_device(device_id: str, client: Client | None = None) -> None:
    client = client or Client()
    client.delete(f"/rest/config/devices/{normalise_device_id(device_id)}")


def normalise_device_id(value: str) -> str:
    """Uppercase, trimmed, and refused outright if it is not one of these.

    The settings page is open on the LAN, and this string is written into
    another program's configuration; "looks like a device ID" is the whole
    check that keeps it from being written into something else.
    """
    text = str(value or "").strip().upper().replace(" ", "")
    if not DEVICE_ID.match(text):
        raise SyncError("that is not a Syncthing device ID — it is eight "
                        "groups of seven letters and digits")
    return text


# --------------------------------------------------------------------------
# The picture the settings page draws
# --------------------------------------------------------------------------

def status(config) -> dict[str, Any]:
    """Everything the settings page shows, gathered defensively.

    Every call here can fail -- no systemd, no Syncthing, a service that is
    starting, a config file half written -- and none of those may raise into
    the web interface, which is why the shape of the answer never changes and
    the problem arrives as ``error``.
    """
    user = user_name()
    out: dict[str, Any] = {
        "configured": bool(config.sync.enabled),
        "installed": installed(),
        "running": False,
        "enabled_at_boot": False,
        "busy": False,
        "answering": False,
        "unit": unit(user),
        "folder_path": folder_path(config),
        "folder_id": config.sync.folder_id or "picframe3",
        "gui_port": int(getattr(config.sync, "gui_port", 8384) or 8384),
        "gui_local_only": not config.sync.gui_lan,
        "device_id": "",
        "device_name": "",
        "version": "",
        "folder": None,
        "devices": [],
        "pending": [],
        "error": "",
    }
    out["busy"] = is_active(on_unit(user)) or is_active(off_unit(user))
    if not out["installed"]:
        return out
    out["running"] = is_active(out["unit"])
    out["enabled_at_boot"] = is_enabled(out["unit"])
    client = Client()
    if not client:
        if out["running"]:
            out["error"] = "Syncthing is running but has not written its " \
                           "configuration yet; give it a moment."
        return out
    try:
        system = client.get("/rest/system/status") or {}
        out["answering"] = True
        out["device_id"] = system.get("myID", "")
        version = client.get("/rest/system/version") or {}
        out["version"] = version.get("version", "")
        configuration = client.get("/rest/config") or {}
        out["device_name"] = next(
            (d.get("name", "") for d in configuration.get("devices", [])
             if d.get("deviceID") == out["device_id"]), "")
        gui = configuration.get("gui") or {}
        address = str(gui.get("address") or "")
        out["gui_port"] = int(address.rpartition(":")[2] or out["gui_port"])
        out["gui_local_only"] = address.startswith(("127.", "localhost", "[::1]"))
        out["folder"] = _folder_state(client, configuration, out["folder_id"])
        out["devices"] = _device_states(client, configuration, out["device_id"],
                                        out["folder"])
        pending = client.get("/rest/cluster/pending/devices") or {}
        out["pending"] = [{"device_id": key,
                           "name": (value or {}).get("name", ""),
                           "address": (value or {}).get("address", "")}
                          for key, value in pending.items()]
    except SyncError as exc:
        out["error"] = str(exc)
    except Exception as exc:                       # pragma: no cover - defensive
        out["error"] = f"could not read Syncthing's state: {exc}"
    return out


def _folder_state(client: Client, configuration: dict, folder_id: str):
    folder = next((f for f in configuration.get("folders", [])
                   if f.get("id") == folder_id), None)
    if folder is None:
        return None
    state = {"id": folder_id, "label": folder.get("label", ""),
             "path": folder.get("path", ""), "type": folder.get("type", ""),
             "paused": bool(folder.get("paused")),
             "shared_with": [d.get("deviceID") for d in folder.get("devices", [])],
             "state": "", "files": 0, "bytes": 0, "need_bytes": 0}
    try:
        db = client.get("/rest/db/status?folder="
                        + urllib.parse.quote(folder_id, safe="")) or {}
    except SyncError:
        return state
    state["state"] = db.get("state", "")
    state["files"] = int(db.get("localFiles", 0) or 0)
    state["bytes"] = int(db.get("localBytes", 0) or 0)
    state["need_bytes"] = int(db.get("needBytes", 0) or 0)
    return state


def _device_states(client: Client, configuration: dict, me: str, folder) -> list[dict]:
    try:
        connections = (client.get("/rest/system/connections") or {}).get("connections", {})
    except SyncError:
        connections = {}
    shared = set((folder or {}).get("shared_with", []) or [])
    out = []
    for device in configuration.get("devices", []):
        device_id = device.get("deviceID", "")
        if not device_id or device_id == me:
            continue
        out.append({
            "device_id": device_id,
            "name": device.get("name", "") or device_id[:7],
            "connected": bool((connections.get(device_id) or {}).get("connected")),
            "shares_the_pictures": device_id in shared,
        })
    return out


# --------------------------------------------------------------------------
# The three things that need root
# --------------------------------------------------------------------------

def switch_on(user: str | None = None) -> None:
    """Install Syncthing if need be, and have it start with the Pi.

    Returns as soon as the unit has been *started*, not when it has finished:
    an apt install on a Pi is minutes, and the settings page polls.
    """
    _start_helper(on_unit(user), "switch Syncthing on")


def switch_off(user: str | None = None) -> None:
    _start_helper(off_unit(user), "switch Syncthing off")


def _start_helper(name: str, what: str) -> None:
    if shutil.which("systemctl") is None:
        raise SyncError(f"cannot {what}: this machine has no systemd")
    result = _systemctl("start", "--no-block", name)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        hint = detail[-1][:200] if detail else ""
        raise SyncError(
            f"cannot {what}: {hint or 'systemd refused'}. Run "
            f"'picframe3 setup' once on the frame itself — it installs the "
            f"permission this needs.")


def helper_busy(user: str | None = None) -> bool:
    return is_active(on_unit(user)) or is_active(off_unit(user))


def wait_for_helper(timeout: float = 900.0) -> None:
    """Wait out an install.  Minutes on a Pi, and it must not be hurried."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not helper_busy():
            return
        time.sleep(2.0)


# --------------------------------------------------------------------------
# Bringing Syncthing into line with the configuration
# --------------------------------------------------------------------------

def apply(config, *, switch: bool | None = None) -> None:
    """Make Syncthing match ``config.sync``.  Blocking: call it on a thread.

    ``switch`` is the one setting that cannot simply be written: True installs
    and starts, False stops.  Everything after it -- where its page listens,
    which folder it keeps -- is done over its own API, and is safe to repeat,
    so this is called on every relevant change and once at startup.
    """
    if switch is True:
        switch_on()
        wait_for_helper()
    elif switch is False:
        switch_off()
        return
    if not config.sync.enabled or not installed():
        return
    if not is_active(unit()):
        _log.info("syncthing is installed but not running; leaving it alone")
        return
    configure(config)


def configure(config, client: Client | None = None) -> None:
    """The two things the frame insists on: its folder, and a reachable page.

    Separate from :func:`apply` because the settings page's *Keep this folder
    in step* button means exactly this and nothing else -- no installing, no
    starting, and no quietly doing nothing because ``sync.enabled`` happens to
    be off.
    """
    client = client or wait_for_api()
    ensure_gui(config, client)
    ensure_folder(config, client)
    _log.info("syncthing keeps %s in step (%s)", folder_path(config),
              config.sync.folder_type)


def apply_async(config, *, switch: bool | None = None) -> None:
    """:func:`apply` on a thread, with its failures in the log rather than in
    the caller's face.  The settings page reads the outcome from
    ``/api/sync``, which is the honest place for it."""
    import threading

    def work() -> None:
        try:
            apply(config, switch=switch)
        except SyncError as exc:
            _log.warning("syncthing: %s", exc)
        except Exception:                          # pragma: no cover - defensive
            _log.exception("syncthing: could not apply the configuration")

    threading.Thread(target=work, name="syncthing", daemon=True).start()
