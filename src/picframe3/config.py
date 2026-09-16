"""Configuration: typed dataclasses loaded from one YAML file.

Design notes:

* Every setting has a default, so a missing or half-written config file still
  produces a frame that runs.  ``picframe`` required a complete
  ``configuration.yaml`` and raised ``KeyError`` deep inside the viewer when a
  key was absent -- a bad failure mode for an appliance.
* Unknown keys are reported as warnings, not errors: a config written for a
  newer version still boots.
* Settings are addressable by dotted path (``slideshow.interval``) so MQTT and
  the web UI can read and write them through one code path, with validation.
"""

from __future__ import annotations

import copy
import functools
import logging
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any, get_args, get_origin

_log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATHS = (
    "~/.config/picframe3/config.yaml",
    "/etc/picframe3/config.yaml",
)


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

@dataclass
class DisplayConfig:
    backend: str = "auto"                 # auto | kms | headless
    device: str | None = None          # /dev/dri/card1; None = autodetect
    connector: str | None = None       # HDMI-A-1; None = first connected
    #: Which mode to set, as ``1920x1080`` or ``3840x2160@30``.  Empty takes
    #: the connector's preferred mode, which is the right answer for almost
    #: every panel.  It is not the right answer when the preferred mode is one
    #: the Pi cannot actually drive -- a 4K television asks for 2160p60, which
    #: needs ``hdmi_enable_4kp60`` on a Pi 4 and is beyond the Pi 4's pixel
    #: clock without it -- or when a panel offers several and lies about which
    #: it likes.  A mode the connector does not list is refused with the list
    #: of the ones it does, rather than rendering somewhere nobody can see.
    mode: str = ""
    #: 0 or 180.  A quarter turn is done by the kernel instead
    #: (``video=HDMI-A-1:1080x1920M@60,rotate=90`` in cmdline.txt) so the Pi
    #: reports a portrait mode and every layer works in real pixels.
    rotate: int = 0
    vsync: bool = True
    #: Only applies while something is animating; a still picture draws nothing.
    fps_limit: float = 60.0
    background: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 1.0])
    brightness: float = 1.0


@dataclass
class SlideshowConfig:
    interval: float = 180.0               # seconds a picture stays up
    transition: str = "fade"              # or "random"
    transition_time: float = 2.5
    order: str = "shuffle"
    recent_days: int = 7
    reshuffle_after: int = 1
    portrait_pairs: bool = False
    shuffle: bool = True                  # kept for compatibility with picframe
    paused: bool = False
    video_loop: bool = False
    video_mute: bool = True
    video_max_seconds: float = 0.0        # 0 = play to the end
    kenburns: bool = False
    kenburns_zoom: float = 1.12
    #: Which transitions ``transition: random`` may draw from.  Empty means the
    #: standard pool; name a few to keep only the ones you like.
    transition_choices: list[str] = field(default_factory=list)


@dataclass
class ViewerConfig:
    fit: str = "auto"                     # auto | cover | contain | blur | mat
    #: What ``fit: auto`` may do with a picture whose shape does not match the
    #: panel.  One name treats every such picture the same; several means "pick
    #: one of these", chosen from the file path so a given photograph always
    #: looks the same.  A picture that already matches the panel is shown edge
    #: to edge whatever is listed here, because that crops nothing.
    fit_choices: list[str] = field(default_factory=lambda: ["mat"])
    blur_amount: float = 20.0
    blur_zoom: float = 1.06
    blur_dim: float = 0.55
    #: Never enlarge a small picture by more than this; beyond it the frame
    #: falls back to a blur-fill rather than showing a soft, stretched image.
    upscale_limit: float = 2.5
    #: Largest picture the frame will decode, in megapixels.  A JPEG or a HEIF
    #: is scaled down as it is decoded and almost never reaches this; it is the
    #: formats that cannot -- PNG, TIFF, BMP -- where a single huge scan would
    #: otherwise take the whole frame down with it.  0 turns the limit off.
    max_decode_megapixels: float = 64.0
    #: What happens to a picture past that limit: on, the frame opens it once
    #: in a child process and keeps a panel-sized copy, so the photograph is
    #: shown from then on and the original is never touched again.  Off leaves
    #: it out of the slideshow instead.
    shrink_oversized: bool = True
    mat_style: str = "single"             # or "random", or "" for all
    mat_tolerance: float = 0.01
    mat_outer_color: list[int] | None = None
    mat_inner_color: list[int] | None = None
    mat_outer_border: int = 75
    mat_inner_border: int = 22
    mat_texture: bool = True
    #: The inner mat is usually smooth card against the textured outer board.
    mat_inner_texture: bool = False
    #: False makes the inner mat a darker shade of the outer instead of its
    #: own colour taken from the photograph.
    mat_auto_inner_color: bool = True
    mat_bevel_width: int = 5

    font: str | None = None
    show_text: list[str] = field(
        default_factory=lambda: ["title", "caption", "date", "location"]
    )
    text_size: int = 34
    text_seconds: float = 16.0
    #: How long the caption stays up when it is *asked* for -- the Home
    #: Assistant button, the ``i`` key -- rather than the few seconds it gets
    #: by itself after each change.  A deliberate look wants longer than a
    #: glance, and picframe had one number for both: making the reveal last
    #: meant every slide's caption lasting too.  Setting ``text_seconds: 0``
    #: and leaving this long is the "captions only when I ask" frame.
    peek_seconds: float = 40.0
    text_justify: str = "L"
    text_opacity: float = 1.0
    text_margin_x: int = 64
    text_margin_y: int = 36
    text_scrim: float = 0.5
    #: Written between the caption elements.  " · " reads as one line of
    #: information; "\n" puts each element on its own line.
    text_separator: str = "  ·  "
    date_format: str = "%-d %B %Y"
    #: Which language month and day names come out in: ``de_DE.UTF-8``,
    #: ``fr_FR.UTF-8``, ``en_GB.UTF-8``.  Empty leaves the process locale
    #: alone.  This is picframe's ``model.locale``.
    locale: str = ""

    show_clock: bool = False
    clock_format: str = "%H:%M"
    clock_size: int = 120
    clock_position: str = "TR"            # TL TC TR BL BC BR
    clock_opacity: float = 0.9
    clock_offset_pct: list[float] = field(default_factory=lambda: [3.0, 3.0])
    #: If this file exists its contents are shown under the time -- the same
    #: hook picframe had at /dev/shm/clock.txt, for a weather line or similar.
    clock_extra_file: str = "/dev/shm/picframe-clock.txt"

    overlay_image: str = "/dev/shm/picframe-overlay.png"

    #: Shown when there is nothing to show.  Empty is the picture that ships
    #: with the frame; a path is your own; "none" draws a plain screen with the
    #: folders it looked in.  picframe called this model.no_files_img.
    no_files_img: str = ""


@dataclass
class LibraryConfig:
    picture_folders: list[str] = field(default_factory=lambda: ["~/Pictures"])
    database: str = "~/.local/share/picframe3/library.db3"
    follow_links: bool = False
    include_videos: bool = True
    ignore_hidden: bool = True
    exclude: list[str] = field(default_factory=lambda: ["@eaDir", ".thumbnails", "#recycle"])
    watch: bool = True                    # inotify
    rescan_interval: float = 3600.0       # belt and braces behind inotify
    scan_on_start: bool = True
    deleted_folder: str = "~/.local/share/picframe3/deleted"
    subfolder: str = ""                   # live filter, changeable at runtime
    #: The largest share of the index a single scan may delete.  A picture
    #: folder on a USB stick or a network share is simply *absent* for a
    #: moment now and then, and every file under it then looks deleted; without
    #: this guard one unlucky scan throws away the play history, the hidden
    #: flags and every geocoded place name.  0 switches the guard off.
    prune_max_fraction: float = 0.2


@dataclass
class SyncConfig:
    """Syncthing: photographs that arrive by themselves.

    The frame owns very little of this.  Which folder is kept in step, who it
    is paired with and where Syncthing's own page listens are all settings
    Syncthing keeps; these are the handful the frame has an opinion about, so
    that a fresh install can be set up without ever opening Syncthing itself.
    """

    #: Whether this frame runs Syncthing at all.  Switching it on installs the
    #: package if it is missing and starts it with the Pi; switching it off
    #: stops it and leaves everything -- folder, pairings, photographs -- in
    #: place.
    enabled: bool = False
    #: The folder Syncthing keeps in step.  Empty means the first picture
    #: folder, which is what anyone actually wants.
    folder_path: str = ""
    #: Its name on the other machines, and the internal id the two sides agree
    #: on.  The id is awkward to change afterwards, so it has a dull default.
    folder_label: str = "Picture Frame"
    folder_id: str = "picframe3-pictures"
    #: ``sendreceive`` is two-way: photographs arrive, and anything the frame
    #: does to them travels back.  ``receiveonly`` takes photographs and never
    #: sends a change of its own.
    folder_type: str = "sendreceive"
    #: Days Syncthing keeps a copy of anything deleted or overwritten in that
    #: folder.  0 switches the trash can off.
    versioning_days: int = 30
    #: Where Syncthing's own web interface listens.  It binds to localhost out
    #: of the box, which on a frame with no browser means nobody can open it.
    gui_lan: bool = True
    gui_port: int = 8384


@dataclass
class GeoConfig:
    enabled: bool = False
    contact: str = ""                     # required by the Nominatim usage policy
    language: str = "en"
    cache: str = "~/.local/share/picframe3/geocache.db3"
    #: How much of the address to write. A preset name, or "custom" to use
    #: the key_order list below verbatim.
    detail: str = "full"
    #: Names never to show — your own country, say, or a county nobody uses.
    suppress: list[str] = field(default_factory=list)
    key_order: list[list[str]] = field(default_factory=lambda: [
        ["tourism", "attraction", "amenity", "isolated_dwelling"],
        ["neighbourhood", "suburb", "village", "town"],
        ["city", "municipality", "county"],
        ["state", "province", "region"],
        ["country"],
    ])


@dataclass
class MqttConfig:
    enabled: bool = False
    host: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""
    tls_ca: str = ""
    tls_insecure: bool = False
    device_id: str = "picframe"
    device_name: str = "Picture Frame"
    discovery_prefix: str = "homeassistant"
    topic_prefix: str = "picframe"
    publish_interval: float = 30.0
    publish_image: bool = True
    image_width: int = 1280
    image_quality: int = 82


@dataclass
class HttpConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 9000
    auth_user: str = ""
    auth_password: str = ""
    #: Whether the Remove button may work from the web interface and from
    #: MQTT.  On by default, because removing a picture from the frame is one
    #: of the things the frame is for; turning it off leaves removal to the
    #: keyboard and the GPIO buttons, which are in the room.
    allow_delete: bool = True
    cors_origins: list[str] = field(default_factory=list)
    #: Extra names the frame will answer to, beyond loopback, the private
    #: address ranges and its own hostname.  Only needed behind a reverse
    #: proxy or on a router with an unusual local domain.
    allowed_hosts: list[str] = field(default_factory=list)


@dataclass
class InputConfig:
    keyboard: bool = True
    touch: bool = True
    mouse: bool = False
    gpio_buttons: dict = field(default_factory=dict)   # {"next": 17, "pause": 27}
    gpio_pull_up: bool = True
    wake_on_input: bool = True
    keymap: dict = field(default_factory=lambda: {
        "next": ["KEY_RIGHT", "KEY_SPACE", "KEY_DOWN"],
        "previous": ["KEY_LEFT", "KEY_UP"],
        "pause": ["KEY_P"],
        "display_toggle": ["KEY_O"],
        "info_toggle": ["KEY_I"],
        "clock_toggle": ["KEY_C"],
        "delete": ["KEY_DELETE"],
        "quit": ["KEY_ESC", "KEY_Q"],
    })


@dataclass
class PowerConfig:
    #: Blank the screen on this weekly schedule.  "22:30-07:00" style ranges,
    #: per weekday name or "all".
    schedule: dict = field(default_factory=dict)
    dim_schedule: dict = field(default_factory=dict)   # {"21:00-23:00": 0.4}
    #: Named ``enabled`` rather than ``on`` on purpose: YAML 1.1 reads a bare
    #: ``on:`` key as the boolean True, which silently loses the setting.
    enabled: bool = True


@dataclass
class HealthConfig:
    """What the frame reports about the Pi it is running on."""

    #: Off removes the diagnostic sensors from Home Assistant and the line from
    #: the web interface; nothing is measured either.
    enabled: bool = True
    #: Seconds between readings.  Taken on a background thread, never in the
    #: render loop.
    interval: float = 30.0
    #: Which filesystem the free-space reading is about.  Empty means the first
    #: picture folder, which is the disk that actually fills up.
    disk_path: str = ""


@dataclass
class NetworkConfig:
    """Watching the frame's own link to the house, and mending it."""

    #: Off means nothing is pinged and nothing is ever restarted.
    enabled: bool = True
    #: What has to answer for the network to count as up.  Empty means the
    #: default gateway, read fresh from the routing table on every check --
    #: which is the right target in any house and needs no configuration.
    #: Never make this an address on the internet: a frame that restarts its
    #: Wi-Fi because a far-away server dropped a packet is worse than no
    #: watchdog at all.
    target: str = ""
    #: Which interface to reconnect.  Empty means the one the default route
    #: goes through.
    interface: str = ""
    #: Seconds between checks.
    interval: float = 60.0
    #: Ping runs, each of several packets, before one check counts as failed.
    attempts: int = 3
    #: Seconds to wait for a reply.
    timeout: float = 3.0
    #: Failed checks in a row before anything is repaired.  Three, an interval
    #: apart, is several minutes of genuine silence -- not a lost packet.
    failures: int = 3
    #: False watches and reports but never touches the connection.
    repair: bool = True
    #: Never repair more often than this.  The single most important setting
    #: here: without it a repair that makes the next check fail becomes a loop.
    cooldown: float = 1800.0
    #: Seconds to let the link settle after a repair before checking again.
    settle: float = 45.0


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = ""
    journald: bool = True


@dataclass
class Config:
    display: DisplayConfig = field(default_factory=DisplayConfig)
    slideshow: SlideshowConfig = field(default_factory=SlideshowConfig)
    viewer: ViewerConfig = field(default_factory=ViewerConfig)
    library: LibraryConfig = field(default_factory=LibraryConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    geo: GeoConfig = field(default_factory=GeoConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    input: InputConfig = field(default_factory=InputConfig)
    power: PowerConfig = field(default_factory=PowerConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    #: Where this config was loaded from, for ``save()``.
    source_path: str | None = None

    # -- loading -----------------------------------------------------------
    @classmethod
    def load(cls, path: str | None = None) -> Config:
        candidates = [path] if path else list(DEFAULT_CONFIG_PATHS)
        for candidate in candidates:
            if not candidate:
                continue
            full = os.path.expanduser(candidate)
            if os.path.exists(full):
                return cls.from_file(full)
        if path:
            raise FileNotFoundError(f"config file not found: {path}")
        _log.info("no config file found; using built-in defaults")
        cfg = cls()
        cfg.source_path = os.path.expanduser(DEFAULT_CONFIG_PATHS[0])
        return cfg

    @classmethod
    def from_file(cls, path: str) -> Config:
        import yaml

        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: top level must be a mapping")
        cfg = cls.from_dict(data)
        cfg.source_path = path
        _log.info("loaded configuration from %s", path)
        return cfg

    @classmethod
    def from_dict(cls, data: dict) -> Config:
        cfg = cls()
        for section_field in fields(cls):
            if section_field.name == "source_path":
                continue
            section = getattr(cfg, section_field.name)
            values = data.get(section_field.name)
            if values is None:
                continue
            if not isinstance(values, dict):
                _log.warning("config section %r should be a mapping; ignoring",
                             section_field.name)
                continue
            _apply(section, values, section_field.name)
        unknown = set(data) - {f.name for f in fields(cls)}
        for name in sorted(unknown):
            _log.warning("ignoring unknown config section %r", name)
        return cfg

    # -- saving ------------------------------------------------------------
    def as_dict(self) -> dict:
        out = {f.name: asdict(getattr(self, f.name))
               for f in fields(self) if f.name != "source_path"}
        return out

    def save(self, path: str | None = None) -> str:
        import yaml

        target = os.path.expanduser(path or self.source_path or DEFAULT_CONFIG_PATHS[0])
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self.as_dict(), fh, sort_keys=False, allow_unicode=True,
                           default_flow_style=False)
            # os.replace is atomic against a *reader*, but says nothing about
            # power.  A picture frame lives on a wall socket, so losing power
            # between the write and the flush is the normal case rather than
            # the exotic one, and ext4's delayed allocation would leave a
            # zero-byte config behind.  Flush the file, then the directory
            # entry that now points at it.
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)            # atomic: never a half-written config
        try:
            dir_fd = os.open(os.path.dirname(target) or ".", os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:                    # pragma: no cover - not all filesystems
            pass
        self.source_path = target
        _log.info("configuration written to %s", target)
        return target

    # -- dotted access -----------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self
        for part in dotted.split("."):
            if is_dataclass(node) and hasattr(node, part):
                node = getattr(node, part)
            elif isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, dotted: str, value: Any) -> Any:
        """Set one setting, coercing the value to the declared type."""
        parts = dotted.split(".")
        node: Any = self
        for part in parts[:-1]:
            if is_dataclass(node) and hasattr(node, part):
                node = getattr(node, part)
            elif isinstance(node, dict):
                node = node.setdefault(part, {})
            else:
                raise KeyError(dotted)
        leaf = parts[-1]
        if is_dataclass(node):
            if leaf not in {f.name for f in fields(node)}:
                raise KeyError(dotted)
            value = _coerce(value, _hints(type(node)).get(leaf), dotted)
            value = _vet(dotted, value)
            setattr(node, leaf, value)
        elif isinstance(node, dict):
            node[leaf] = value
        else:
            raise KeyError(dotted)
        return value

    def copy(self) -> Config:
        return copy.deepcopy(self)


# --------------------------------------------------------------------------
# Locale
# --------------------------------------------------------------------------

def set_time_locale(name: str) -> bool:
    """Make ``%B``, ``%b`` and ``%A`` come out in the owner's language.

    A frame writes one date per picture, so "7 September 2026" on a German
    wall is the kind of small wrongness that never stops being noticed.
    picframe had ``model.locale``; picframe3 inherited the process locale
    instead, which under systemd on Raspberry Pi OS Lite is ``C`` -- English,
    whatever ``raspi-config`` was told, because nothing exports ``LANG`` to a
    service.

    Returns whether the locale is now in force.  A locale the system has not
    generated cannot be set, and saying so beats leaving the dates in English
    with no explanation -- ``doctor`` reports it too.
    """
    if not name:
        return True

    import locale as locale_module

    tried: list[str] = []
    for candidate in (name, name.replace("-", "_"),
                      f"{name}.UTF-8", f"{name}.utf8"):
        if candidate in tried:
            continue
        tried.append(candidate)
        try:
            locale_module.setlocale(locale_module.LC_TIME, candidate)
            _log.info("date and time names in %s", candidate)
            return True
        except locale_module.Error:
            continue
    _log.warning(
        "locale %r is not available on this system, so dates stay in the "
        "default language; build it with 'picframe3 setup --yes' (or add it "
        "to /etc/locale.gen and run 'sudo locale-gen')", name,
    )
    return False


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

@functools.cache
def _hints(cls: type) -> dict[str, Any]:
    """Resolved type hints.  ``from __future__ import annotations`` makes every
    annotation a string, so they have to be evaluated once, here."""
    import typing

    try:
        return typing.get_type_hints(cls)
    except Exception:  # pragma: no cover - exotic forward refs
        return {f.name: f.type for f in fields(cls)}


def _apply(section: Any, values: dict, prefix: str) -> None:
    known = _hints(type(section))
    valid = {f.name for f in fields(section)}
    for key, value in values.items():
        if key not in valid:
            _log.warning("ignoring unknown config key %s.%s", prefix, key)
            continue
        try:
            dotted = f"{prefix}.{key}"
            setattr(section, key, _vet(dotted, _coerce(value, known.get(key), dotted)))
        except (TypeError, ValueError) as exc:
            _log.warning("bad value for %s.%s (%s); keeping the default",
                         prefix, key, exc)


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _vet(dotted: str, value: Any) -> Any:
    """Reject a value the frame cannot draw, and clamp one it can only survive.

    Type coercion alone is not enough.  ``viewer.clock_position = ""`` is a
    perfectly good string and puts an ``IndexError`` in the middle of every
    frame; ``slideshow.interval = 0`` is a perfectly good float and turns the
    render loop into a busy loop that decodes twenty pictures a second.  Both
    arrive the same way -- one MQTT message, one line in a hand-edited file --
    so both are stopped here, at the one place every setting passes through.

    Choices raise (the caller reports "that is not a value for this"); ranges
    clamp with a log line, because "3600 seconds is the most I will wait" is
    more useful to somebody than a refusal.
    """
    from . import uischema

    allowed = uischema.CHOICES.get(dotted)
    if allowed and isinstance(value, str):
        match = {c.casefold(): c for c in allowed}.get(value.strip().casefold())
        if match is None:
            raise ValueError(f"{value!r} is not one of {', '.join(allowed)}")
        return match
    limits = uischema.LIMITS.get(dotted)
    if limits and isinstance(value, (int, float)) and not isinstance(value, bool):
        low, high = limits
        clamped = value
        if low is not None:
            clamped = max(low, clamped)
        if high is not None:
            clamped = min(high, clamped)
        if clamped != value:
            _log.warning("%s: %r is outside %s..%s; using %r",
                         dotted, value, low, high, clamped)
        return type(value)(clamped)
    return value


def _coerce(value: Any, annotation: Any, dotted: str) -> Any:
    """Best-effort conversion, tolerant of YAML's and MQTT's loose typing."""
    if value is None or annotation is None:
        return value
    origin = get_origin(annotation)
    if origin is not None:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if value is None:
            return None
        if origin in (list, set, tuple):
            items = list(value) if isinstance(value, (list, tuple, set)) else [value]
            # The elements matter as much as the container.  A colour typed
            # into a text box arrives as ["0", "0", "0", "1"], and four
            # strings in a list of floats do not fail here -- they fail much
            # later, inside the renderer or inside Pillow, with an error that
            # names neither the setting nor the value.
            inner = get_args(annotation)
            if inner and inner[0] is not Any:
                return [_coerce(item, inner[0], dotted) for item in items]
            return items
        if origin is dict:
            if isinstance(value, dict):
                return dict(value)
            # Silently turning a malformed schedule into {} deletes the
            # schedule and says nothing; the caller keeps the old value and
            # logs instead.
            raise ValueError(f"{value!r} is not a mapping")
        if len(args) == 1:
            return _coerce(value, args[0], dotted)
        return value
    if annotation is Any:
        return value
    if annotation is bool:
        if isinstance(value, str):
            low = value.strip().lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ValueError(f"{value!r} is not a boolean")
        return bool(value)
    if annotation is int:
        return int(float(value))
    if annotation is float:
        return float(value)
    if annotation is str:
        return str(value)
    # A bare `dict` or `list` annotation -- power.schedule, input.keymap --
    # reaches here with no origin to inspect.  Letting anything through meant a
    # mistyped schedule was *stored* as a string and then quietly ignored by
    # everything that expected a mapping.
    if annotation is dict and not isinstance(value, dict):
        raise ValueError(f"{value!r} is not a mapping")
    if annotation is list and not isinstance(value, (list, tuple)):
        raise ValueError(f"{value!r} is not a list")
    return value


