"""Display power and brightness scheduling.

Two separate jobs that ``picframe`` conflated:

* **power** -- the panel is actually off (DPMS), drawing almost nothing, at
  night or when nobody is in the room;
* **brightness** -- the picture is dimmed but still visible, which is what you
  usually want in the evening.

Schedules are expressed as human time ranges and are allowed to cross midnight,
because "22:30-07:00" is the obvious way to say it.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from dataclasses import dataclass

_log = logging.getLogger(__name__)

_RANGE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class TimeRange:
    start: int          # minutes since midnight
    end: int

    @classmethod
    def parse(cls, text: str) -> TimeRange | None:
        m = _RANGE.match(text)
        if not m:
            _log.warning("cannot parse time range %r; expected e.g. '22:30-07:00'", text)
            return None
        sh, sm, eh, em = (int(g) for g in m.groups())
        if not (0 <= sh < 24 and 0 <= eh < 24 and 0 <= sm < 60 and 0 <= em < 60):
            _log.warning("time range %r is out of bounds", text)
            return None
        return cls(sh * 60 + sm, eh * 60 + em)

    def contains(self, minutes: int) -> bool:
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= minutes < self.end
        return minutes >= self.start or minutes < self.end     # wraps midnight


def _ranges_for(schedule: dict, when: _dt.datetime) -> list[tuple[TimeRange, object]]:
    """Collect the ranges that apply to ``when``'s weekday.

    Accepts either ``{"all": ["22:00-07:00"]}`` (a list of ranges) or
    ``{"20:00-23:00": 0.4}`` (a range mapped to a value).
    """
    day = _DAYS[when.weekday()]
    today_aliases = {"all", "daily", "*", day,
                     "weekday" if when.weekday() < 5 else "weekend"}
    day_keys = set(_DAYS) | {"all", "daily", "*", "weekday", "weekend"}
    out: list[tuple[TimeRange, object]] = []
    for key, value in (schedule or {}).items():
        key_l = str(key).strip().lower()
        if key_l in day_keys:
            if key_l not in today_aliases:
                continue                      # a rule for some other day
            items = value if isinstance(value, (list, tuple)) else [value]
            for item in items:
                rng = TimeRange.parse(str(item))
                if rng:
                    out.append((rng, True))
        else:
            rng = TimeRange.parse(key_l)
            if rng is not None:
                out.append((rng, value))
    return out


class PowerSchedule:
    def __init__(self, off_schedule: dict | None = None,
                 dim_schedule: dict | None = None):
        self.off_schedule = off_schedule or {}
        self.dim_schedule = dim_schedule or {}

    def display_should_be_on(self, when: _dt.datetime | None = None) -> bool:
        when = when or _dt.datetime.now()
        minutes = when.hour * 60 + when.minute
        for rng, _ in _ranges_for(self.off_schedule, when):
            if rng.contains(minutes):
                return False
        return True

    def brightness(self, when: _dt.datetime | None = None,
                   default: float = 1.0) -> float:
        when = when or _dt.datetime.now()
        minutes = when.hour * 60 + when.minute
        value = default
        for rng, level in _ranges_for(self.dim_schedule, when):
            if rng.contains(minutes):
                try:
                    value = max(0.05, min(1.0, float(level)))
                except (TypeError, ValueError):
                    continue
        return value

    def describe(self) -> str:
        parts = []
        if self.off_schedule:
            parts.append(f"off {self.off_schedule}")
        if self.dim_schedule:
            parts.append(f"dim {self.dim_schedule}")
        return "; ".join(parts) or "always on"


class BacklightControl:
    """Hardware brightness for DSI panels, which have a sysfs backlight.

    HDMI monitors do not, so this quietly reports unsupported and the renderer
    dims in the shader instead.
    """

    SYSFS = "/sys/class/backlight"

    def __init__(self) -> None:
        import os

        self.device: str | None = None
        self.max_value = 255
        try:
            entries = sorted(os.listdir(self.SYSFS))
        except OSError:
            entries = []
        for name in entries:
            path = f"{self.SYSFS}/{name}"
            try:
                with open(f"{path}/max_brightness") as fh:
                    self.max_value = int(fh.read().strip())
                self.device = path
                break
            except OSError:
                continue

    @property
    def available(self) -> bool:
        return self.device is not None

    def set(self, fraction: float) -> bool:
        if not self.device:
            return False
        value = int(round(max(0.0, min(1.0, fraction)) * self.max_value))
        try:
            with open(f"{self.device}/brightness", "w") as fh:
                fh.write(str(value))
            return True
        except OSError as exc:
            _log.debug("cannot write backlight brightness: %s", exc)
            return False

    def get(self) -> float | None:
        if not self.device:
            return None
        try:
            with open(f"{self.device}/brightness") as fh:
                return int(fh.read().strip()) / self.max_value
        except OSError:
            return None
