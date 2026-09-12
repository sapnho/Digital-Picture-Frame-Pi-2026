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

Restoring rewrites the line rather than deleting it, and so does emptying the
trash.  A journal that forgets the things it was asked to undo -- or the things
it was asked to delete for good -- is not a journal.  A purged line keeps every
field it had and gains ``purged_at``: the file is gone, the record of what it
was is not.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
from collections.abc import Iterable
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
            # Carried from the start so a line is self-describing: a reader
            # with no schema can see that purging is a thing that happens.
            "purged_at": None,
        }
        self._append(entry)
        return entry

    @contextlib.contextmanager
    def _locked(self):
        """Hold the journal lock, on a file that is never replaced.

        Deliberately not the journal itself.  A restore rewrites the journal
        through a temporary file and ``os.replace``, which puts a *new* inode
        at that path -- so a removal waiting on a lock taken on the old inode
        would be granted it after the rename and write into a file nobody will
        ever read again.  The lock file has no content and is never renamed,
        so the two really do take turns.
        """
        os.makedirs(self.folder, exist_ok=True)
        with open(self.path + ".lock", "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _append(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            os.makedirs(self.folder, exist_ok=True)
            with self._locked(), open(self.path, "a", encoding="utf-8") as fh:
                # Exclusive for the whole append: a restore running at the same
                # moment reads the journal, rewrites it into a temp file and
                # renames that over the original, and a removal appended in
                # between would land in the file that is about to be replaced
                # and be lost without trace.  The lock is what makes the two
                # take turns.
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
        """Rewrite one line as restored.  The entry stays in the journal.

        Read and rewrite happen under the same lock as :meth:`_append`, and the
        journal is re-read inside it rather than before it -- otherwise a
        removal recorded between the read and the rename is written into a file
        that is then replaced by an older copy of itself.
        """
        stamp = when if when is not None else time.time()
        if not os.path.exists(self.path):
            return False            # nothing to rewrite, and nothing to create
        try:
            os.makedirs(self.folder, exist_ok=True)
            # "a" rather than "w": opening the journal to lock it must not be
            # able to truncate it.
            with self._locked():
                self._invalidate()          # do not trust a cache read before the lock
                entries = self.entries(include_restored=True, include_purged=True)
                hit = False
                for entry in entries:
                    # Not a purged line: the file is gone for good, and a
                    # journal that says a deleted picture was put back is
                    # worse than one that says nothing.
                    if (entry.get("stored_as") == stored_as
                            and not entry.get("restored_at")
                            and not entry.get("purged_at")):
                        entry["restored_at"] = stamp
                        entry["restored_iso"] = _iso(stamp)
                        entry["restored_to"] = restored_to
                        hit = True
                if hit:
                    self._rewrite(entries)
        except OSError as exc:
            _log.error("cannot update the removal journal at %s: %s", self.path, exc)
            return False
        return hit

    def mark_purged(self, names: str | Iterable[str],
                    when: float | None = None) -> list[str]:
        """Rewrite lines as purged -- the file is gone for good.

        Takes a whole batch in one call on purpose.  Emptying a trash of four
        hundred pictures one line at a time would rewrite the journal four
        hundred times, and every one of those rewrites is a full read, a temp
        file and an ``os.replace``.

        Returns the names actually marked, so a caller can tell "deleted" from
        "was never there".  A line already purged or already restored is left
        alone: purging cannot undo a restore, and it cannot happen twice.
        """
        wanted = {names} if isinstance(names, str) else {str(n) for n in names}
        if not wanted or not os.path.exists(self.path):
            return []
        stamp = when if when is not None else time.time()
        done: list[str] = []
        try:
            os.makedirs(self.folder, exist_ok=True)
            with self._locked():
                self._invalidate()      # do not trust a cache read before the lock
                entries = self.entries(include_restored=True, include_purged=True)
                for entry in entries:
                    name = entry.get("stored_as")
                    if (name in wanted and not entry.get("restored_at")
                            and not entry.get("purged_at")):
                        entry["purged_at"] = stamp
                        entry["purged_iso"] = _iso(stamp)
                        done.append(str(name))
                if done:
                    self._rewrite(entries)
        except OSError as exc:
            _log.error("cannot update the removal journal at %s: %s", self.path, exc)
            return []
        return done

    def _rewrite(self, entries: list[dict[str, Any]]) -> None:
        """Replace the journal with *entries*.  Call it holding the lock."""
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
                include_purged: bool = False,
                newest_first: bool = False) -> list[dict[str, Any]]:
        """Journal lines, oldest first unless asked otherwise.

        Purged lines are out by default: the question nearly every caller is
        asking is "what is in the trash right now", and a purged picture is
        not.  Pass ``include_purged`` to read the record of what was deleted --
        and note that :meth:`_rewrite` callers *must* pass it, or a rewrite
        would silently drop the lines it left out.
        """
        rows = self._load()
        if not include_restored:
            rows = [r for r in rows if not r.get("restored_at")]
        if not include_purged:
            rows = [r for r in rows if not r.get("purged_at")]
        return list(reversed(rows)) if newest_first else list(rows)

    def in_trash(self) -> list[dict[str, Any]]:
        """The lines whose file should still be in the folder."""
        return [r for r in self._load()
                if not r.get("restored_at") and not r.get("purged_at")]

    def find(self, stored_as: str) -> dict[str, Any] | None:
        for entry in reversed(self._load()):
            if (entry.get("stored_as") == stored_as and not entry.get("restored_at")
                    and not entry.get("purged_at")):
                return entry
        return None

    def count(self) -> int:
        """How many removed pictures are still sitting in the folder."""
        return len(self.in_trash())

    def purged_count(self) -> int:
        """How many were deleted for good.  The journal still explains them."""
        return sum(1 for r in self._load() if r.get("purged_at"))

    def latest(self) -> dict[str, Any] | None:
        for entry in reversed(self._load()):
            if not entry.get("restored_at") and not entry.get("purged_at"):
                return entry
        return None

    def summary(self) -> dict[str, Any]:
        """The small payload the state document and Home Assistant read."""
        last = self.latest()
        return {
            "count": self.count(),
            "purged": self.purged_count(),
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
