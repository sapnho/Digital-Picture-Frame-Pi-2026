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
import time
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .db import Library, Record

_log = logging.getLogger(__name__)

#: ``shuffle``      every picture once per round, random order, no repeats
#: ``random``       a fresh shuffle with no memory: repeats are possible
#: ``least_played`` strictly the picture that has been shown fewest times
ORDER_MODES = ("shuffle", "random", "date_desc", "date_asc", "name", "folder",
               "recent", "least_played")


@dataclass
class Filters:
    subfolder: str = ""
    tags_any: list[str] = field(default_factory=list)
    tags_all: list[str] = field(default_factory=list)
    tags_none: list[str] = field(default_factory=list)
    date_from: float | None = None
    date_to: float | None = None
    min_rating: int | None = None
    location_contains: str = ""
    search: str = ""
    include_videos: bool = True
    include_images: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "subfolder": self.subfolder,
            "tags_any": list(self.tags_any),
            "tags_all": list(self.tags_all),
            "tags_none": list(self.tags_none),
            "date_from": self.date_from,
            "date_to": self.date_to,
            "min_rating": self.min_rating,
            "location_contains": self.location_contains,
            "search": self.search,
            "include_videos": self.include_videos,
            "include_images": self.include_images,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Filters:
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @property
    def active(self) -> bool:
        return bool(
            self.subfolder or self.tags_any or self.tags_all or self.tags_none
            or self.date_from or self.date_to or self.min_rating
            or self.location_contains or self.search
            or not self.include_videos or not self.include_images
        )


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
    def _where(self) -> tuple[str, list[Any]]:
        clauses = ["f.hidden = 0"]
        params: list[Any] = []
        f = self.filters
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
                  ", filtered" if self.filters.active else "",
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
