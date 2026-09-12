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
    #: 0 or 180.  A quarter turn is done by the kernel instead
    #: (``video=HDMI-A-1:1080x1920M@60,rotate=90`` in cmdline.txt) so the Pi
    #: reports a portrait mode and every layer works in real pixels.
    rotate: int = 0
    vsync: bool = True
    #: Only applies while something is animating; a still picture draws nothing.
    fps_limit: float = 30.0
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


@dataclass
class ViewerConfig:
    fit: str = "auto"                     # auto | cover | contain | blur | mat
    blur_amount: float = 20.0
    blur_zoom: float = 1.06
    blur_dim: float = 0.55
    #: Never enlarge a small picture by more than this; beyond it the frame
    #: falls back to a blur-fill rather than showing a soft, stretched image.
    upscale_limit: float = 2.5
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
    text_justify: str = "L"
    text_opacity: float = 1.0
    text_margin_x: int = 64
    text_margin_y: int = 36
    text_scrim: float = 0.5
    #: Written between the caption elements.  " · " reads as one line of
    #: information; "\n" puts each element on its own line.
    text_separator: str = "  ·  "
    date_format: str = "%-d %B %Y"

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


@dataclass
class GeoConfig:
    enabled: bool = False
    contact: str = ""                     # required by the Nominatim usage policy
    language: str = "en"
    cache: str = "~/.local/share/picframe3/geocache.db3"
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


@dataclass
class HttpConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 9000
    auth_user: str = ""
    auth_password: str = ""
    allow_delete: bool = False
    cors_origins: list[str] = field(default_factory=list)


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
    geo: GeoConfig = field(default_factory=GeoConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    input: InputConfig = field(default_factory=InputConfig)
    power: PowerConfig = field(default_factory=PowerConfig)
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
        os.replace(tmp, target)            # atomic: never a half-written config
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
            setattr(node, leaf, value)
        elif isinstance(node, dict):
            node[leaf] = value
        else:
            raise KeyError(dotted)
        return value

    def copy(self) -> Config:
        return copy.deepcopy(self)


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
            setattr(section, key, _coerce(value, known.get(key), f"{prefix}.{key}"))
        except (TypeError, ValueError) as exc:
            _log.warning("bad value for %s.%s (%s); keeping the default",
                         prefix, key, exc)


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


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
            return list(value) if isinstance(value, (list, tuple, set)) else [value]
        if origin is dict:
            return dict(value) if isinstance(value, dict) else {}
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
    return value


