"""Which picture comes next.

The selection logic is separated from both the index and the renderer so it can
be reasoned about (and tested) on its own.  It covers the things that actually
matter on a frame that runs for years:

* a shuffle that gives every picture exactly one turn per round, in a fresh
  random order each round, and that survives reboots, rescans and pictures
  arriving over the network in the middle of a round -- because "already had
  its turn this round" is a column in the index, not a cursor in memory;
* newly added photographs jumping the queue for a while, because that is what
  people want to see after a trip;
* filters (folder, tag, date, rating, free text) that compose;
* portrait pairing -- two upright photographs shown side by side on a landscape
  panel instead of one picture floating in a sea of background.
"""

from __future__ import annotations

import logging
import random
import re
import sqlite3
import time
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .db import Library, Record, fts_query, like_escape

_log = logging.getLogger(__name__)

#: ``shuffle``      every picture once per round, random order, no repeats
#: ``random``       a fresh shuffle with no memory: repeats are possible
#: ``least_played`` strictly the picture that has been shown fewest times
ORDER_MODES = ("shuffle", "random", "date_desc", "date_asc", "name", "folder",
               "recent", "least_played")

#: Where a sort order chosen on the running frame is remembered.
ORDER_STATE_KEY = "playlist_order"


def starting_order(configured: str, remembered: Any) -> str:
    """The order a frame should start in.

    Choosing an order from the web page or Home Assistant applies it at once
    but does not write the config file, so a restart used to put the frame
    straight back to whatever the file said.  The choice is remembered in the
    index together with the order the *file* said at the time, and wins at
    start-up for as long as the file still says that.  Once somebody edits the
    file (or presses Save, which writes the choice into it) the file is the
    newer word and is obeyed.  With nothing set anywhere the answer is
    ``shuffle``.
    """
    configured = configured if configured in ORDER_MODES else "shuffle"
    if isinstance(remembered, dict):
        order = remembered.get("order")
        if order in ORDER_MODES and remembered.get("configured") == configured:
            return order
    return configured


#: What a dropdown says when it is not narrowing anything down.  Home
#: Assistant's select entity has no empty option, so the word has to be a real
#: one; ``ANY_OTHER`` is what it shows when the filter was set from somewhere
#: else to something that is not in its list.
ANYTHING = "(all)"
ANY_OTHER = "(other)"

#: The date filters the frame offers, as *rules* rather than as dates.
#:
#: This is the whole point of them.  Picking "the last 7 days" and storing the
#: two dates it resolved to means that a week later the frame is still showing
#: that same week -- the filter freezes on the day the button was pressed, and
#: on a device that runs for months it drifts silently out of date.  What is
#: stored is the rule; the dates are worked out afresh every time the query
#: runs, and the maintenance loop refreshes the selection when the day turns.
#:
#: The value is the number of days the window covers, counting today as one.
#: ``on_this_day`` is not a range at all -- it is the same calendar day in every
#: year, the "what were we doing a year ago today" view -- and ``all`` is the
#: absence of a date filter.
DATE_WINDOWS: dict[str, str] = {
    "all": "All dates",
    "today": "Today",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "1y": "Last year",
    "3y": "Last 3 years",
    "on_this_day": "On this day",
}

_WINDOW_DAYS = {"today": 1, "7d": 7, "30d": 30, "90d": 90, "1y": 365, "3y": 1095}


def window_clause(name: str, now: float | None = None) -> tuple[str, list[Any]]:
    """SQL for a rolling date window, resolved against *now*.

    Returns an empty clause for "all" and for anything unrecognised, so a
    filter written by an older frame -- or by a typo in an MQTT payload --
    shows everything rather than nothing.
    """
    key = str(name or "").strip().lower().replace("-", "_")
    if key in ("", "all"):
        return "", []
    if key == "on_this_day":
        # Resolved by SQLite itself, so it stays true across midnight without
        # anybody having to re-run anything.
        return ("strftime('%m-%d', f.taken_at, 'unixepoch', 'localtime') "
                "= strftime('%m-%d', 'now', 'localtime')"), []
    days = _WINDOW_DAYS.get(key)
    if days is None:
        _log.warning("unknown date window %r; showing every date", name)
        return "", []
    moment = datetime.fromtimestamp(now if now is not None else time.time())
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight.timestamp() - (days - 1) * 86400
    return "f.taken_at >= ?", [start]


def parse_date(value: Any, *, end_of_day: bool = False) -> float | None:
    """A date as a person writes it, as a Unix timestamp.

    Four kinds of caller reach this: Home Assistant and the browser's date
    input send ``2024-07-14``, a migrated picframe configuration may hold a
    raw timestamp, somebody typing by hand in Germany writes ``14.07.2024``,
    and an empty box means "no limit".  Guessing wrong here silently empties
    the frame, so every form is understood and anything else is refused.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) or None
    text = str(value).strip()
    if not text:
        return None
    # %Y:%m:%d is the EXIF spelling, and it is what the picframe MQTT
    # automations people already have publish -- dropping it silently sent
    # "2026:09:12" through as "no limit" and quietly widened the filter.
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d",
                "%Y:%m:%d %H:%M:%S", "%Y:%m:%d"):
        try:
            stamp = datetime.strptime(text, fmt)
        except ValueError:
            continue
        # A bare date as an upper limit means the whole of that day, which is
        # what everyone expects and what midnight would quietly cut off.
        if end_of_day:
            stamp = stamp.replace(hour=23, minute=59, second=59)
        return stamp.timestamp()
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        pass
    try:
        return float(text) or None
    except ValueError:
        return None


def date_text(stamp: float | None) -> str:
    """The other direction: what a date box should show."""
    if not stamp:
        return ""
    try:
        return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):  # pragma: no cover
        return ""


def split_tags(value: Any) -> list[str]:
    """``"holiday, france"`` and ``["holiday", "france"]`` mean the same."""
    if value is None:
        return []
    parts = re.split(r"[,;\n]", value) if isinstance(value, str) else [
        str(v) for v in value]
    out: list[str] = []
    for part in parts:
        tag = part.strip()
        if tag and tag not in out:
            out.append(tag)
    return out


def _truth(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "on", "true", "yes", "ja")
    return bool(value)


def _plain(value: Any) -> str:
    """A dropdown's "all" or "other" is not something to search for."""
    text = str(value or "").strip()
    return "" if text in (ANYTHING, ANY_OTHER) else text


@dataclass
class Filters:
    subfolder: str = ""
    tags_any: list[str] = field(default_factory=list)
    tags_all: list[str] = field(default_factory=list)
    tags_none: list[str] = field(default_factory=list)
    #: Whether a picture has to carry *every* tag that was asked for or just
    #: one of them.  Which of ``tags_all`` and ``tags_any`` the tags live in
    #: follows from it, so the switch and the query can never disagree.
    tags_match_all: bool = False
    date_from: float | None = None
    date_to: float | None = None
    #: A rolling window such as "7d" or "on_this_day", re-resolved every time
    #: the query runs.  Set, it replaces date_from/date_to; see DATE_WINDOWS.
    date_window: str = ""
    min_rating: int | None = None
    location_contains: str = ""
    search: str = ""
    include_videos: bool = True
    include_images: bool = True

    #: What the control surfaces may call a field.  Home Assistant's entity is
    #: "location", the old picframe card said "directory", the query says
    #: ``location_contains`` -- one place to keep them all pointing at it.
    ALIASES = {
        "folder": "subfolder",
        "directory": "subfolder",
        "location": "location_contains",
        "place": "location_contains",
        "from": "date_from",
        "to": "date_to",
        "window": "date_window",
        "date_preset": "date_window",
        "dates": "date_window",
        "rating": "min_rating",
    }

    def __post_init__(self) -> None:
        # A filter restored from an older frame, or written by hand, states
        # which bucket it means by putting the tags in it.
        if self.tags_all and not self.tags_any:
            self.tags_match_all = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "subfolder": self.subfolder,
            "tags_any": list(self.tags_any),
            "tags_all": list(self.tags_all),
            "tags_none": list(self.tags_none),
            "tags_match_all": self.tags_match_all,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "date_window": self.date_window,
            "min_rating": self.min_rating,
            "location_contains": self.location_contains,
            "search": self.search,
            "include_videos": self.include_videos,
            "include_images": self.include_images,
            # Derived, for the surfaces that cannot compute: a text box wants
            # "holiday, france" and a date box wants "2024-07-14", and Home
            # Assistant templates cannot do either.
            "tags": self.tags,
            "tags_text": ", ".join(self.tags),
            "date_from_text": date_text(self.date_from),
            "date_to_text": date_text(self.date_to),
            "date_window_text": DATE_WINDOWS.get(self.date_window or "all", ""),
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Filters:
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)

    # -- the tags, as one field --------------------------------------------
    @property
    def tags(self) -> list[str]:
        """The tags being filtered on, whichever way they are combined."""
        return list(self.tags_all or self.tags_any)

    def set_tags(self, value: Any, match_all: bool | None = None) -> None:
        if match_all is not None:
            self.tags_match_all = bool(match_all)
        tags = split_tags(value)
        if self.tags_match_all:
            self.tags_all, self.tags_any = tags, []
        else:
            self.tags_any, self.tags_all = tags, []

    # -- patching ----------------------------------------------------------
    def merged(self, patch: dict[str, Any]) -> Filters:
        """A copy of these filters with the keys in *patch* applied.

        A patch and not a replacement, because every control surface sends one
        field at a time: Home Assistant has a text box per filter, and a frame
        whose location box wiped the date range whenever somebody typed in it
        would be unusable.  Absent keys are left exactly as they were.
        """
        out = Filters.from_dict(self.as_dict())
        data = {self.ALIASES.get(k, k): v for k, v in (patch or {}).items()}
        if _truth(data.pop("reset", False)):
            keep_videos, keep_images = out.include_videos, out.include_images
            out = Filters(include_videos=keep_videos, include_images=keep_images)
        if "tags_match_all" in data:
            out.tags_match_all = _truth(data["tags_match_all"])
            out.set_tags(out.tags)          # into the other bucket, unchanged
        for key in ("tags_any", "tags_all", "tags_none"):
            if key in data:
                setattr(out, key, split_tags(data[key]))
        if "tags" in data:
            out.set_tags(data["tags"])
        if "subfolder" in data:
            out.subfolder = _plain(data["subfolder"])
        if "location_contains" in data:
            out.location_contains = _plain(data["location_contains"])
        if "search" in data:
            out.search = _plain(data["search"])
        if "date_window" in data:
            window = str(data["date_window"] or "").strip().lower().replace("-", "_")
            out.date_window = "" if window in ("", "all", ANYTHING) else window
            # A window and a pair of dates are two answers to the same
            # question, and a frame showing one while the panel says the other
            # is the kind of thing nobody can debug from the sofa.
            if out.date_window:
                out.date_from = out.date_to = None
        if "date_from" in data:
            out.date_from = parse_date(data["date_from"])
            if out.date_from is not None:
                out.date_window = ""
        if "date_to" in data:
            out.date_to = parse_date(data["date_to"], end_of_day=True)
            if out.date_to is not None:
                out.date_window = ""
        if "min_rating" in data:
            value = data["min_rating"]
            try:
                rating = int(float(value)) if str(value).strip() != "" else 0
            except (TypeError, ValueError):
                rating = 0
            out.min_rating = rating or None
        for key in ("include_videos", "include_images"):
            if key in data:
                setattr(out, key, _truth(data[key]))
        if not out.include_videos and not out.include_images:
            # "Neither pictures nor videos" is not a filter anybody means: it
            # selects nothing at all and the frame goes to the empty-library
            # placeholder.  It happens when two control surfaces each switch
            # one of them off, so take it as "both" rather than as an order to
            # show nothing.
            _log.warning("filter excluded both images and videos; showing both")
            out.include_videos = out.include_images = True
        return out

    @property
    def active(self) -> bool:
        return bool(
            self.subfolder or self.tags_any or self.tags_all or self.tags_none
            or self.date_from or self.date_to or self.date_window
            or self.min_rating
            or self.location_contains or self.search
            or not self.include_videos or not self.include_images
        )

    def describe(self) -> str:
        """One line saying what is being shown, for a log or a status line."""
        bits = []
        if self.subfolder:
            bits.append(f"folder ~ {self.subfolder}")
        if self.tags:
            bits.append((" and " if self.tags_match_all else " or ").join(self.tags))
        if self.tags_none:
            bits.append("not " + ", ".join(self.tags_none))
        if self.location_contains:
            bits.append(f"place ~ {self.location_contains}")
        if self.date_window:
            bits.append(DATE_WINDOWS.get(self.date_window, self.date_window).lower())
        elif self.date_from or self.date_to:
            bits.append(f"{date_text(self.date_from) or '…'} to "
                        f"{date_text(self.date_to) or '…'}")
        if self.min_rating:
            bits.append(f"{self.min_rating}+ stars")
        if self.search:
            bits.append(f"\u201c{self.search}\u201d")
        if not self.include_videos:
            bits.append("no videos")
        if not self.include_images:
            bits.append("videos only")
        return "; ".join(bits) or "everything"


class Playlist:
    def __init__(
        self,
        library: Library,
        *,
        order: str = "shuffle",
        filters: Filters | None = None,
        recent_days: int = 7,
        reshuffle_after: int = 1,
        portrait_pairs: bool = False,
        history_size: int = 200,
        persist: bool = True,
    ):
        self.library = library
        self.order = order if order in ORDER_MODES else "shuffle"
        self.filters = filters or Filters()
        self.recent_days = recent_days
        self.reshuffle_after = max(1, reshuffle_after)
        self.portrait_pairs = portrait_pairs
        self.persist = persist

        self._ids: list[int] = []
        self._pos = -1
        # Rounds are 1-based so that a file's default play_round of 0 means
        # "never shown", which is exactly what a freshly indexed picture is.
        self._round = max(1, int(library.get_state("playlist_round", 1))) if persist else 1
        self._seed = str(library.get_state("playlist_seed", "")) if persist else ""
        if not self._seed:
            self._seed = uuid.uuid4().hex
            if persist:
                library.set_state("playlist_seed", self._seed)
        self._passes = 0
        self._history: deque[list[int]] = deque(maxlen=history_size)
        self._future: deque[list[int]] = deque()
        #: The group on screen.  An instance attribute, deliberately: as a
        #: class-level default it would be shared by every Playlist ever made.
        self.current_ids: list[int] = []
        self.refresh()
        if persist:
            self._restore_position()

    # -- query building ----------------------------------------------------
    def _where(self, filters: Filters | None = None) -> tuple[str, list[Any]]:
        # ``held`` is the deliberate one: a picture that was removed and has
        # turned up on disk again stays out of every selection until somebody
        # releases it.  ``hidden`` is the accidental one -- a file that would
        # not decode.
        clauses = ["f.hidden = 0", "f.held = 0"]
        params: list[Any] = []
        f = filters if filters is not None else self.filters
        if f.subfolder:
            clauses.append("f.folder LIKE ? ESCAPE '\\'")
            params.append(f"%{like_escape(f.subfolder.rstrip('/'))}%")
        if not f.include_videos:
            clauses.append("f.is_video = 0")
        if not f.include_images:
            clauses.append("f.is_video = 1")
        if f.date_window:
            # Re-resolved here, on every query, which is what makes "the last
            # 7 days" still mean the last 7 days a month later.
            clause, window_params = window_clause(f.date_window)
            if clause:
                clauses.append(clause)
                params.extend(window_params)
        else:
            if f.date_from is not None:
                clauses.append("f.taken_at >= ?")
                params.append(f.date_from)
            if f.date_to is not None:
                clauses.append("f.taken_at <= ?")
                params.append(f.date_to)
        if f.min_rating is not None:
            clauses.append("COALESCE(f.rating, 0) >= ?")
            params.append(f.min_rating)
        if f.location_contains:
            clauses.append("f.location LIKE ? ESCAPE '\\'")
            params.append(f"%{like_escape(f.location_contains)}%")
        for tag in f.tags_all:
            clauses.append(
                "EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON t.id=ft.tag_id "
                "WHERE ft.file_id=f.id AND t.name = ? COLLATE NOCASE)"
            )
            params.append(tag)
        if f.tags_any:
            placeholders = ",".join("?" * len(f.tags_any))
            clauses.append(
                f"EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON t.id=ft.tag_id "
                f"WHERE ft.file_id=f.id AND t.name IN ({placeholders}) COLLATE NOCASE)"
            )
            params.extend(f.tags_any)
        if f.tags_none:
            placeholders = ",".join("?" * len(f.tags_none))
            clauses.append(
                f"NOT EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON t.id=ft.tag_id "
                f"WHERE ft.file_id=f.id AND t.name IN ({placeholders}) COLLATE NOCASE)"
            )
            params.extend(f.tags_none)
        query = self._search_query(f.search)
        if query:
            clauses.append("f.id IN (SELECT rowid FROM search WHERE search MATCH ?)")
            params.append(query)
        return " AND ".join(clauses), params

    def _search_query(self, text: str) -> str:
        """The MATCH expression for the search box, or "" if it cannot be used.

        FTS5 parses the expression itself, so a search term can still be
        rejected after quoting -- and an unusable search term must never be
        allowed to take the frame down with it.  The filter is persisted, so an
        exception here came back on every start: the frame was bricked by a
        stray double quote until somebody edited the database by hand.  Trying
        the expression once, on a query that touches at most one row, turns
        that into "this search matches nothing" and a line in the log.
        """
        query = fts_query(text)
        if not query:
            return ""
        try:
            self.library.connect().execute(
                "SELECT rowid FROM search WHERE search MATCH ? LIMIT 1", (query,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            _log.warning("search term %r cannot be used (%s); ignoring it", text, exc)
            return ""
        return query

    def _order_sql(self) -> str:
        return {
            "date_desc": "f.taken_at DESC, f.path",
            "date_asc": "f.taken_at ASC, f.path",
            "name": "f.basename COLLATE NOCASE, f.path",
            "folder": "f.folder COLLATE NOCASE, f.basename COLLATE NOCASE",
            "recent": "COALESCE(f.taken_at, f.mtime) DESC",
            "least_played": "f.play_count ASC, COALESCE(f.last_played, 0) ASC, f.path",
        }.get(self.order, "f.path")

    def refresh(self) -> int:
        """Rebuild the id list from the index, preserving the current picture."""
        current = self.current_ids
        where, params = self._where()
        rows = self.library.connect().execute(
            f"SELECT f.id, f.is_portrait, f.taken_at, f.mtime, f.play_round FROM files f "
            f"WHERE {where} ORDER BY {self._order_sql()}",
            params,
        ).fetchall()
        if self.order == "shuffle":
            self._ids, self._pos = self._round_order(rows)
        elif self.order == "random":
            self._ids = self._shuffled(rows)
            self._pos = -1
        else:
            self._ids = [r["id"] for r in rows]
            self._pos = -1
        self._future.clear()
        if current and self.order != "shuffle":
            try:
                self._pos = self._ids.index(current[0]) - 1
            except ValueError:
                self._pos = -1
        _log.info("playlist: %d pictures (order=%s%s)%s", len(self._ids), self.order,
                  f", filtered: {self.filters.describe()}" if self.filters.active else "",
                  f", {len(self._ids) - self._pos - 1} left in round {self._round}"
                  if self.order == "shuffle" else "")
        return len(self._ids)

    def _rng(self) -> random.Random:
        """A different order every round, and a different one on every frame.

        Seeding from the round number alone -- which this did at first -- means
        every picture frame in the world shows round 7 in the same order, and
        that a frame reset to round 7 repeats an order its owner has already
        seen.  The installation seed fixes both while keeping the round itself
        reproducible for as long as it lasts.
        """
        return random.Random(f"{self._seed}:{self._round}")

    def _round_order(self, rows: Sequence[Any]) -> tuple[list[int], int]:
        """Pictures already shown this round, then the rest in random order.

        Everything that matters about fairness is in the split.  A picture is
        owed a turn until its ``play_round`` catches up with the current round,
        so the queue is rebuilt correctly after a reboot, after a rescan, and
        when a hundred new photographs land in the folder halfway through --
        they simply join the pictures still waiting, instead of restarting the
        round and letting the unlucky half never come up.
        """
        done = [r["id"] for r in rows if (r["play_round"] or 0) >= self._round]
        todo = [r for r in rows if (r["play_round"] or 0) < self._round]
        if not todo and done:
            # Everything has had its turn; the next call to next() opens the
            # next round rather than stalling on an empty tail.
            return done, len(done) - 1
        return done + self._shuffled(todo), len(done) - 1

    def _shuffled(self, rows: Sequence[Any]) -> list[int]:
        """Shuffle, but float recent additions to the front of the round."""
        rng = self._rng()
        cutoff = time.time() - self.recent_days * 86400 if self.recent_days > 0 else None
        recent: list[int] = []
        rest: list[int] = []
        for r in rows:
            stamp = r["taken_at"] or r["mtime"]
            if cutoff is not None and stamp and stamp >= cutoff:
                recent.append(r["id"])
            else:
                rest.append(r["id"])
        if self.order == "random":
            combined = recent + rest
            rng.shuffle(combined)
            return combined
        rng.shuffle(recent)
        rng.shuffle(rest)
        return recent + rest

    def count_for(self, filters: Filters) -> int:
        """How many pictures a filter would select, without applying it.

        The panel counts down while you type, so nobody applies a filter to
        the wall to discover it matches four photographs.
        """
        where, params = self._where(filters)
        return self.library.connect().execute(
            f"SELECT COUNT(*) AS n FROM files f WHERE {where}", params
        ).fetchone()["n"]

    def selection(self, limit: int = 200, offset: int = 0) -> list[int]:
        """The ids the filter selects, newest first -- what the browser shows.

        The same ``WHERE`` clause the slideshow itself runs on, so the grid
        cannot disagree with the frame about which pictures are in.
        """
        where, params = self._where()
        rows = self.library.connect().execute(
            f"SELECT f.id FROM files f WHERE {where} "
            f"ORDER BY COALESCE(f.taken_at, f.mtime) DESC LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        ).fetchall()
        return [r["id"] for r in rows]

    # -- navigation --------------------------------------------------------
    @property
    def size(self) -> int:
        return len(self._ids)

    @property
    def position(self) -> int:
        return self._pos

    @property
    def round(self) -> int:
        """The shuffle round now in progress; stamped onto every picture shown."""
        return self._round

    @property
    def remaining(self) -> int:
        """How many pictures are still owed a turn in this round."""
        return max(0, len(self._ids) - self._pos - 1)

    def _advance_group(self) -> list[int]:
        """Take the next one or two ids, honouring portrait pairing."""
        if not self._ids:
            # Nothing selected: there is no round to end.  Going through
            # _end_of_round() anyway bumped the round number, wrote it to the
            # database and re-ran the whole query every time the slideshow
            # ticked -- some 8600 pointless writes a day onto an SD card, for a
            # filter that matches nothing.
            return []
        if self._pos + 1 >= len(self._ids):
            self._end_of_round()
            if not self._ids:
                return []
        self._pos += 1
        first = self._ids[self._pos]
        group = [first]
        if self.portrait_pairs and self._pos + 1 < len(self._ids):
            rec_a = self.library.get(first)
            rec_b = self.library.get(self._ids[self._pos + 1])
            if (rec_a is not None and rec_b is not None
                    and rec_a.is_portrait and rec_b.is_portrait
                    and not rec_a.is_video and not rec_b.is_video):
                self._pos += 1
                group.append(rec_b.id)
        return group

    def _end_of_round(self) -> None:
        self._passes += 1
        self._pos = -1
        if self.order in ("shuffle", "random") and self._passes >= self.reshuffle_after:
            self._passes = 0
            self._round += 1
            if self.persist:
                self.library.set_state("playlist_round", self._round)
            self.refresh()
            self._pos = -1
        _log.info("playlist: round %d complete, starting round %d",
                  self._round - 1, self._round)

    def next(self) -> list[Record]:
        """The next group, skipping ids the index no longer knows about.

        The skipping is the point.  An id goes stale whenever a picture leaves
        the library between two refreshes -- deleted over the share, removed by
        a parallel rescan, pruned -- and resolving it gives an empty list.
        Returning that empty list is indistinguishable to the caller from "the
        library is empty", so one deleted photograph put the *nothing to show*
        screen on the wall for the retry interval instead of simply moving on
        to the next picture.

        The bound is the length of the list: a playlist whose every id has gone
        stale must end, not spin.
        """
        for _ in range(len(self._ids) + 1):
            if self._future:
                ids = self._future.popleft()
            else:
                ids = self._advance_group()
            if not ids:
                return []
            records = [r for r in (self.library.get(i) for i in ids) if r is not None]
            if not records:
                _log.debug("skipping %d picture(s) that left the library", len(ids))
                continue
            if self.current_ids:
                self._history.append(list(self.current_ids))
            self.current_ids = [r.id for r in records]
            if self.persist:
                self.library.mark_round(self.current_ids, self._round)
            self._save_position()
            return records
        return []

    def previous(self) -> list[Record]:
        """The group before this one, skipping any that no longer resolve.

        A picture removed or deleted while it was in the history leaves an id
        that resolves to nothing.  Returning the empty list for it -- which is
        what happened -- is indistinguishable to the caller from "the library is
        empty", so pressing back onto a deleted photograph put the empty-library
        placeholder on the wall.  Walk back until a group actually resolves, and
        if none does, go forward instead.
        """
        while self._history:
            ids = self._history.pop()
            records = [r for r in (self.library.get(i) for i in ids) if r is not None]
            if not records:
                _log.debug("skipping %d removed picture(s) in the history", len(ids))
                continue
            if self.current_ids:
                self._future.appendleft(list(self.current_ids))
            self.current_ids = [r.id for r in records]
            self._save_position()
            return records
        return self.next()

    def jump_to(self, file_id: int) -> list[Record]:
        try:
            self._pos = self._ids.index(file_id) - 1
        except ValueError:
            rec = self.library.get(file_id)
            if rec is None:
                return []
            self._ids.insert(self._pos + 1, file_id)
        self._future.clear()
        return self.next()

    def peek(self) -> list[Record]:
        """The group that ``next()`` would return, without consuming it.

        Purely a look ahead in the list as it stands.  Replaying
        ``_advance_group()`` and putting the fields back afterwards was not
        enough: at the end of a round it went through ``_end_of_round()``, which
        persists the next round number, throws away the redo queue that
        ``previous()`` had filled, and logs a round as complete that nobody has
        watched yet.  The one thing peeking must not do is change anything.
        """
        if self._future:
            ids = list(self._future[0])
        elif self._pos + 1 < len(self._ids):
            ids = [self._ids[self._pos + 1]]
            if self.portrait_pairs and self._pos + 2 < len(self._ids):
                rec_a = self.library.get(ids[0])
                rec_b = self.library.get(self._ids[self._pos + 2])
                if (rec_a is not None and rec_b is not None
                        and rec_a.is_portrait and rec_b.is_portrait
                        and not rec_a.is_video and not rec_b.is_video):
                    ids.append(rec_b.id)
        elif self._ids:
            # The round is exhausted; what comes next is the first picture of
            # the next round.  Which one that is depends on a reshuffle that has
            # not happened yet, so the best honest answer is the head of the
            # list as it stands now.
            ids = [self._ids[0]]
        else:
            ids = []
        return [r for r in (self.library.get(i) for i in ids) if r is not None]

    # -- persistence -------------------------------------------------------
    def _save_position(self) -> None:
        if not self.persist:
            return
        self.library.set_state("playlist_position", {
            "ids": self.current_ids, "pos": self._pos, "round": self._round,
        })

    def _restore_position(self) -> None:
        # A shuffle round rebuilds its own position from play_round, which is
        # more reliable than a saved cursor: the cursor goes stale the moment
        # a file is added or removed while the frame is off.
        if self.order == "shuffle":
            return
        state = self.library.get_state("playlist_position") or {}
        pos = state.get("pos")
        if isinstance(pos, int) and 0 <= pos < len(self._ids):
            self._pos = pos - 1

    # -- mutation ----------------------------------------------------------
    def set_order(self, order: str) -> None:
        if order not in ORDER_MODES:
            raise ValueError(f"unknown order {order!r}; one of {', '.join(ORDER_MODES)}")
        self.order = order
        self.refresh()

    def set_filters(self, filters: Filters) -> None:
        """Apply a filter, and only remember it once it has actually worked.

        The order is the point.  Persisting first and refreshing afterwards
        meant a filter that makes the query fail was already in the database by
        the time it blew up -- and ``__init__`` restores it and refreshes, so
        the frame then failed to start, for good.  Refreshing first keeps the
        blast radius to the one call that asked for it.
        """
        previous = self.filters
        self.filters = filters
        try:
            self.refresh()
        except Exception:
            self.filters = previous
            try:
                self.refresh()
            except Exception:       # pragma: no cover - the index itself is ill
                _log.exception("could not restore the previous filter")
            raise
        if self.persist:
            self.library.set_state("playlist_filters", filters.as_dict())
