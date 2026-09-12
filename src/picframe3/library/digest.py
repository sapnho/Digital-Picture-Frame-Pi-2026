"""What a picture *is*, independent of what it is called or where it lives.

A removal has to survive the picture coming back.  Syncthing re-copies it,
a backup is restored over the folder, the same photograph arrives again on a
USB stick under a different name -- and a frame that remembers removals by
path lets every one of those put it back on the wall.

So a removal is remembered by the bytes.  SHA-256 of the whole file: slow
enough that it must never run over a whole library, exact enough that "the
same photograph" means the same photograph and not "the same size, probably".

It runs in exactly two places, both rare:

* when a picture is removed -- one file, once;
* when a scan meets a file whose **size** matches one that was removed --
  and the size comes free with the ``stat`` the scan already does, so the
  hash is computed for a handful of candidates rather than for thousands of
  photographs that were never anywhere near the trash.
"""

from __future__ import annotations

import hashlib
import logging
import os

_log = logging.getLogger(__name__)

#: Read size.  Large enough that a 40-megapixel JPEG is a few reads, small
#: enough not to hold a megabyte-sized buffer per call on a frame with 1 GB.
CHUNK = 1 << 20


def file_digest(path: str) -> str | None:
    """SHA-256 of a file, or ``None`` if it cannot be read.

    Unreadable is not an error worth raising here: every caller's honest
    fallback is "then this is not a picture I can recognise", and a removal
    that cannot be hashed simply falls back to being remembered by path.
    """
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        _log.warning("cannot read %s to recognise it later: %s", path, exc)
        return None


def size_of(path: str) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0
