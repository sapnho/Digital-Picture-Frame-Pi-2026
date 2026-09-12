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

import copy
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


class _ParsedSchedule:
    """One schedule dict, parsed once.

    The maintenance loop asks the schedule what it should be doing every
    second.  Parsing the dict on every one of those calls was not merely
    wasteful: a range the owner mistyped produced a warning per evaluation, so
    a single bad character in the config wrote 86 400 lines a day into the
    journal and, on a frame that logs to the card, eventually filled it.  The
    dict is small and changes almost never, so it is parsed when it changes and
    the result is kept -- and a parse failure is therefore reported once, when
    it happens, which is also when it is useful.
    """

    def __init__(self, schedule: dict | None):
        #: The dict as it was when this was built.  Compared (not merely
        #: identity-checked) on every use, so a schedule edited in place is
        #: noticed just as reliably as one replaced wholesale.
        self.source = copy.deepcopy(schedule or {})
        #: One entry per usable range, in the order the config wrote them:
        #: the weekday key it lives under (None for a range that is itself the
        #: key), the range, and the value it carries.  Source order is kept
        #: because the dimming rules are applied last-match-wins.
        self.rules: list[tuple[str | None, TimeRange, object]] = []
        self._parse()

    def _parse(self) -> None:
        day_keys = set(_DAYS) | {"all", "daily", "*", "weekday", "weekend"}
        for key, value in (self.source or {}).items():
            key_l = str(key).strip().lower()
            if key_l in day_keys:
                items = value if isinstance(value, (list, tuple)) else [value]
                for item in items:
                    rng = TimeRange.parse(str(item))
                    if rng is not None:
                        self.rules.append((key_l, rng, True))
            else:
                rng = TimeRange.parse(key_l)
                if rng is not None:
                    self.rules.append((None, rng, value))

    def matches(self, schedule: dict | None) -> bool:
        """Whether this parse is still the parse of ``schedule``."""
        return self.source == (schedule or {})

    def for_day(self, when: _dt.datetime) -> list[tuple[TimeRange, object]]:
        """The ranges that apply on ``when``'s weekday, already parsed."""
        day = _DAYS[when.weekday()]
        today = {"all", "daily", "*", day,
                 "weekday" if when.weekday() < 5 else "weekend"}
        return [(rng, value) for key, rng, value in self.rules
                if key is None or key in today]


class PowerSchedule:
    """When the screen is off, and how bright it is when it is on.

    Both schedules are parsed lazily and kept until the dict they came from
    actually changes, because this object is consulted once a second for the
    life of the frame and the answer only moves when the owner edits a setting.
    """

    def __init__(self, off_schedule: dict | None = None,
                 dim_schedule: dict | None = None):
        self.off_schedule = off_schedule or {}
        self.dim_schedule = dim_schedule or {}
        self._off_parsed: _ParsedSchedule | None = None
        self._dim_parsed: _ParsedSchedule | None = None

    def _parsed(self, which: str) -> _ParsedSchedule:
        """The parsed form of one schedule, rebuilt only when it has changed.

        The attributes are public and the config layer replaces them, so the
        cache is validated against the dict rather than trusted -- a stale
        parse would have the screen turning off at last week's times.
        """
        attribute = f"_{which}_parsed"
        schedule = getattr(self, f"{which}_schedule")
        parsed = getattr(self, attribute)
        if parsed is None or not parsed.matches(schedule):
            parsed = _ParsedSchedule(schedule)
            setattr(self, attribute, parsed)
        return parsed

    def display_should_be_on(self, when: _dt.datetime | None = None) -> bool:
        when = when or _dt.datetime.now()
        minutes = when.hour * 60 + when.minute
        for rng, _ in self._parsed("off").for_day(when):
            if rng.contains(minutes):
                return False
        return True

    def brightness(self, when: _dt.datetime | None = None,
                   default: float = 1.0) -> float:
        when = when or _dt.datetime.now()
        minutes = when.hour * 60 + when.minute
        value = default
        for rng, level in self._parsed("dim").for_day(when):
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
