"""Reading the Pi's own vital signs.

Every reading here comes from a file or a command that may be missing, report
a different unit than expected, or need a group membership the frame does not
have.  The rule the tests hold is that none of that may ever raise: a machine
that cannot say how hot it is returns None and loses one sensor, it does not
take the picture frame down with it.
"""

import threading
import time

import pytest

from picframe3 import health

# -- temperature -----------------------------------------------------------

def test_millidegrees_become_degrees(monkeypatch):
    monkeypatch.setattr(health, "_read", lambda path: "48123")
    assert health.cpu_temperature() == 48.1


def test_a_driver_reporting_whole_degrees_is_not_divided(monkeypatch):
    """A handful of thermal drivers report °C, not m°C. 48 °C is plausible;
    0.048 °C is not, which is what makes this safe to tell apart."""
    monkeypatch.setattr(health, "_read", lambda path: "48")
    assert health.cpu_temperature() == 48.0


def test_no_thermal_zone_and_no_vcgencmd_is_not_an_error(monkeypatch):
    monkeypatch.setattr(health, "_read", lambda path: None)
    monkeypatch.setattr(health, "_vcgencmd", lambda *args: None)
    monkeypatch.setattr(health.os, "listdir", lambda path: [])
    assert health.cpu_temperature() is None


def test_vcgencmd_is_the_fallback(monkeypatch):
    monkeypatch.setattr(health, "_read", lambda path: None)
    monkeypatch.setattr(health.os, "listdir", lambda path: [])
    monkeypatch.setattr(health, "_vcgencmd",
                        lambda *args: "temp=61.2'C" if args == ("measure_temp",) else None)
    assert health.cpu_temperature() == 61.2


# -- the power supply ------------------------------------------------------

def test_the_throttling_bits_are_read_the_way_the_firmware_means_them(monkeypatch):
    # 0x50005: undervoltage and throttling, both now and since boot.
    monkeypatch.setattr(health, "_vcgencmd", lambda *args: "throttled=0x50005")
    flags = health.throttling()
    assert flags == {
        "undervoltage": True, "frequency_capped": False, "throttled": True,
        "soft_temp_limit": False, "undervoltage_since_boot": True,
        "throttled_since_boot": True,
    }


def test_a_healthy_supply_reports_every_flag_clear(monkeypatch):
    monkeypatch.setattr(health, "_vcgencmd", lambda *args: "throttled=0x0")
    assert not any(health.throttling().values())


def test_the_hwmon_alarm_covers_a_frame_without_vcgencmd(monkeypatch):
    """`vcgencmd` needs the video group; the kernel's rpi_volt driver does not."""
    monkeypatch.setattr(health, "_vcgencmd", lambda *args: None)
    monkeypatch.setattr(health.os, "listdir", lambda path: ["hwmon0", "hwmon1"])
    monkeypatch.setattr(health, "_read", lambda path: {
        "/sys/class/hwmon/hwmon0/name": "cpu_thermal",
        "/sys/class/hwmon/hwmon1/name": "rpi_volt",
        "/sys/class/hwmon/hwmon1/in0_lcrit_alarm": "1",
    }.get(path))
    assert health.throttling() == {"undervoltage": True,
                                   "undervoltage_since_boot": True}


def test_neither_source_present_says_so_rather_than_claiming_health(monkeypatch):
    monkeypatch.setattr(health, "_vcgencmd", lambda *args: None)
    monkeypatch.setattr(health.os, "listdir", lambda path: [])
    assert health.throttling() is None


# -- memory, load, disk ----------------------------------------------------

MEMINFO = """MemTotal:        8242524 kB
MemFree:          204196 kB
MemAvailable:    6210964 kB
Buffers:          131072 kB
"""


def test_memory_uses_available_not_free(monkeypatch):
    """Linux spends every spare page on cache; a frame up for a week always
    looks full if you read MemFree."""
    monkeypatch.setattr(health, "_read", lambda path: MEMINFO)
    memory = health.memory()
    assert memory["total"] == 8242524 * 1024
    assert memory["available"] == 6210964 * 1024
    assert memory["used_percent"] == pytest.approx(24.6, abs=0.1)


def test_load_is_a_percentage_of_the_cores_there_are(monkeypatch):
    monkeypatch.setattr(health.os, "getloadavg", lambda: (2.0, 1.0, 0.5))
    monkeypatch.setattr(health.os, "cpu_count", lambda: 4)
    assert health.load()["used_percent"] == 50.0


def test_load_never_exceeds_a_hundred_percent(monkeypatch):
    monkeypatch.setattr(health.os, "getloadavg", lambda: (40.0, 1.0, 0.5))
    monkeypatch.setattr(health.os, "cpu_count", lambda: 4)
    assert health.load()["used_percent"] == 100.0


def test_free_space_is_reported_for_a_folder_that_does_not_exist_yet(tmp_path):
    """The picture folder may be an empty mount point at first run; the
    filesystem above it is still the honest answer."""
    reading = health.disk(str(tmp_path / "pictures" / "not-yet"))
    assert reading["total"] > 0
    assert reading["path"] == str(tmp_path)


def test_an_unreadable_path_is_not_an_exception(monkeypatch):
    monkeypatch.setattr(health.shutil, "disk_usage",
                        lambda path: (_ for _ in ()).throw(OSError("gone")))
    assert health.disk("/") is None


# -- one complete reading --------------------------------------------------

def test_a_sample_always_has_the_same_shape(monkeypatch):
    """Whatever this machine is, every key the templates and the web page read
    is present -- missing readings are None, never absent."""
    monkeypatch.setattr(health, "_read", lambda path: None)
    monkeypatch.setattr(health, "_vcgencmd", lambda *args: None)
    monkeypatch.setattr(health.os, "listdir", lambda path: [])
    reading = health.sample("/")
    for key in ("cpu_temp", "cpu_percent", "memory_percent", "disk_percent",
                "disk_free", "undervoltage", "undervoltage_since_boot",
                "throttled", "host_uptime", "load", "memory", "disk", "power",
                "at"):
        assert key in reading, key
    assert reading["cpu_temp"] is None
    assert reading["undervoltage"] is None


def test_a_sample_is_json_serialisable():
    import json

    json.dumps(health.sample(""))


# -- the accessor the render loop uses -------------------------------------

def test_snapshot_never_blocks_the_render_loop(monkeypatch):
    """The frame asks for this several times a second mid-transition. It must
    not be what runs vcgencmd."""
    calls = []

    def slow_sample(disk_path=""):
        calls.append(threading.current_thread().name)
        time.sleep(0.05)
        return {"cpu_temp": 44.0}

    monkeypatch.setattr(health, "sample", slow_sample)
    monitor = health.Health(interval=5.0)

    started = time.monotonic()
    assert monitor.snapshot() == {}          # nothing measured yet
    assert time.monotonic() - started < 0.02, "snapshot did the reading itself"

    deadline = time.monotonic() + 2.0
    while not monitor.snapshot() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert monitor.snapshot() == {"cpu_temp": 44.0}
    assert calls and all(name.startswith("picframe3-health") for name in calls)


def test_a_fresh_reading_is_not_taken_again(monkeypatch):
    calls = []
    monkeypatch.setattr(health, "sample", lambda disk_path="": calls.append(1) or {"x": 1})
    monitor = health.Health(interval=3600.0)
    monitor.refresh()
    for _ in range(20):
        monitor.snapshot()
    assert len(calls) == 1


def test_a_failed_reading_leaves_the_last_one_alone(monkeypatch):
    monitor = health.Health(interval=5.0)
    monkeypatch.setattr(health, "sample", lambda disk_path="": {"cpu_temp": 50.0})
    monitor.refresh()
    monitor._taken_at = time.monotonic() - 60      # due for a new reading

    def boom(disk_path=""):
        raise OSError("no /sys today")

    monkeypatch.setattr(health, "sample", boom)
    deadline = time.monotonic() + 2.0
    monitor.snapshot()
    while monitor._busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert monitor.snapshot() == {"cpu_temp": 50.0}


def test_switching_it_off_publishes_nothing(monkeypatch):
    monkeypatch.setattr(health, "sample", lambda disk_path="": {"cpu_temp": 50.0})
    monitor = health.Health(interval=5.0, enabled=False)
    monitor.refresh()
    assert monitor.snapshot() == {}


def test_the_interval_has_a_floor():
    """A frame configured to measure ten times a second would spend its life
    in vcgencmd."""
    assert health.Health(interval=0.1).interval == 5.0
