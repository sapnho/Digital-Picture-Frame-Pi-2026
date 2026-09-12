"""What the Pi itself is doing: temperature, load, memory, disk, power.

A picture frame is a fanless computer in a warm room that nobody looks at
again once it is on the wall.  The two things that actually go wrong with one
are heat and a marginal power supply -- both of which announce themselves long
before the frame misbehaves, and neither of which is visible from the picture
on the screen.  So the frame measures itself and publishes the result
alongside everything else it already says.

Everything here is read from ``/sys`` and ``/proc``; the only subprocess is
``vcgencmd``, and only for the throttling flags, which have no sysfs
equivalent on older firmware.  Nothing is required: on a machine that is not a
Raspberry Pi every reading that cannot be taken is simply ``None``, and the
entities that would show it are not announced.

Sampling happens on a background thread, never in the render loop.  A frame
mid-transition must not stall for the 30-odd milliseconds ``vcgencmd`` takes to
answer, so :meth:`Health.snapshot` only ever returns the last reading and
starts a new one when that reading has gone stale.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Any

_log = logging.getLogger(__name__)

#: Where the SoC temperature lives.  ``thermal_zone0`` is the CPU on every
#: Raspberry Pi; the glob is for everything else.
_THERMAL = "/sys/class/thermal"

#: Bits of ``vcgencmd get_throttled``.  The low four are "right now", the same
#: four shifted into bits 16-19 are "has happened since boot" -- which is the
#: half that matters for a frame nobody watches: a brownout at 3am is invisible
#: by breakfast unless someone kept the flag.
_UNDERVOLTAGE_NOW = 0x1
_FREQ_CAPPED_NOW = 0x2
_THROTTLED_NOW = 0x4
_SOFT_TEMP_LIMIT_NOW = 0x8
_UNDERVOLTAGE_EVER = 0x10000
_THROTTLED_EVER = 0x40000


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return None


def cpu_temperature() -> float | None:
    """Degrees Celsius, or None where the kernel does not say."""
    raw = _read(f"{_THERMAL}/thermal_zone0/temp")
    if raw is None:
        # Not a Pi, or a kernel that numbers its zones differently.
        try:
            zones = sorted(z for z in os.listdir(_THERMAL) if z.startswith("thermal_zone"))
        except OSError:
            zones = []
        for zone in zones:
            raw = _read(f"{_THERMAL}/{zone}/temp")
            if raw:
                break
    if not raw:
        return _cpu_temperature_vcgencmd()
    try:
        value = float(raw)
    except ValueError:
        return None
    # Millidegrees on Linux; a handful of drivers report whole degrees.
    return round(value / 1000.0 if value > 200 else value, 1)


def _cpu_temperature_vcgencmd() -> float | None:
    out = _vcgencmd("measure_temp")           # "temp=48.3'C"
    if not out or "=" not in out:
        return None
    try:
        return round(float(out.split("=", 1)[1].rstrip("'C\n").rstrip("C").rstrip("'")), 1)
    except ValueError:
        return None


def _vcgencmd(*args: str) -> str | None:
    try:
        result = subprocess.run(["vcgencmd", *args], capture_output=True,
                                text=True, timeout=2.0)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def throttling() -> dict[str, bool] | None:
    """The Pi's power and thermal flags, or None if they cannot be read.

    ``vcgencmd`` is asked first because it is authoritative and present on
    every Raspberry Pi OS image.  The hwmon fallback covers a frame whose user
    is not in the ``video`` group: the kernel's ``rpi_volt`` driver exposes the
    undervoltage alarm to anyone.
    """
    raw = _vcgencmd("get_throttled")          # "throttled=0x0"
    if raw and "=" in raw:
        try:
            bits = int(raw.split("=", 1)[1], 16)
        except ValueError:
            bits = None
        if bits is not None:
            return {
                "undervoltage": bool(bits & _UNDERVOLTAGE_NOW),
                "frequency_capped": bool(bits & _FREQ_CAPPED_NOW),
                "throttled": bool(bits & _THROTTLED_NOW),
                "soft_temp_limit": bool(bits & _SOFT_TEMP_LIMIT_NOW),
                "undervoltage_since_boot": bool(bits & _UNDERVOLTAGE_EVER),
                "throttled_since_boot": bool(bits & _THROTTLED_EVER),
            }
    alarm = _rpi_volt_alarm()
    if alarm is None:
        return None
    return {"undervoltage": alarm, "undervoltage_since_boot": alarm}


def _rpi_volt_alarm() -> bool | None:
    base = "/sys/class/hwmon"
    try:
        entries = os.listdir(base)
    except OSError:
        return None
    for entry in entries:
        if _read(f"{base}/{entry}/name") != "rpi_volt":
            continue
        raw = _read(f"{base}/{entry}/in0_lcrit_alarm")
        if raw in ("0", "1"):
            return raw == "1"
    return None


def memory() -> dict[str, Any] | None:
    """Total and available bytes, and how much is in use as a percentage.

    *Available*, not *free*: Linux spends every spare page on cache, and a
    frame that has been up a week always looks full if you read ``MemFree``.
    """
    text = _read("/proc/meminfo")
    if not text:
        return None
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            try:
                values[key] = int(rest.strip().split()[0]) * 1024
            except (IndexError, ValueError):
                pass
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    if not total or available is None:
        return None
    return {
        "total": total,
        "available": available,
        "used_percent": round(100.0 * (total - available) / total, 1),
    }


def load() -> dict[str, Any] | None:
    """Run queue over one, five and fifteen minutes, and as a percentage.

    The percentage is the one-minute average divided by the number of cores,
    which is what someone means by "how busy is it" -- a load of 4 on the Pi
    5's four cores is 100 %, not 400 %.
    """
    try:
        one, five, fifteen = os.getloadavg()
    except (OSError, AttributeError):
        return None
    cores = os.cpu_count() or 1
    return {
        "load1": round(one, 2),
        "load5": round(five, 2),
        "load15": round(fifteen, 2),
        "cores": cores,
        "used_percent": round(min(100.0, 100.0 * one / cores), 1),
    }


def disk(path: str) -> dict[str, Any] | None:
    """Free space on the filesystem the photographs live on.

    Not the root filesystem: on a frame fed by a USB stick or a network share
    those are different disks, and the one that fills up is the one with the
    pictures on it.
    """
    target = os.path.expanduser(path or "~")
    while target and not os.path.exists(target):
        parent = os.path.dirname(target)
        if parent == target:
            break
        target = parent
    try:
        usage = shutil.disk_usage(target or "/")
    except OSError:
        return None
    return {
        "path": target,
        "total": usage.total,
        "free": usage.free,
        "used_percent": round(100.0 * (usage.total - usage.free) / usage.total, 1)
        if usage.total else 0.0,
    }


def host_uptime() -> float | None:
    """Seconds since the machine booted, as opposed to since the frame started."""
    raw = _read("/proc/uptime")
    if not raw:
        return None
    try:
        return round(float(raw.split()[0]), 1)
    except (IndexError, ValueError):
        return None


def sample(disk_path: str = "") -> dict[str, Any]:
    """One complete reading.  Anything unavailable is None and says so."""
    reading: dict[str, Any] = {
        "cpu_temp": cpu_temperature(),
        "host_uptime": host_uptime(),
        "at": time.time(),
    }
    for name, value in (("load", load()), ("memory", memory()),
                        ("disk", disk(disk_path)), ("power", throttling())):
        reading[name] = value
    # Flattened copies, because a Home Assistant template that has to reach
    # through two levels for a number is a template that silently returns
    # nothing the day a reading is missing.
    reading["cpu_percent"] = (reading["load"] or {}).get("used_percent")
    reading["memory_percent"] = (reading["memory"] or {}).get("used_percent")
    reading["disk_percent"] = (reading["disk"] or {}).get("used_percent")
    reading["disk_free"] = (reading["disk"] or {}).get("free")
    power = reading["power"] or {}
    reading["undervoltage"] = power.get("undervoltage")
    reading["undervoltage_since_boot"] = power.get("undervoltage_since_boot")
    reading["throttled"] = power.get("throttled")
    return reading


class Health:
    """The last reading, refreshed on a background thread when it goes stale.

    The frame asks for this from inside its render loop -- several times a
    second while a transition is running -- so the accessor does no I/O at all.
    """

    def __init__(self, interval: float = 30.0, disk_path: str = "",
                 enabled: bool = True):
        self.interval = max(5.0, float(interval))
        self.disk_path = disk_path
        self.enabled = enabled
        self._reading: dict[str, Any] = {}
        self._taken_at = 0.0
        self._lock = threading.Lock()
        self._busy = False

    def refresh(self) -> dict[str, Any]:
        """Take a reading now, on this thread.  Used at startup and by doctor."""
        reading = sample(self.disk_path)
        with self._lock:
            self._reading = reading
            self._taken_at = time.monotonic()
        return reading

    def snapshot(self) -> dict[str, Any]:
        """The last reading, and a new one on its way if that one is old."""
        if not self.enabled:
            return {}
        with self._lock:
            reading = self._reading
            age = time.monotonic() - self._taken_at
            due = age >= self.interval and not self._busy
            if due:
                self._busy = True
        if due:
            threading.Thread(target=self._refresh_in_background,
                             name="picframe3-health", daemon=True).start()
        return reading

    def _refresh_in_background(self) -> None:
        try:
            self.refresh()
        except Exception:                     # pragma: no cover - never fatal
            _log.debug("health sample failed", exc_info=True)
        finally:
            with self._lock:
                self._busy = False

    # -- what the readings mean -------------------------------------------
    @property
    def available(self) -> bool:
        """Whether this machine reports anything at all."""
        return bool(self._reading.get("cpu_temp") is not None
                    or self._reading.get("load"))
