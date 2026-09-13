"""A smaller copy of a picture that is too large to decode on the render path.

The decode ceiling in :mod:`picframe3.media.prepare` exists because a 200
megapixel flatbed scan is 600 MB of RGB on a machine with 4 GB and a 4K texture
already resident, and the kernel does not kill the scan, it kills us.  But
refusing the picture is a poor answer to somebody who put it in the folder on
purpose: the photograph is fine, the frame simply cannot open it the way it
opens the others.

So it is opened once, in a child process, and a panel-sized copy is kept.  From
then on the frame shows that copy and never touches the original again.

Three things make the one dangerous decode safe:

* **A child process.**  If the decode is going to exhaust memory it takes the
  child with it, and the frame carries on with the next picture.  In-process,
  the OOM killer picks the biggest thing on the Pi, which is the frame.
* **A hard address-space limit**, sized from the picture's own header -- known
  before anything is decoded, because a header read is not a decode.  A file
  too big for even that ceiling is refused outright rather than attempted.
* **A timeout, and a marker when it fails.**  Without the marker the frame
  would stall for minutes on the same picture every time it came round.

The copy lives outside the photograph's folder on purpose.  Writing it beside
the original would put a file the owner did not create into a folder Syncthing
is watching, and it would come back on every device they own.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger(__name__)

#: Where the reduced copies go.  Not a setting: it is a cache, it is safe to
#: delete, and it belongs with the frame's other state rather than in the
#: pictures.
CACHE_DIR = Path("~/.cache/picframe3/oversize").expanduser()

#: The most address space the child may claim.  Above this the picture is
#: refused rather than attempted -- at a gigapixel there is no size of Pi that
#: makes this a good idea.
MEMORY_CEILING = 2 * 1024**3

#: Decode plus resample needs about three copies of the raw pixels, and the
#: interpreter, Pillow and its codecs want their own space on top.
BYTES_PER_PIXEL = 3 * 3
INTERPRETER_OVERHEAD = 512 * 1024**2

#: Long enough for a very large scan on a Pi 4, short enough that a wedged
#: decode does not hold the slideshow past the point anybody would wait.
TIMEOUT_SECONDS = 240.0

#: Written when a shrink fails, so the next attempt is instantaneous rather
#: than another four minutes.  Deleting the cache folder clears it.
FAILED_SUFFIX = ".failed"


def _key(path: str, size: tuple[int, int], target: tuple[int, int]) -> str:
    """Identify source *and* target: a picture recut for a different panel is
    a different copy, and a file replaced in place is a different picture."""
    try:
        stat = os.stat(path)
        stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        stamp = "?"
    raw = f"{os.path.realpath(path)}|{stamp}|{size[0]}x{size[1]}|{target[0]}x{target[1]}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def budget(pixels: int) -> int:
    """Address space one decode of this many pixels needs."""
    return pixels * BYTES_PER_PIXEL + INTERPRETER_OVERHEAD


def cached_copy(path: str, source_size: tuple[int, int], target: tuple[int, int],
                *, cache_dir: Path | None = None,
                timeout: float = TIMEOUT_SECONDS) -> str | None:
    """Path to a panel-sized copy of *path*, making it if it does not exist.

    ``None`` means the frame should go on refusing this picture: either the
    decode is beyond the ceiling, or it has already been tried and failed.
    """
    folder = Path(cache_dir) if cache_dir is not None else CACHE_DIR
    name = _key(path, source_size, target)
    copy = folder / f"{name}.jpg"
    if copy.exists():
        return str(copy)
    failed = folder / f"{name}{FAILED_SUFFIX}"
    if failed.exists():
        return None

    needed = budget(source_size[0] * source_size[1])
    if needed > MEMORY_CEILING:
        _log.warning(
            "%s is %d×%d: too large to open even once, so it stays out of "
            "the slideshow", path, source_size[0], source_size[1])
        _mark_failed(failed)
        return None

    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log.warning("cannot write the reduced-copy cache at %s: %s", folder, exc)
        return None

    _log.info("%s is %d×%d; making a %d×%d copy once so it can be shown",
              path, source_size[0], source_size[1], target[0], target[1])
    partial = copy.with_suffix(".part")
    command = [sys.executable, "-m", __name__, path, str(partial),
               str(target[0]), str(target[1]), str(needed)]
    try:
        done = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        _log.warning("gave up shrinking %s after %.0f seconds", path, timeout)
        partial.unlink(missing_ok=True)
        _mark_failed(failed)
        return None
    if done.returncode != 0 or not partial.exists():
        detail = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        _log.warning("could not shrink %s: %s", path,
                     detail[-1] if detail else f"exit {done.returncode}")
        partial.unlink(missing_ok=True)
        _mark_failed(failed)
        return None
    # Renamed into place only once it is whole, so a copy that is in the cache
    # is always a copy that can be opened.
    partial.replace(copy)
    return str(copy)


def _mark_failed(marker: Path) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:                                  # pragma: no cover - disk
        pass


def _child(source: str, destination: str, width: int, height: int,
           limit: int) -> int:
    """The decode itself.  Runs as ``python -m picframe3.media.shrink``."""
    import warnings

    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception:
        # No RLIMIT here (or not enforceable).  The timeout in the parent is
        # then the only guard, which is weaker but still not nothing.
        pass

    from PIL import Image, ImageOps

    from . import prepare as prepare_module  # registers HEIF/AVIF openers

    # The frame's own decompression-bomb guard is what sent this picture here;
    # inside the child the address-space limit is the real ceiling, and it is
    # enforced by the kernel rather than by a pixel count.
    Image.MAX_IMAGE_PIXELS = None
    warnings.simplefilter("ignore", Image.DecompressionBombWarning)
    assert prepare_module is not None

    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        # thumbnail() reduces in place and, on a JPEG, in DCT steps first.
        image.thumbnail((width, height), Image.LANCZOS, reducing_gap=2.0)
        # Saved without the EXIF block: the orientation is baked in above, and
        # a second application of it by the reader would turn the picture.
        image.save(destination, "JPEG", quality=92, subsampling=0, optimize=True)
    return 0


if __name__ == "__main__":                          # pragma: no cover - child
    _source, _destination, _w, _h, _limit = sys.argv[1:6]
    sys.exit(_child(_source, _destination, int(_w), int(_h), int(_limit)))
