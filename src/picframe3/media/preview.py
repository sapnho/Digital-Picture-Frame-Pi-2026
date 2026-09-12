"""A JPEG of a photograph, small enough to hand to something else.

One renderer behind three consumers: the web interface's thumbnails, the
``/api/current`` endpoint, and the picture the MQTT bridge publishes for Home
Assistant.  They differ only in size and quality, and a single implementation
is what keeps a portrait photograph upright in all three -- ``exif_transpose``
is the kind of step that gets forgotten in a copy.
"""

from __future__ import annotations

import io
import logging

_log = logging.getLogger(__name__)


def render_preview(path: str, is_video: bool = False,
                   size: tuple[int, int] = (480, 480),
                   quality: int = 82) -> bytes | None:
    """A JPEG no larger than ``size``, or ``None`` if it cannot be made.

    Never raises: a broken file, a codec the Pi does not have, a video whose
    first frame will not decode -- all of them mean "no picture this time",
    which every caller can live with.
    """
    try:
        from PIL import Image, ImageOps

        if is_video:
            from .video import poster_frame

            image = poster_frame(path, size)
            if image is None:
                return None
        else:
            image = Image.open(path)
            image.draft("RGB", size)          # JPEG DCT scaling: much faster
            image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        image.thumbnail(size, Image.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception as exc:
        _log.debug("preview failed for %s: %s", path, exc)
        return None
