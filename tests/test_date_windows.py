"""Rolling date filters: a rule that is re-resolved, not a pair of dates.

The bug these tests exist for is a quiet one.  "Last 7 days" used to be
implemented by working out the two dates it meant at the moment the button was
pressed and storing those, so a month later the frame was still showing that
same week and nothing anywhere said so.  On a device that runs for a year
between restarts, a filter that freezes is worse than one that fails.

So what is stored is the rule, and every one of these tests is about the rule
still meaning what it says after time has passed.
"""

import time
from datetime import datetime, timedelta

import pytest

from picframe3.library.playlist import (
    DATE_WINDOWS,
    Filters,
    Playlist,
    window_clause,
)


def _day(offset_days: float) -> float:
    """A timestamp that many days ago, at noon, so no test sits on midnight."""
    return (datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
            + timedelta(days=offset_days)).timestamp()


@pytest.fixture
def dated_library(library):
    """The shared library, with known dates written onto its pictures."""
    ids = [r["id"] for r in library.connect().execute("SELECT id FROM files ORDER BY id")]
    ages = [0, 3, 20, 60, 200, 800, 2000]          # days ago
    conn = library.connect()
    for file_id, age in zip(ids, ages, strict=False):
        conn.execute("UPDATE files SET taken_at=? WHERE id=?", (_day(-age), file_id))
    return library, ids


def _matching(library, window: str) -> int:
    playlist = Playlist(library, order="name", persist=False,
                        filters=Filters(date_window=window))
    return playlist.size


# -- the rule itself --------------------------------------------------------

def test_every_offered_window_resolves():
    """A button the frame offers must not come back as "no idea"."""
    for name in DATE_WINDOWS:
        clause, params = window_clause(name)
        if name == "all":
            assert clause == ""
        else:
            assert clause, f"{name} produced no clause"
        assert isinstance(params, list)


def test_an_unknown_window_shows_everything_rather_than_nothing():
    """A filter written by an older frame must not empty the wall."""
    assert window_clause("last_fortnight") == ("", [])


def test_the_window_moves_with_the_day():
    """The same rule, asked on two different days, means two different ranges."""
    monday = datetime(2026, 3, 2, 9, 0).timestamp()
    friday = datetime(2026, 3, 6, 9, 0).timestamp()
    _, monday_params = window_clause("7d", now=monday)
    _, friday_params = window_clause("7d", now=friday)
    assert friday_params[0] - monday_params[0] == pytest.approx(4 * 86400)


def test_today_starts_at_midnight_not_twentyfour_hours_ago():
    noon = datetime(2026, 3, 2, 12, 0).timestamp()
    _, params = window_clause("today", now=noon)
    assert params[0] == datetime(2026, 3, 2, 0, 0).timestamp()


# -- against a real library -------------------------------------------------

def test_each_window_selects_the_pictures_inside_it(dated_library):
    library, _ = dated_library
    assert _matching(library, "today") == 1               # 0 days ago
    assert _matching(library, "7d") == 2                  # 0, 3
    assert _matching(library, "30d") == 3                 # 0, 3, 20
    assert _matching(library, "90d") == 4                 # + 60
    assert _matching(library, "1y") == 5                  # + 200
    assert _matching(library, "3y") == 6                  # + 800
    assert _matching(library, "all") == _matching(library, "")


def test_on_this_day_finds_the_same_date_in_other_years(library):
    """The anniversary view: same day and month, any year."""
    ids = [r["id"] for r in library.connect().execute("SELECT id FROM files ORDER BY id")]
    conn = library.connect()
    conn.execute("UPDATE files SET taken_at=?", (_day(-40),))    # nobody matches
    conn.execute("UPDATE files SET taken_at=? WHERE id=?", (_day(-365), ids[0]))
    conn.execute("UPDATE files SET taken_at=? WHERE id=?", (_day(-730), ids[1]))
    assert _matching(library, "on_this_day") == 2


# -- how it reaches the filter ---------------------------------------------

def test_a_window_and_a_date_range_never_both_apply():
    """Two answers to one question is how a filter panel starts lying."""
    both = Filters().merged({"date_window": "30d"}).merged({"date_from": "2024-07-14"})
    assert both.date_from is not None and both.date_window == ""

    other_way = Filters().merged({"date_from": "2024-07-14"}).merged(
        {"date_window": "30d"})
    assert other_way.date_window == "30d"
    assert other_way.date_from is None and other_way.date_to is None


def test_the_window_survives_a_patch_that_says_nothing_about_dates():
    """Home Assistant sends one box at a time; typing a place must not undo it."""
    filters = Filters().merged({"date_window": "7d"})
    after = filters.merged({"location_contains": "France"})
    assert after.date_window == "7d"


def test_choosing_all_dates_clears_the_window():
    filters = Filters().merged({"date_window": "7d"}).merged({"date_window": "all"})
    assert filters.date_window == ""
    assert not filters.active


def test_the_window_is_what_gets_stored_not_the_dates(library):
    """The stored filter has to be the rule, or it freezes the day it is set."""
    playlist = Playlist(library, order="name", persist=True)
    playlist.set_filters(Filters().merged({"date_window": "7d"}))
    stored = library.get_state("playlist_filters")
    assert stored["date_window"] == "7d"
    assert not stored["date_from"] and not stored["date_to"]


def test_the_panel_is_told_what_to_highlight():
    filters = Filters().merged({"date_window": "on_this_day"})
    payload = filters.as_dict()
    assert payload["date_window"] == "on_this_day"
    assert payload["date_window_text"] == "On this day"
    assert "on this day" in filters.describe()


def test_the_frame_re_resolves_when_the_day_turns(library):
    """Nothing is ever *wrong*, but yesterday's selection is still in hand."""
    from picframe3.app import PicFrame
    from picframe3.config import Config

    frame = PicFrame(Config())
    frame.library = library
    frame.playlist = Playlist(library, order="name", persist=False,
                              filters=Filters(date_window="today"))
    frame._window_day = "1970-01-01"
    refreshed = []
    frame.playlist.refresh = lambda: refreshed.append(True)
    frame._publish = lambda: None

    frame._roll_date_window()
    assert refreshed == [True]
    assert frame._window_day == time.strftime("%Y-%m-%d")

    frame._roll_date_window()                 # same day: nothing to do
    assert refreshed == [True]
