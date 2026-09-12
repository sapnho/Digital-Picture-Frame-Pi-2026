"""Builders for the standard overlay layers: info bar, clock, notices.

Each returns a PIL RGBA image plus where to put it, which the renderer uploads
as a texture.  Kept separate from the renderer so the layouts can be unit
tested without a GL context.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from PIL import Image

from . import textstyle
from .textstyle import TextStyle

_log = logging.getLogger(__name__)

DEFAULT_SEPARATOR = "  ·  "

#: Every caption element picframe3 knows how to write, with the wording the
#: settings page uses.  The frame writes the chosen elements in the order the
#: user puts them in, which is why ``viewer.show_text`` is a list and not a
#: set of switches.
CAPTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("title",    "Title"),
    ("caption",  "Caption or description"),
    ("name",     "File name"),
    ("date",     "Date taken"),
    ("location", "Place"),
    ("folder",   "Folder"),
    ("camera",   "Camera"),
    ("exposure", "Exposure (f-stop, speed, ISO, focal length)"),
)

#: Just the names, for validation.
CAPTION_FIELD_NAMES = tuple(name for name, _ in CAPTION_FIELDS)


#: Where the clock goes when the configured position makes no sense.
DEFAULT_POSITION = "TR"


def normalise_position(position: str, default: str = DEFAULT_POSITION) -> str:
    """One of TL TC TR BL BC BR, whatever the configuration file said.

    The layout code indexes ``position[0]`` and ``position[1]``, so an empty
    string or a stray "top-right" from a hand-edited config used to take the
    draw path down with an IndexError -- several frames after the setting was
    changed, which makes it look like anything but a bad value.  Configuration
    is validated elsewhere; the draw path still has to survive what reaches it.
    """
    value = str(position or "").strip().upper()
    if len(value) == 2 and value[0] in "TB" and value[1] in "LCR":
        return value
    _log.warning("unusable overlay position %r; using %s", position, default)
    return default


@dataclass
class Placement:
    image: Image.Image
    x: int
    y: int


def info_bar(
    lines: Sequence[str] | Sequence[Sequence[str]],
    screen: tuple[int, int],
    style: TextStyle,
    *,
    scrim_fraction: float = 0.26,
    scrim_opacity: float = 0.55,
    separator: str | None = None,
) -> Placement | None:
    """Caption block anchored to the bottom of the screen, over a gradient.

    ``lines`` is either a list of strings (one caption across the full width)
    or a list of *two* such lists, in which case each caption is centred under
    its own half of a portrait pair -- two photographs side by side deserve two
    captions, not one describing only the left-hand picture.
    """
    columns = _as_columns(lines)
    joiner = DEFAULT_SEPARATOR if separator is None else separator
    texts = [joiner.join(t for t in col if t) for col in columns]
    if not any(texts):
        return None

    w, h = screen
    paired = len(texts) > 1
    column_w = (w // len(texts)) - style.margin_x
    bodies = [
        textstyle.render_text(text, style, max_width=max(80, column_w),
                              align="C" if paired else None) if text else None
        for text in texts
    ]
    rendered = [b for b in bodies if b is not None]
    if not rendered:
        return None

    band_h = min(h, max(b.height for b in rendered) + style.margin_y * 2)
    layer = Image.new("RGBA", (w, band_h), (0, 0, 0, 0))
    if scrim_opacity > 0:
        layer = Image.alpha_composite(
            layer,
            textstyle.scrim((w, band_h), height_fraction=1.0, opacity=scrim_opacity),
        )

    for index, body in enumerate(bodies):
        if body is None:
            continue
        if paired:
            centre = w * (2 * index + 1) // (2 * len(texts))
            x = centre - body.width // 2
        else:
            justify = style.justify.upper()
            if justify == "C":
                x = (w - body.width) // 2
            elif justify == "R":
                x = w - body.width - style.margin_x
            else:
                x = style.margin_x
        layer.alpha_composite(body, (max(0, min(x, w - body.width)),
                                     (band_h - body.height) // 2))
    return Placement(layer, 0, h - band_h)


def _as_columns(lines) -> list[list[str]]:
    items = list(lines or [])
    if items and all(isinstance(item, (list, tuple)) for item in items):
        return [list(col) for col in items][:2]
    return [[str(item) for item in items]]


def clock(
    screen: tuple[int, int],
    style: TextStyle,
    *,
    fmt: str = "%H:%M",
    extra: str = "",
    position: str = "TR",
    offset_pct: tuple[float, float] = (3.0, 3.0),
    now: float | None = None,
    extra_scale: float = 0.34,
) -> Placement | None:
    """Large time readout.  ``position`` is one of TL TC TR BL BC BR.

    ``extra`` is a second line — a weather summary, a countdown, whatever
    writes to the file — and it is set much smaller than the time.  Matching
    the clock's size, as picframe did, puts a sentence across the picture in
    120-point type.
    """
    position = normalise_position(position)
    align = _align_for(position)
    text = datetime.fromtimestamp(now or time.time()).strftime(fmt)
    w, h = screen
    face = textstyle.render_text(text, style, max_width=w, align=align)
    if face is None:
        return None

    body = face
    if extra:
        small = replace(style, size=max(11, int(style.size * extra_scale)))
        caption = textstyle.render_text(extra, small, max_width=int(w * 0.45),
                                        align=align)
        if caption is not None:
            gap = max(2, style.size // 12)
            width = max(face.width, caption.width)
            body = Image.new("RGBA", (width, face.height + gap + caption.height),
                             (0, 0, 0, 0))
            body.alpha_composite(face, (_offset(width, face.width, align), 0))
            body.alpha_composite(caption,
                                 (_offset(width, caption.width, align),
                                  face.height + gap))

    dx = int(w * offset_pct[0] / 100)
    dy = int(h * offset_pct[1] / 100)
    horiz, vert = position[1].upper(), position[0].upper()
    if horiz == "L":
        x = dx
    elif horiz == "C":
        x = (w - body.width) // 2
    else:
        x = w - body.width - dx
    y = dy if vert == "T" else h - body.height - dy
    return Placement(body, x, y)


def _offset(total: int, part: int, align: str) -> int:
    if align == "R":
        return total - part
    if align == "C":
        return (total - part) // 2
    return 0


def _align_for(position: str) -> str:
    return {"L": "L", "C": "C", "R": "R"}.get(normalise_position(position)[1], "L")


def notice(
    text: str,
    screen: tuple[int, int],
    style: TextStyle,
) -> Placement | None:
    """Transient centred message: PAUSED, display off, errors."""
    body = textstyle.render_text(text, style, max_width=int(screen[0] * 0.8), align="C")
    if body is None:
        return None
    return Placement(body, (screen[0] - body.width) // 2, (screen[1] - body.height) // 2)


def format_info_lines(
    meta,
    fields: Iterable[str],
    *,
    date_format: str = "%-d %B %Y",
    location: str | None = None,
    paused: bool = False,
) -> list[str]:
    """Pick and format the metadata fields the user asked to see."""
    import os

    out: list[str] = []
    for field_name in fields:
        value: str | None = None
        if field_name == "title":
            value = meta.title
        elif field_name == "caption":
            value = meta.caption
        elif field_name == "name":
            value = os.path.splitext(os.path.basename(meta.path))[0]
        elif field_name == "folder":
            value = os.path.basename(os.path.dirname(meta.path))
        elif field_name == "date" and meta.taken_at:
            try:
                value = datetime.fromtimestamp(meta.taken_at).strftime(date_format)
            except ValueError:  # %-d is not portable everywhere
                value = datetime.fromtimestamp(meta.taken_at).strftime("%d %B %Y")
        elif field_name == "location":
            value = location
        elif field_name == "camera":
            parts = [p for p in (meta.make, meta.model) if p]
            if parts:
                # "Canon Canon EOS R6" -> "Canon EOS R6"
                if len(parts) == 2 and parts[1].lower().startswith(parts[0].lower()):
                    parts = [parts[1]]
                value = " ".join(parts)
        elif field_name == "exposure":
            bits = []
            if meta.f_number:
                bits.append(f"f/{meta.f_number:g}")
            if meta.exposure_time:
                bits.append(meta.exposure_time)
            if meta.iso:
                bits.append(f"ISO {meta.iso}")
            if meta.focal_length:
                bits.append(f"{meta.focal_length:g}mm")
            value = " ".join(bits) or None
        if value:
            out.append(value.strip())
    if paused:
        out.append("PAUSED")
    return out
