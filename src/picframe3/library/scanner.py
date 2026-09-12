"""Walks the picture folders and keeps the index in step with the disk."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..media import metadata
from ..media.geocode import Geocoder
from .db import Library
from .digest import file_digest
from .watch import InotifyWatcher

_log = logging.getLogger(__name__)


#: How many files one transaction covers while scanning.  Large enough that a
#: full scan is not thousands of fsyncs on an SD card, small enough that a power
#: cut loses only the last few files and that the write lock is released often
#: enough for the render loop to get its reads in.
BATCH_SIZE = 100


@dataclass
class ScanResult:
    scanned: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    #: Files recognised as pictures that were removed once and have come back.
    #: Indexed, but kept out of the playlist until somebody releases them.
    held: int = 0
    geocoded: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    #: Configured picture folders that were not there during this scan.  A
    #: missing folder is almost always an unmounted share, and the caller has
    #: to be able to tell that apart from an empty one.
    missing_roots: list[str] = field(default_factory=list)

    def summary(self) -> str:
        out = (f"{self.scanned} files in {self.seconds:.1f}s "
               f"(+{self.added} new, ~{self.updated} changed, -{self.removed} gone)")
        if self.held:
            out += f"; {self.held} held out (removed before)"
        if self.missing_roots:
            out += f"; {len(self.missing_roots)} folder(s) missing"
        return out


class Scanner:
    def __init__(
        self,
        library: Library,
        roots: Sequence[str],
        *,
        follow_links: bool = False,
        geocoder: Geocoder | None = None,
        include_videos: bool = True,
        ignore_hidden: bool = True,
        exclude: Sequence[str] = (),
        workers: int = 2,
        prune_max_fraction: float = 0.2,
    ):
        self.library = library
        self.roots = [os.path.abspath(os.path.expanduser(r)) for r in roots]
        self.follow_links = follow_links
        self.geocoder = geocoder
        self.include_videos = include_videos
        self.ignore_hidden = ignore_hidden
        self.exclude = [e for e in exclude if e]
        #: The largest share of the indexed files under the scanned roots that
        #: a single scan is allowed to delete.  Anything above it is treated as
        #: a mount that has gone away rather than as a deletion; 0 disables the
        #: check for somebody who really does empty folders that way.
        self.prune_max_fraction = prune_max_fraction
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="scan"
        )
        self._watcher: InotifyWatcher | None = None
        self._scanning = False
        #: Filled in by iter_files(); see its docstring.
        self.missing_roots: list[str] = []
        #: Files whose metadata could not be read, with the mtime and size they
        #: had when it failed.  A HEIC on a frame without pillow-heif, or a
        #: truncated download, fails identically on every pass, and a periodic
        #: rescan would otherwise open and fail on all of them every hour.  The
        #: file is retried as soon as it changes on disk -- and after a restart,
        #: which is when the missing plugin may well have been installed.
        self._unreadable: dict[str, tuple[float, int]] = {}

    # -- walking -----------------------------------------------------------
    def wanted(self, path: str) -> bool:
        """Whether this file belongs in the index, by the configured rules.

        The one place that answers it.  ``rescan_paths()`` used to decide for
        itself and applied none of them, so every inotify event put back the
        videos and the dot-files that the settings exclude -- until the next
        full scan took them out again.
        """
        name = os.path.basename(path)
        if self.ignore_hidden and name.startswith("."):
            return False
        try:
            # A filename that is not valid UTF-8 reaches Python with surrogate
            # escapes, and SQLite stores TEXT as UTF-8: binding such a path
            # raises UnicodeEncodeError and, before this check, took the whole
            # scan down with it.  The index cannot hold the name, so the file is
            # skipped -- loudly, because renaming it is the fix.
            path.encode("utf-8")
        except UnicodeEncodeError:
            _log.warning("skipping %r: the name is not valid UTF-8 and cannot be "
                         "indexed; rename it to include it", path)
            return False
        if any(x in path for x in self.exclude):
            return False
        if not metadata.is_supported(path):
            return False
        return not (not self.include_videos and metadata.is_video(path))

    def _wanted_dir(self, dirpath: str, name: str) -> bool:
        if self.ignore_hidden and name.startswith("."):
            return False
        return not any(x in os.path.join(dirpath, name) for x in self.exclude)

    def iter_files(self) -> Iterable[tuple[str, os.stat_result]]:
        """Walk the picture folders, recording any root that is not there.

        The missing roots are collected in :attr:`missing_roots` rather than
        only logged: "the share is not mounted" and "the share is empty" look
        identical from here, and pruning the index on the strength of the wrong
        one is how a library loses everything it knows about its pictures.
        """
        self.missing_roots = []
        for root in self.roots:
            if not os.path.isdir(root):
                _log.warning("picture folder does not exist: %s", root)
                self.missing_roots.append(root)
                continue
            # A symlink pointing at one of its own parents is a loop that
            # os.walk(followlinks=True) will happily follow for ever, building
            # ever longer paths.  Remembering the directories already visited by
            # identity, not by name, is what ends it.
            seen: set[tuple[int, int]] = set()
            for dirpath, dirnames, filenames in os.walk(root, followlinks=self.follow_links):
                if self.follow_links and not self._first_visit(dirpath, seen):
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames if self._wanted_dir(dirpath, d)]
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    if not self.wanted(path):
                        continue
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    if st.st_size == 0:
                        continue
                    yield path, st

    @staticmethod
    def _first_visit(path: str, seen: set[tuple[int, int]]) -> bool:
        """True the first time a directory is reached, by device and inode."""
        try:
            st = os.stat(path)
        except OSError:
            return False
        key = (st.st_dev, st.st_ino)
        if key in seen:
            _log.warning("skipping %s: already visited (symlink loop?)", path)
            return False
        seen.add(key)
        return True

    # -- scanning ----------------------------------------------------------
    def scan(self, *, prune: bool = True,
             progress: Callable[[int, str], None] | None = None) -> ScanResult:
        result = ScanResult()
        t0 = time.monotonic()
        self._scanning = True
        batch: list[tuple[Any, os.stat_result, str | None, bool, str | None, bool]] = []
        held_sizes = self.library.held_sizes()
        try:
            for path, st in self.iter_files():
                result.scanned += 1
                if progress and result.scanned % 200 == 0:
                    progress(result.scanned, path)
                state = self.library.file_state(path)
                if self.library.is_current(state, st.st_mtime, st.st_size):
                    result.skipped += 1
                    continue
                if self._unreadable.get(path) == (st.st_mtime, st.st_size):
                    result.skipped += 1
                    continue
                meta = metadata.read(path)
                if meta is None:
                    self._unreadable[path] = (st.st_mtime, st.st_size)
                    result.errors.append(path)
                    continue
                self._unreadable.pop(path, None)
                location = None
                if self.geocoder is not None and meta.latitude is not None:
                    # Only if it is already cached -- a cold lookup waits for
                    # the backfill pass rather than holding up the scan.
                    location = self.geocoder.lookup(meta.latitude, meta.longitude,
                                                    cached_only=True)
                    if location:
                        result.geocoded += 1
                digest, held = self._check_hold(path, st, held_sizes)
                if held:
                    result.held += 1
                batch.append((meta, st, location, state is None, digest, held))
                if len(batch) >= BATCH_SIZE:
                    self._write_batch(batch, result)
            self._write_batch(batch, result)
            if prune:
                result.missing_roots = list(self.missing_roots)
                if self.missing_roots:
                    # Every row under a folder that is not mounted looks
                    # deleted.  Skip the prune entirely rather than let one
                    # missing share take the play counts, rounds, hidden flags
                    # and place names of a whole library with it.
                    _log.warning(
                        "not pruning the index: %d picture folder(s) are missing "
                        "(%s) -- that looks like a mount problem, not a deletion",
                        len(self.missing_roots), ", ".join(self.missing_roots),
                    )
                else:
                    result.removed = self.library.prune_missing(
                        self.roots, max_fraction=self.prune_max_fraction)
        finally:
            self._scanning = False
        result.seconds = time.monotonic() - t0
        self.library.set_state("last_scan", time.time())
        _log.info("scan complete: %s", result.summary())
        return result

    def _check_hold(self, path: str, st: os.stat_result,
                    held_sizes: set[int]) -> tuple[str | None, bool]:
        """Is this file a picture that was removed once and has come back?

        Only ever asked about a file that is new or whose bytes have changed --
        an already-indexed held-out picture is *current*, so the scan skips it
        long before here and it is never hashed twice.

        The size test in front of the hash is what makes this affordable: the
        size is already in the ``stat`` the walk did, so a library of eight
        thousand photographs costs eight thousand integer comparisons, and only
        a file that really does weigh exactly as much as something in the trash
        is read and hashed.
        """
        if st.st_size not in held_sizes:
            return None, False
        digest = file_digest(path)
        if digest is None:
            return None, False
        hold = self.library.hold_for(digest)
        if hold is None:
            # Same size, different picture.  Keep the digest anyway: it costs
            # one column and means a later removal recognises this copy
            # without reading the file again.
            return digest, False
        self.library.note_seen(digest, path)
        _log.info("%s is a picture that was removed before (as %s); keeping it out "
                  "of the playlist until you say otherwise",
                  path, hold["basename"] or hold["stored_as"])
        return digest, True

    def _write_batch(self,
                     batch: list[tuple[Any, os.stat_result, str | None, bool,
                                       str | None, bool]],
                     result: ScanResult) -> None:
        """Index a batch of files in one transaction, then empty it.

        One transaction per file means one fsync per file, which on an SD card
        is what makes a first scan of a holiday folder take minutes.  One
        transaction per batch also means a crash mid-scan leaves the index
        consistent: the files in the unfinished batch are simply not indexed
        yet, and the next scan picks them up.
        """
        if not batch:
            return
        added = updated = 0
        try:
            with self.library.transaction():
                for meta, st, location, is_new, digest, held in batch:
                    self.library.upsert(meta, mtime=st.st_mtime, size=st.st_size,
                                        location=location, digest=digest, held=held)
                    if is_new:
                        added += 1
                    else:
                        updated += 1
        except Exception as exc:  # pragma: no cover - db level
            # The transaction rolled back, so none of these were indexed --
            # counting them as written would make the result a lie.
            _log.warning("could not index a batch of %d files: %s", len(batch), exc)
            result.errors.extend(meta.path for meta, *_ in batch)
        else:
            result.added += added
            result.updated += updated
        finally:
            batch.clear()

    async def scan_async(self, **kwargs) -> ScanResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, lambda: self.scan(**kwargs))

    def rescan_paths(self, paths: Iterable[str]) -> ScanResult:
        """Reindex a specific set of paths, e.g. after an inotify batch."""
        result = ScanResult()
        t0 = time.monotonic()
        batch: list[tuple[Any, os.stat_result, str | None, bool,
                          str | None, bool]] = []
        held_sizes = self.library.held_sizes()
        for path in paths:
            if os.path.isdir(path):
                continue
            if not os.path.exists(path):
                # The path has gone, and what it was is no longer knowable from
                # the filesystem.  If it was a directory, every picture that was
                # inside it is still in the index and would be shown as a
                # missing file until the next full scan, so drop the subtree as
                # well as the path itself.
                result.removed += self.library.forget([path])
                result.removed += self.library.forget_under(path)
                continue
            if not self.wanted(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            result.scanned += 1
            state = self.library.file_state(path)
            if self.library.is_current(state, st.st_mtime, st.st_size):
                result.skipped += 1
                continue
            meta = metadata.read(path)
            if meta is None:
                self._unreadable[path] = (st.st_mtime, st.st_size)
                result.errors.append(path)
                continue
            self._unreadable.pop(path, None)
            location = None
            if self.geocoder is not None and meta.latitude is not None:
                location = self.geocoder.lookup(meta.latitude, meta.longitude,
                                                cached_only=True)
            digest, held = self._check_hold(path, st, held_sizes)
            if held:
                result.held += 1
            batch.append((meta, st, location, state is None, digest, held))
        self._write_batch(batch, result)
        result.seconds = time.monotonic() - t0
        return result

    # -- geocoding ---------------------------------------------------------
    def backfill_locations(self, limit: int = 25,
                           progress: Callable[[int, str], None] | None = None) -> int:
        """Resolve place names for indexed photographs that still lack one.

        Runs separately from the scan so that a rate-limited lookup service
        never slows indexing down, and so that switching geocoding on after
        the library is already built still fills everything in.  Cached
        coordinates cost no request at all, so repeated passes over a library
        shot in a few places finish almost instantly.
        """
        if self.geocoder is None:
            return 0
        pending = self.library.locations_missing(limit)
        resolved = 0
        for file_id, lat, lon in pending:
            try:
                location = self.geocoder.lookup(lat, lon)
            except Exception as exc:  # pragma: no cover - network
                _log.debug("geocoding failed for %s: %s", file_id, exc)
                continue
            if location:
                self.library.set_location(file_id, location)
                resolved += 1
                if progress:
                    progress(resolved, location)
        return resolved

    async def backfill_locations_async(self, limit: int = 25) -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self.backfill_locations, limit)

    def resolve_location(self, lat: float | None, lon: float | None) -> str | None:
        """One lookup, for the picture that is about to go on screen."""
        if self.geocoder is None or lat is None or lon is None:
            return None
        try:
            return self.geocoder.lookup(lat, lon)
        except Exception as exc:  # pragma: no cover - network
            _log.debug("geocoding failed for %.4f,%.4f: %s", lat, lon, exc)
            return None

    # -- watching ----------------------------------------------------------
    def start_watching(self, on_change: Callable[[ScanResult], None]) -> bool:
        def handle(paths: set[str]) -> None:
            if InotifyWatcher.RESCAN_ALL in paths:
                # The kernel dropped events (a Syncthing or rsync burst filled
                # the inotify queue), so what changed is no longer knowable from
                # the events: anything less than a full scan silently loses
                # files until the next scheduled one.
                _log.info("inotify lost events; running a full scan instead")
                result = self.scan()
            else:
                extra: set[str] = set()
                for p in list(paths):
                    if os.path.isdir(p):
                        try:
                            extra.update(os.path.join(p, n) for n in os.listdir(p))
                        except OSError:
                            pass
                result = self.rescan_paths(paths | extra)
            if result.added or result.updated or result.removed:
                _log.info("library changed: %s", result.summary())
                on_change(result)

        self._watcher = InotifyWatcher(self.roots, handle, follow_links=self.follow_links)
        if not self._watcher.start():
            self._watcher = None
            return False
        return True

    def close(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        self._pool.shutdown(wait=False, cancel_futures=True)
