"""The removal journal: what left the library, when, and where it came from.

``picframe`` moved a rejected picture into a folder and forgot everything else
about it.  That is fine until the folder holds four hundred files called
``IMG_4312.jpg`` and nobody can say which holiday any of them came from, or
whether the one missing from the wall was removed on purpose.

So the frame writes a line here *before* it drops the database row -- the row
is the only place the title, the date taken, the place name and the tags live,
and ``Library.forget()`` deletes it.  One JSON object per line, appended, in
the same folder as the files themselves:

* it survives losing the database, which is the failure that would otherwise
  take the provenance with it,
* it can be read with ``cat`` on the Pi, without this software,
* and because it keeps the original full path, a removal can be undone -- the
  picture goes back exactly where it was, not into some generic inbox.

Restoring rewrites the line rather than deleting it.  A journal that forgets
the things it was asked to undo is not a journal.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any

_log = logging.getLogger(__name__)

#: Lives beside the removed files, not in the data directory, so that copying
#: the folder off the Pi copies its explanation with it.
JOURNAL_NAME = "removals.jsonl"


def _iso(stamp: float | None) -> str | None:
    if not stamp:
        return None
    try:
        return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return None


class RemovalLog:
    """Append-only journal of removed pictures, with restore."""

    def __init__(self, folder: str):
        self.folder = os.path.expanduser(folder)
        self.path = os.path.join(self.folder, JOURNAL_NAME)
        self._cache: list[dict[str, Any]] | None = None
        self._cache_key: tuple[float, int] | None = None

    # -- writing -----------------------------------------------------------
    def record(self, record: Any, stored_as: str, *, source: str = "") -> dict[str, Any]:
        """Note one removal.  ``record`` is a :class:`~picframe3.library.db.Record`."""
        entry: dict[str, Any] = {
            "stored_as": stored_as,
            "removed_at": time.time(),
            "removed_iso": _iso(time.time()),
            "source": source or "",
            "original_path": record.path,
            "folder": record.folder,
            "basename": record.basename,
            "size": record.size,
            "is_video": bool(record.is_video),
            "width": record.width,
            "height": record.height,
            "taken_at": record.taken_at,
            "taken_iso": _iso(record.taken_at),
            "title": record.title,
            "caption": record.caption,
            "location": record.location,
            "latitude": record.latitude,
            "longitude": record.longitude,
            "tags": list(record.tags or []),
            "make": record.make,
            "model": record.model,
            "rating": record.rating,
            "duration": record.duration,
            "play_count": record.play_count,
            "last_played": record.last_played,
            "last_played_iso": _iso(record.last_played),
            "restored_at": None,
            "restored_to": None,
        }
        self._append(entry)
        return entry

    def _append(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            os.makedirs(self.folder, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                # A power cut can leave the last line without its newline.
                # Appending straight onto it would glue the next removal to the
                # torn one and lose both; a newline first costs nothing and
                # keeps the damage to the single line that was actually torn.
                if fh.tell() and not self._ends_with_newline():
                    fh.write("\n")
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:      # never let bookkeeping break the frame
            _log.error("cannot write the removal journal at %s: %s", self.path, exc)
            return
        self._invalidate()

    def _ends_with_newline(self) -> bool:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                return fh.read(1) == b"\n"
        except OSError:
            return True         # empty or unreadable: nothing to glue onto

    def mark_restored(self, stored_as: str, restored_to: str,
                      when: float | None = None) -> bool:
        """Rewrite one line as restored.  The entry stays in the journal."""
        entries = self.entries(include_restored=True)
        hit = False
        stamp = when if when is not None else time.time()
        for entry in entries:
            if entry.get("stored_as") == stored_as and not entry.get("restored_at"):
                entry["restored_at"] = stamp
                entry["restored_iso"] = _iso(stamp)
                entry["restored_to"] = restored_to
                hit = True
        if hit:
            self._rewrite(entries)
        return hit

    def _rewrite(self, entries: list[dict[str, Any]]) -> None:
        os.makedirs(self.folder, exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                for entry in entries:
                    fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)      # atomic: never a half-written journal
        except OSError as exc:
            _log.error("cannot rewrite the removal journal at %s: %s", self.path, exc)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return
        self._invalidate()

    # -- reading -----------------------------------------------------------
    def entries(self, *, include_restored: bool = True,
                newest_first: bool = False) -> list[dict[str, Any]]:
        rows = self._load()
        if not include_restored:
            rows = [r for r in rows if not r.get("restored_at")]
        return list(reversed(rows)) if newest_first else list(rows)

    def find(self, stored_as: str) -> dict[str, Any] | None:
        for entry in reversed(self._load()):
            if entry.get("stored_as") == stored_as and not entry.get("restored_at"):
                return entry
        return None

    def count(self) -> int:
        """How many removed pictures are still sitting in the folder."""
        return sum(1 for r in self._load() if not r.get("restored_at"))

    def latest(self) -> dict[str, Any] | None:
        for entry in reversed(self._load()):
            if not entry.get("restored_at"):
                return entry
        return None

    def summary(self) -> dict[str, Any]:
        """The small payload the state document and Home Assistant read."""
        last = self.latest()
        return {
            "count": self.count(),
            "folder": self.folder,
            "journal": self.path,
            "last_basename": (last or {}).get("basename") or "",
            "last_removed_iso": (last or {}).get("removed_iso") or "",
            "last_folder": (last or {}).get("folder") or "",
        }

    # -- cache -------------------------------------------------------------
    def _invalidate(self) -> None:
        self._cache = None
        self._cache_key = None

    def _load(self) -> list[dict[str, Any]]:
        """Parsed journal, re-read only when the file has actually changed.

        ``state()`` runs on every publish and every open web page, so this must
        not be a file read each time.  mtime plus size is enough: an edit that
        changes neither is an edit this journal never makes.
        """
        try:
            stat = os.stat(self.path)
            key = (stat.st_mtime, stat.st_size)
        except OSError:
            self._cache, self._cache_key = [], None
            return self._cache
        if self._cache is not None and key == self._cache_key:
            return self._cache
        rows: list[dict[str, Any]] = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                for number, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        # A power cut mid-append can leave one torn line.  Skip
                        # it and keep the rest rather than losing the journal.
                        _log.warning("skipping unreadable line %d of %s", number, self.path)
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError as exc:
            _log.error("cannot read the removal journal at %s: %s", self.path, exc)
            return []
        self._cache, self._cache_key = rows, key
        return rows
