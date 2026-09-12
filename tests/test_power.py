import datetime as dt

import pytest

from picframe3.control.power import PowerSchedule, TimeRange


@pytest.mark.parametrize("text,minutes,inside", [
    ("09:00-17:00", 10 * 60, True),
    ("09:00-17:00", 18 * 60, False),
    ("22:30-07:00", 23 * 60, True),      # wraps midnight
    ("22:30-07:00", 6 * 60, True),
    ("22:30-07:00", 12 * 60, False),
])
def test_ranges(text, minutes, inside):
    assert TimeRange.parse(text).contains(minutes) is inside


def test_bad_range_is_ignored(caplog):
    assert TimeRange.parse("nonsense") is None


def test_nightly_off_schedule():
    schedule = PowerSchedule({"all": ["22:30-07:00"]})
    assert schedule.display_should_be_on(dt.datetime(2026, 9, 12, 12, 0))
    assert not schedule.display_should_be_on(dt.datetime(2026, 9, 12, 23, 0))
    assert not schedule.display_should_be_on(dt.datetime(2026, 9, 12, 6, 0))


def test_per_weekday_rule_does_not_leak(caplog):
    schedule = PowerSchedule({"saturday": ["00:00-09:00"]})
    assert not schedule.display_should_be_on(dt.datetime(2026, 9, 12, 8, 0))   # Sat
    assert schedule.display_should_be_on(dt.datetime(2026, 9, 11, 8, 0))       # Fri
    assert "cannot parse" not in caplog.text


def test_dimming():
    schedule = PowerSchedule({}, {"19:00-23:00": 0.4})
    assert schedule.brightness(dt.datetime(2026, 9, 12, 20, 0)) == 0.4
    assert schedule.brightness(dt.datetime(2026, 9, 12, 12, 0)) == 1.0
