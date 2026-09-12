"""Text rendering for the on-screen overlays.

Text is drawn by Pillow into an RGBA image and uploaded as a texture, rather
than built from a glyph atlas in GL.  A picture frame redraws its caption once
per slide and its clock once per minute, so there is nothing to gain from a GPU
text pipeline -- and Pillow gives real hinting, kerning, and (with libraqm)
shaping for Arabic, Hebrew and Indic scripts, none of which pi3d's
``FixedString`` could do.
"""

from __future__ import annotations

import functools
import logging
import os
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_log = logging.getLogger(__name__)

#: Searched in order when no font is configured.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


@functools.lru_cache(maxsize=32)
def load_font(path: str | None, size: int) -> ImageFont.FreeTypeFont:
    candidates: list[str] = []
    if path:
        candidates.append(os.path.expanduser(path))
    candidates.extend(FONT_CANDIDATES)
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except (OSError, ValueError):
            continue
    _log.warning("no TrueType font found (looked in %s); falling back to the bitmap default",
                 ", ".join(candidates[:3]))
    return ImageFont.load_default(size=size)


def font_available() -> bool:
    return any(os.path.exists(p) for p in FONT_CANDIDATES)


@dataclass
class TextStyle:
    font_path: str | None = None
    size: int = 34
    color: tuple[int, int, int] = (255, 255, 255)
    opacity: float = 1.0
    shadow: bool = True
    shadow_radius: float = 3.0
    shadow_opacity: float = 0.75
    justify: str = "L"                      # L, C, R
    line_spacing: float = 1.25
    margin_x: int = 64
    margin_y: int = 40

    @property
    def font(self) -> ImageFont.FreeTypeFont:
        return load_font(self.font_path, self.size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        words = paragraph.split(" ")
        line = ""
        for word in words:
            probe = f"{line} {word}".strip()
            if draw.textlength(probe, font=font) <= max_width or not line:
                line = probe
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def render_text(
    text: str,
    style: TextStyle,
    max_width: int,
    *,
    align: str | None = None,
) -> Image.Image | None:
    """Render (wrapped, shadowed) text to a tight RGBA image."""
    if not text:
        return None
    font = style.font
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = _wrap(probe, text, font, max_width)
    if not lines:
        return None

    ascent, descent = font.getmetrics()
    line_h = int((ascent + descent) * style.line_spacing)
    width = max(1, int(max(probe.textlength(ln, font=font) for ln in lines)))
    pad = int(style.shadow_radius * 3) if style.shadow else 2
    img = Image.new("RGBA", (width + pad * 2, line_h * len(lines) + pad * 2), (0, 0, 0, 0))

    justify = (align or style.justify).upper()
    alpha = int(255 * max(0.0, min(1.0, style.opacity)))

    if style.shadow:
        mask = Image.new("L", img.size, 0)
        mdraw = ImageDraw.Draw(mask)
        _draw_lines(mdraw, lines, font, width, pad, line_h, justify, 255)
        mask = mask.filter(ImageFilter.GaussianBlur(style.shadow_radius))
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        shadow.putalpha(mask.point(lambda v: int(v * style.shadow_opacity)))
        img = Image.alpha_composite(img, shadow)

    draw = ImageDraw.Draw(img)
    _draw_lines(draw, lines, font, width, pad, line_h, justify,
                (*style.color, alpha))
    return img


def _draw_lines(draw, lines, font, width, pad, line_h, justify, fill) -> None:
    for i, line in enumerate(lines):
        lw = draw.textlength(line, font=font)
        if justify == "C":
            x = pad + (width - lw) / 2
        elif justify == "R":
            x = pad + (width - lw)
        else:
            x = pad
        draw.text((x, pad + i * line_h), line, font=font, fill=fill)


def scrim(size: tuple[int, int], *, height_fraction: float = 0.28,
          opacity: float = 0.55, from_bottom: bool = True) -> Image.Image:
    """A soft gradient behind text so captions stay readable over bright skies."""
    import numpy as np

    w, h = size
    band = max(1, int(h * height_fraction))
    ramp = np.linspace(0.0, 1.0, band, dtype=np.float32) ** 1.8
    if from_bottom:
        column = np.concatenate([np.zeros(h - band, dtype=np.float32), ramp])
    else:
        column = np.concatenate([ramp[::-1], np.zeros(h - band, dtype=np.float32)])
    alpha = (column * opacity * 255).astype("uint8")
    arr = np.zeros((h, w, 4), dtype="uint8")
    arr[..., 3] = alpha[:, None]
    return Image.fromarray(arr, "RGBA")
