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


# -- a bad schedule must not fill the SD card ------------------------------
# The maintenance loop asks the schedule what it should be doing once a second.
# Parsing on every call meant one mistyped range wrote one warning per second —
# 86 400 lines a day, into a journal that on this machine lives on the card the
# photographs are on.

def test_a_malformed_range_is_reported_once_not_on_every_evaluation(caplog):
    schedule = PowerSchedule({"all": ["22:30 to seven"]})
    with caplog.at_level("WARNING"):
        for _ in range(200):
            schedule.display_should_be_on(dt.datetime(2026, 9, 12, 12, 0))
    complaints = [r for r in caplog.records if "cannot parse" in r.getMessage()]
    assert len(complaints) == 1
    assert schedule.display_should_be_on(dt.datetime(2026, 9, 12, 23, 0)), \
        "an unparseable range turns nothing off"


def test_a_malformed_dimming_step_is_reported_once_too(caplog):
    schedule = PowerSchedule({}, {"19:00 till late": 0.4})
    with caplog.at_level("WARNING"):
        for _ in range(200):
            schedule.brightness(dt.datetime(2026, 9, 12, 20, 0))
    assert len([r for r in caplog.records if "cannot parse" in r.getMessage()]) == 1


def test_editing_the_schedule_is_picked_up_without_a_new_object():
    """`power` is a live section: the settings page writes a new dict onto the
    running config, and the cache must notice rather than keep last week's
    times."""
    schedule = PowerSchedule({"all": ["22:30-07:00"]})
    assert not schedule.display_should_be_on(dt.datetime(2026, 9, 12, 23, 0))
    schedule.off_schedule = {"all": ["09:00-10:00"]}
    assert schedule.display_should_be_on(dt.datetime(2026, 9, 12, 23, 0))
    assert not schedule.display_should_be_on(dt.datetime(2026, 9, 12, 9, 30))


def test_the_schedule_is_parsed_once_rather_than_on_every_call(monkeypatch):
    """The point of the cache, held directly: a thousand evaluations, one parse."""
    from picframe3.control import power as power_module

    calls = []
    original = power_module.TimeRange.parse

    def counted(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(power_module.TimeRange, "parse", staticmethod(counted))
    schedule = PowerSchedule({"all": ["22:30-07:00"]}, {"19:00-22:30": 0.45})
    for _ in range(1000):
        schedule.display_should_be_on(dt.datetime(2026, 9, 12, 12, 0))
        schedule.brightness(dt.datetime(2026, 9, 12, 12, 0))
    assert len(calls) == 2
