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
import time
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .db import Library, Record

_log = logging.getLogger(__name__)

#: ``shuffle``      every picture once per round, random order, no repeats
#: ``random``       a fresh shuffle with no memory: repeats are possible
#: ``least_played`` strictly the picture that has been shown fewest times
ORDER_MODES = ("shuffle", "random", "date_desc", "date_asc", "name", "folder",
               "recent", "least_played")

#: What a dropdown says when it is not narrowing anything down.  Home
#: Assistant's select entity has no empty option, so the word has to be a real
#: one; ``ANY_OTHER`` is what it shows when the filter was set from somewhere
#: else to something that is not in its list.
ANYTHING = "(all)"
ANY_OTHER = "(other)"


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
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
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
        if "date_from" in data:
            out.date_from = parse_date(data["date_from"])
        if "date_to" in data:
            out.date_to = parse_date(data["date_to"], end_of_day=True)
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
        return out

    @property
    def active(self) -> bool:
        return bool(
            self.subfolder or self.tags_any or self.tags_all or self.tags_none
            or self.date_from or self.date_to or self.min_rating
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
        if self.date_from or self.date_to:
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
        clauses = ["f.hidden = 0"]
        params: list[Any] = []
        f = filters if filters is not None else self.filters
        if f.subfolder:
            clauses.append("f.folder LIKE ?")
            params.append(f"%{f.subfolder.rstrip('/')}%")
        if not f.include_videos:
            clauses.append("f.is_video = 0")
        if not f.include_images:
            clauses.append("f.is_video = 1")
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
            clauses.append("f.location LIKE ?")
            params.append(f"%{f.location_contains}%")
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
        if f.search:
            query = " ".join(f'"{t}"*' for t in f.search.split())
            clauses.append("f.id IN (SELECT rowid FROM search WHERE search MATCH ?)")
            params.append(query)
        return " AND ".join(clauses), params

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
        if self._future:
            ids = self._future.popleft()
        else:
            ids = self._advance_group()
        if not ids:
            return []
        if self.current_ids:
            self._history.append(list(self.current_ids))
        self.current_ids = ids
        if self.persist:
            self.library.mark_round(ids, self._round)
        self._save_position()
        return [r for r in (self.library.get(i) for i in ids) if r is not None]

    def previous(self) -> list[Record]:
        if not self._history:
            return self.next()
        ids = self._history.pop()
        if self.current_ids:
            self._future.appendleft(list(self.current_ids))
        self.current_ids = ids
        self._save_position()
        return [r for r in (self.library.get(i) for i in ids) if r is not None]

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
        """The group that ``next()`` would return, without consuming it."""
        saved = (self._pos, self._passes, self._round, list(self._ids))
        try:
            ids = self._advance_group()
        finally:
            self._pos, self._passes, self._round, self._ids = saved
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
        self.filters = filters
        if self.persist:
            self.library.set_state("playlist_filters", filters.as_dict())
        self.refresh()
