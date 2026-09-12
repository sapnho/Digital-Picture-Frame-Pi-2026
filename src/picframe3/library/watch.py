"""Filesystem watching via inotify, bound directly with ctypes.

A frame whose pictures arrive over Syncthing, Nextcloud or a Samba share should
show a new photo within seconds, not at the next scheduled walk.  ``picframe``
re-walked the tree on a timer; inotify makes that unnecessary and costs nothing
while idle.

Falls back cleanly: if inotify is unavailable or the watch limit is exhausted,
the caller keeps its periodic rescan.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import logging
import os
import select
import struct
import threading
from collections.abc import Callable, Iterable

_log = logging.getLogger(__name__)

IN_ACCESS = 0x00000001
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_ISDIR = 0x40000000
IN_ONLYDIR = 0x01000000

WATCH_MASK = (IN_CLOSE_WRITE | IN_MOVED_TO | IN_MOVED_FROM | IN_CREATE
              | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF)

IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000

_EVENT_HEADER = struct.Struct("iIII")


class InotifyWatcher:
    """Watches a set of directory trees and calls back when they change.

    The callback is coalesced by the caller: it fires per batch of events, not
    per file, because an rsync of a holiday folder produces thousands.
    """

    def __init__(self, roots: Iterable[str], on_change: Callable[[set[str]], None],
                 *, follow_links: bool = False, quiet_period: float = 2.0):
        self.roots = [os.path.abspath(os.path.expanduser(r)) for r in roots]
        self.on_change = on_change
        self.follow_links = follow_links
        self.quiet_period = quiet_period
        self._libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        self._fd = -1
        self._wd_to_path: dict[int, str] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> bool:
        if not hasattr(self._libc, "inotify_init1"):
            _log.info("inotify not available on this system; falling back to periodic scans")
            return False
        self._fd = self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self._fd < 0:
            _log.warning("inotify_init1 failed: %s", os.strerror(ctypes.get_errno()))
            return False
        added = 0
        for root in self.roots:
            added += self._add_tree(root)
        if added == 0:
            _log.warning("inotify added no watches; falling back to periodic scans")
            self.stop()
            return False
        _log.info("watching %d directories for changes", added)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="inotify", daemon=True)
        self._thread.start()
        return True

    def _add_tree(self, root: str) -> int:
        count = 0
        if not os.path.isdir(root):
            return 0
        for dirpath, dirnames, _ in os.walk(root, followlinks=self.follow_links):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if self._add(dirpath):
                count += 1
        return count

    def _add(self, path: str) -> bool:
        wd = self._libc.inotify_add_watch(self._fd, path.encode(), WATCH_MASK | IN_ONLYDIR)
        if wd < 0:
            err = ctypes.get_errno()
            if err == errno.ENOSPC:
                _log.warning(
                    "inotify watch limit reached; raise fs.inotify.max_user_watches "
                    "to watch the whole library"
                )
            return False
        self._wd_to_path[wd] = path
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        self._wd_to_path.clear()

    # -- loop --------------------------------------------------------------
    def _run(self) -> None:
        pending: set[str] = set()
        while not self._stop.is_set():
            timeout = self.quiet_period if pending else 1.0
            ready, _, _ = select.select([self._fd], [], [], timeout)
            if ready:
                pending |= self._drain()
                continue
            if pending:
                changed, pending = pending, set()
                try:
                    self.on_change(changed)
                except Exception:  # pragma: no cover - user callback
                    _log.exception("filesystem change handler failed")

    def _drain(self) -> set[str]:
        changed: set[str] = set()
        try:
            data = os.read(self._fd, 64 * 1024)
        except BlockingIOError:
            return changed
        except OSError as exc:  # pragma: no cover
            _log.debug("inotify read failed: %s", exc)
            return changed
        offset = 0
        while offset + _EVENT_HEADER.size <= len(data):
            wd, mask, _cookie, length = _EVENT_HEADER.unpack_from(data, offset)
            offset += _EVENT_HEADER.size
            raw = data[offset:offset + length]
            offset += length
            name = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
            base = self._wd_to_path.get(wd)
            if base is None:
                continue
            full = os.path.join(base, name) if name else base
            if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO):
                self._add_tree(full)          # pick up newly created subtrees
            changed.add(full)
        return changed
