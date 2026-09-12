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
#: The kernel's "I threw events away" event.  It arrives with wd == -1 and no
#: name, so it looks like an event for an unknown watch and used to be skipped
#: with them -- which is exactly the case where skipping loses files.
IN_Q_OVERFLOW = 0x00004000

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

    #: Put into the changed set instead of a path when the kernel's event queue
    #: overflowed.  It is not a path and never will be one: a receiver that
    #: passes it to the filesystem gets "does not exist", so the worst an
    #: unaware caller can do is ignore it.  A caller that knows what it means
    #: must fall back to a full scan -- after an overflow the events that would
    #: have named the new files are simply gone.
    RESCAN_ALL = "\x00inotify-overflow"

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
            try:
                added += self._add_tree(root)
            except OSError as exc:
                # One unreadable or vanished folder is not a reason to give up
                # watching the others.
                _log.warning("cannot watch %s: %s", root, exc)
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
        # With follow_links a symlink back to a parent directory is an infinite
        # walk, and here it also spends a kernel watch on every turn of the
        # loop until the per-user limit is exhausted and nothing is watched at
        # all.  Directories are remembered by device and inode, which is the
        # only identity a symlink cannot disguise.
        seen: set[tuple[int, int]] = set()
        for dirpath, dirnames, _ in os.walk(root, followlinks=self.follow_links):
            if self.follow_links and not self._first_visit(dirpath, seen):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if self._add(dirpath):
                count += 1
        return count

    @staticmethod
    def _first_visit(path: str, seen: set[tuple[int, int]]) -> bool:
        try:
            st = os.stat(path)
        except OSError:
            return False
        key = (st.st_dev, st.st_ino)
        if key in seen:
            _log.warning("not watching %s: already seen (symlink loop?)", path)
            return False
        seen.add(key)
        return True

    def _add(self, path: str) -> bool:
        try:
            # os.fsencode, not str.encode: a filename that is not valid UTF-8
            # reaches Python with surrogate escapes, and encoding those raises
            # UnicodeEncodeError.  That exception used to escape through
            # _add_tree() and kill the watcher thread outright, so one oddly
            # named folder somewhere in the library stopped the frame noticing
            # any new pictures at all, silently.
            raw = os.fsencode(path)
        except (UnicodeEncodeError, ValueError) as exc:  # pragma: no cover - exotic
            _log.warning("cannot watch %r: %s", path, exc)
            return False
        wd = self._libc.inotify_add_watch(self._fd, raw, WATCH_MASK | IN_ONLYDIR)
        if wd < 0:
            err = ctypes.get_errno()
            if err == errno.ENOSPC:
                _log.warning(
                    "inotify watch limit reached; raise fs.inotify.max_user_watches "
                    "to watch the whole library"
                )
            else:
                _log.debug("cannot watch %s: %s", path, os.strerror(err))
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
            if mask & IN_Q_OVERFLOW:
                _log.warning("inotify queue overflowed; events were lost, "
                             "asking for a full rescan")
                changed.add(self.RESCAN_ALL)
                continue
            name = os.fsdecode(raw.split(b"\0", 1)[0])
            base = self._wd_to_path.get(wd)
            if base is None:
                continue
            full = os.path.join(base, name) if name else base
            if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO):
                try:
                    self._add_tree(full)      # pick up newly created subtrees
                except OSError as exc:
                    _log.debug("cannot watch the new directory %s: %s", full, exc)
            changed.add(full)
        return changed
