"""Walks the picture folders and keeps the index in step with the disk."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from ..media import metadata
from ..media.geocode import Geocoder
from .db import Library
from .watch import InotifyWatcher

_log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    scanned: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    geocoded: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.scanned} files in {self.seconds:.1f}s "
                f"(+{self.added} new, ~{self.updated} changed, -{self.removed} gone)")


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
    ):
        self.library = library
        self.roots = [os.path.abspath(os.path.expanduser(r)) for r in roots]
        self.follow_links = follow_links
        self.geocoder = geocoder
        self.include_videos = include_videos
        self.ignore_hidden = ignore_hidden
        self.exclude = [e for e in exclude if e]
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="scan"
        )
        self._watcher: InotifyWatcher | None = None
        self._scanning = False

    # -- walking -----------------------------------------------------------
    def iter_files(self) -> Iterable[tuple[str, os.stat_result]]:
        for root in self.roots:
            if not os.path.isdir(root):
                _log.warning("picture folder does not exist: %s", root)
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=self.follow_links):
                if self.ignore_hidden:
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                dirnames[:] = [d for d in dirnames
                               if not any(x in os.path.join(dirpath, d) for x in self.exclude)]
                for name in filenames:
                    if self.ignore_hidden and name.startswith("."):
                        continue
                    path = os.path.join(dirpath, name)
                    if not metadata.is_supported(path):
                        continue
                    if not self.include_videos and metadata.is_video(path):
                        continue
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    if st.st_size == 0:
                        continue
                    yield path, st

    # -- scanning ----------------------------------------------------------
    def scan(self, *, prune: bool = True,
             progress: Callable[[int, str], None] | None = None) -> ScanResult:
        result = ScanResult()
        t0 = time.monotonic()
        self._scanning = True
        try:
            for path, st in self.iter_files():
                result.scanned += 1
                if progress and result.scanned % 200 == 0:
                    progress(result.scanned, path)
                existing = self.library.by_path(path)
                if existing is not None and not self.library.needs_reindex(
                    path, st.st_mtime, st.st_size
                ):
                    result.skipped += 1
                    continue
                meta = metadata.read(path)
                if meta is None:
                    result.errors.append(path)
                    continue
                location = None
                if self.geocoder is not None and meta.latitude is not None:
                    # Only if it is already cached -- a cold lookup waits for
                    # the backfill pass rather than holding up the scan.
                    location = self.geocoder.lookup(meta.latitude, meta.longitude,
                                                    cached_only=True)
                    if location:
                        result.geocoded += 1
                try:
                    self.library.upsert(meta, mtime=st.st_mtime, size=st.st_size,
                                        location=location)
                except Exception as exc:  # pragma: no cover - db level
                    _log.warning("could not index %s: %s", path, exc)
                    result.errors.append(path)
                    continue
                if existing is None:
                    result.added += 1
                else:
                    result.updated += 1
            if prune:
                result.removed = self.library.prune_missing(self.roots)
        finally:
            self._scanning = False
        result.seconds = time.monotonic() - t0
        self.library.set_state("last_scan", time.time())
        _log.info("scan complete: %s", result.summary())
        return result

    async def scan_async(self, **kwargs) -> ScanResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, lambda: self.scan(**kwargs))

    def rescan_paths(self, paths: Iterable[str]) -> ScanResult:
        """Reindex a specific set of paths, e.g. after an inotify batch."""
        result = ScanResult()
        t0 = time.monotonic()
        for path in paths:
            if os.path.isdir(path):
                continue
            if not metadata.is_supported(path):
                continue
            if not os.path.exists(path):
                result.removed += self.library.forget([path])
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            result.scanned += 1
            existing = self.library.by_path(path)
            if existing is not None and not self.library.needs_reindex(
                path, st.st_mtime, st.st_size
            ):
                result.skipped += 1
                continue
            meta = metadata.read(path)
            if meta is None:
                result.errors.append(path)
                continue
            location = None
            if self.geocoder is not None and meta.latitude is not None:
                location = self.geocoder.lookup(meta.latitude, meta.longitude,
                                                cached_only=True)
            self.library.upsert(meta, mtime=st.st_mtime, size=st.st_size, location=location)
            if existing is None:
                result.added += 1
            else:
                result.updated += 1
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
