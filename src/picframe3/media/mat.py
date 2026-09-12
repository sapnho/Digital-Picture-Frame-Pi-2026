"""Passepartout ("mat") generation.

A photograph shown edge-to-edge on a screen reads as a screen.  The same
photograph inset in a mat reads as a framed print, which is the whole point of
the object sitting on a shelf.  ``picframe`` established this; here the mats
are generated procedurally instead of composited from shipped nine-patch PNGs,
so there is no resource folder to install, no ``ninepatch`` dependency, and
every edge scales cleanly to any panel size.

The style set covers everything picframe offered, and its names are accepted
as aliases so a migrated configuration keeps working:

===================  =========================================================
``float``            the print floating on the board
``float_shadow``     …with a drop shadow under it
``float_wrap``       …wrapped in a coloured band with a highlight
                     (picframe's ``float_color_wrap``)
``polaroid``         white board, deeper lower margin (``float_polaroid``)
``single``           one mat, flat opening
``single_bevel``     one mat, chamfered opening
``double``           two mats, flat openings (``double_flat``)
``double_bevel``     two mats, chamfered openings
===================  =========================================================

Colours are derived from the image so the mat belongs to the picture: the
dominant colour is found by k-means over the pixels — the same approach
picframe took, because an average turns a sunset with a blue sky into mud —
then its saturation is cut hard and its lightness pushed to museum-board
levels.
"""

from __future__ import annotations

import hashlib
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

_log = logging.getLogger(__name__)

STYLES = (
    "float", "float_shadow", "float_wrap", "polaroid",
    "single", "single_bevel", "double", "double_bevel",
)

#: Plain English for the settings page.  ``polaroid`` names its own quirk,
#: because a print sitting high on its board looks like a centring bug to
#: anyone who did not choose it deliberately.
STYLE_LABELS = {
    "float":        "Float — the print bare on the board",
    "float_shadow": "Float with a drop shadow",
    "float_wrap":   "Float in a coloured band",
    "polaroid":     "Polaroid — deliberately low on the board, deep lower margin",
    "single":       "Single mat",
    "single_bevel": "Single mat, chamfered opening",
    "double":       "Double mat",
    "double_bevel": "Double mat, chamfered openings",
}

#: picframe's names, so a migrated config keeps working.
ALIASES = {
    "float_polaroid": "polaroid",
    "float_color_wrap": "float_wrap",
    "double_flat": "double",
    "bevel": "double_bevel",
    "single_flat": "single",
}

RGB = tuple[int, int, int]


@dataclass
class MatStyle:
    style: str = "single"
    outer_color: RGB | None = None
    inner_color: RGB | None = None
    outer_border: int = 75
    inner_border: int = 22
    texture: bool = True
    #: Override the shipped board scan with your own greyscale image.
    texture_file: str | None = None
    #: The inner mat is usually a smooth card against the textured outer board.
    inner_texture: bool = False
    #: When False the inner mat is a darker shade of the outer one rather than
    #: its own colour drawn from the photograph.
    auto_inner_color: bool = True
    shadow: bool = True
    bevel_width: int = 5
    #: Mat only when the image and the screen disagree about shape by more
    #: than this (0 = always mat, 1 = never, -1 = always).
    tolerance: float = 0.01

    def resolved_style(self, rng: random.Random) -> str:
        """Pick the style for one picture.

        Accepts a single name, ``random``/empty for any, or -- as picframe
        did -- several names separated by spaces or commas, meaning "choose
        from these".
        """
        raw = (self.style or "").strip().lower().replace(",", " ")
        if raw in ("", "random", "any", "all"):
            return rng.choice(STYLES)
        wanted: list[str] = []
        for token in raw.split():
            name = ALIASES.get(token, token)
            if name in STYLES:
                wanted.append(name)
            else:
                _log.warning("unknown mat style %r; ignoring it", token)
        if not wanted:
            _log.warning("no usable mat style in %r; using 'single'", self.style)
            return "single"
        return wanted[0] if len(wanted) == 1 else rng.choice(wanted)


# --------------------------------------------------------------------------
# Colour
# --------------------------------------------------------------------------

def dominant_color(image: Image.Image, samples: int = 100, clusters: int = 3) -> RGB:
    """The photograph's leading colour, by k-means -- picframe's method, kept.

    Two details matter, and both were wrong in the first version of this file.

    *Which* cluster: the centroids are ranked by **saturation**, not by how many
    pixels they own, and the most saturated wins.  A photograph that is
    four-fifths grey sky and one-fifth red jacket should give a red mat; rank by
    size and you get the sky.

    *How much of it*: the colour is used **raw**.  Desaturating it towards a
    pale museum board -- what this did before -- throws away the thing the
    extraction was for, and every photograph ends up with the same off-white
    mat.
    """
    small = image.convert("RGB").copy()
    small.thumbnail((samples, samples))
    data = np.asarray(small, dtype=np.float64).reshape(-1, 3)
    if len(data) == 0:
        return (128, 128, 128)

    # Seeded, so the same photograph always produces the same mat.
    rng = np.random.default_rng(20260912)
    centroids = data[rng.choice(len(data), size=min(clusters, len(data)),
                                replace=False)].copy()
    for _ in range(10):
        distances = ((data[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2)
        nearest = distances.argmin(axis=1)
        moved = 0.0
        for k in range(len(centroids)):
            members = data[nearest == k]
            if len(members):
                centre = members.mean(axis=0)
                moved = max(moved, float(np.linalg.norm(centre - centroids[k])))
                centroids[k] = centre
        if moved < 5.0:
            break

    saturation = centroids.max(axis=1) - centroids.min(axis=1)
    best = centroids[int(saturation.argmax())]
    return tuple(int(round(c)) for c in np.clip(best, 0, 255))  # type: ignore[return-value]


def _scale(color: RGB, factor: float) -> RGB:
    return tuple(int(round(min(255, max(0, c * factor)))) for c in color)  # type: ignore[return-value]


def auto_colors(image: Image.Image, *, auto_inner: bool = True) -> tuple[RGB, RGB]:
    """(outer, inner): the photograph's colour, and half of it for the core.

    Half is picframe's ``__get_darker_shade(colour, 0.5)`` -- a literal halving
    of the RGB values, which keeps the hue and reads as the same board in
    shadow.
    """
    outer = dominant_color(image)
    inner = _scale(outer, 0.5 if auto_inner else 0.35)
    return outer, inner


# --------------------------------------------------------------------------
# Texture
# --------------------------------------------------------------------------

#: The board scan picframe shipped, kept because it is the look people
#: recognise.  It is a luminance ramp, not a picture: ``ImageOps.colorize``
#: tints it from black to the mat colour, and that gradient is what gives the
#: board depth instead of a flat fill.  Stored greyscale at 1440p rather than
#: the original 4000x2250 RGB — the other two channels carried nothing, and the
#: difference on a 1080p panel measures under half a percent.
TEXTURE_FILE = Path(__file__).resolve().parents[1] / "data" / "mat_texture.jpg"

_texture_cache: dict[tuple[str, int, int], Image.Image] = {}


def board_texture(size: tuple[int, int],
                  path: str | None = None) -> Image.Image | None:
    """The board's luminance map at ``size``, or None if it cannot be read."""
    source = Path(path).expanduser() if path else TEXTURE_FILE
    key = (str(source), size[0], size[1])
    cached = _texture_cache.get(key)
    if cached is not None:
        return cached
    try:
        with Image.open(source) as handle:
            texture = handle.convert("L").resize(size, Image.BICUBIC)
    except (OSError, ValueError) as exc:
        _log.info("mat texture %s unavailable (%s); falling back to generated grain",
                  source, exc)
        return None
    if len(_texture_cache) > 4:
        _texture_cache.clear()
    _texture_cache[key] = texture
    return texture


_grain_cache: dict[tuple[int, int, int], Image.Image] = {}


def paper_texture(size: tuple[int, int], seed: int = 0) -> Image.Image:
    """Museum-board grain: fine noise plus faint fibres, as a greyscale mask."""
    key = (size[0], size[1], seed)
    cached = _grain_cache.get(key)
    if cached is not None:
        return cached
    rng = np.random.default_rng(seed)
    w, h = size
    small = (max(8, w // 3), max(8, h // 3))
    grain = rng.normal(0.5, 0.055, (small[1], small[0])).astype(np.float32)
    fibre = rng.normal(0.5, 0.030, (small[1], 1)).astype(np.float32)
    field = np.clip(grain * 0.72 + fibre * 0.28, 0.3, 0.7)
    field = (field - field.min()) / max(float(np.ptp(field)), 1e-6)
    img = Image.fromarray((field * 255).astype(np.uint8), "L")
    img = img.resize(size, Image.BICUBIC).filter(ImageFilter.GaussianBlur(0.4))
    if len(_grain_cache) > 6:
        _grain_cache.clear()
    _grain_cache[key] = img
    return img


def _board(size: tuple[int, int], color: RGB, textured: bool, seed: int,
           texture_file: str | None = None) -> Image.Image:
    """A sheet of mat board in ``color``, filling ``size``."""
    if not textured:
        return Image.new("RGB", size, color)

    texture = board_texture(size, texture_file)
    if texture is not None:
        # The scan runs from black to the mat colour, so the paper's own
        # unevenness shades the board.  This is picframe's step, and it is what
        # stops the mat reading as a flat rectangle of paint.
        return ImageOps.colorize(texture, black="black", white=color)

    # No texture file: generate grain instead, so a stripped-down install
    # still gets a board rather than a poster-paint fill.
    grain = paper_texture(size, seed)
    arr = np.asarray(Image.new("RGB", size, color), dtype=np.float32)
    g = (np.asarray(grain, dtype=np.float32) / 255.0 - 0.5)[..., None]
    arr = np.clip(arr * (1.0 + g * 0.16) + g * 6.0, 0, 255)
    return Image.fromarray(arr.astype(np.uint8), "RGB")


# --------------------------------------------------------------------------
# Edges
# --------------------------------------------------------------------------

def _drop_shadow(canvas: Image.Image, box: tuple[int, int, int, int],
                 radius: int = 14, opacity: int = 110, offset: int = 3) -> None:
    """Soft shadow under the print, so it sits above the board."""
    x0, y0, x1, y1 = box
    pad = radius * 3
    layer = Image.new("L", (x1 - x0 + pad * 2, y1 - y0 + pad * 2), 0)
    ImageDraw.Draw(layer).rectangle([pad, pad, layer.width - pad, layer.height - pad],
                                    fill=opacity)
    layer = layer.filter(ImageFilter.GaussianBlur(radius))
    shadow = Image.new("RGB", layer.size, (0, 0, 0))
    canvas.paste(shadow, (x0 - pad, y0 - pad + offset), layer)


def _bevel(canvas: Image.Image, box: tuple[int, int, int, int], width: int,
           color: RGB) -> None:
    """A chamfered mat opening: the cut edge of the board, catching the light.

    Drawn as four shaded trapezoids rather than a stretched nine-patch, so the
    angle stays correct at any size and the colour always belongs to the mat it
    is cut from.  Light is assumed to come from the top left, as it does in
    every real room with a window.
    """
    if width <= 0:
        return
    x0, y0, x1, y1 = box
    draw = ImageDraw.Draw(canvas)
    faces = (
        ([(x0, y0), (x1, y0), (x1 - width, y0 + width), (x0 + width, y0 + width)], 1.22),
        ([(x0, y0), (x0 + width, y0 + width), (x0 + width, y1 - width), (x0, y1)], 1.12),
        ([(x1, y0), (x1, y1), (x1 - width, y1 - width), (x1 - width, y0 + width)], 0.86),
        ([(x0, y1), (x0 + width, y1 - width), (x1 - width, y1 - width), (x1, y1)], 0.74),
    )
    for points, factor in faces:
        draw.polygon(points, fill=_scale(color, factor))


def _inner_shadow(canvas: Image.Image, box: tuple[int, int, int, int],
                  radius: int = 6, opacity: int = 90) -> None:
    """The shadow the mat casts onto the print just inside the opening."""
    x0, y0, x1, y1 = box
    if x1 - x0 < 4 or y1 - y0 < 4:
        return
    mask = Image.new("L", (x1 - x0, y1 - y0), opacity)
    inset = max(2, radius * 2)
    ImageDraw.Draw(mask).rectangle([inset, inset, mask.width - inset, mask.height - inset],
                                   fill=0)
    mask = mask.filter(ImageFilter.GaussianBlur(radius))
    canvas.paste(Image.new("RGB", mask.size, (0, 0, 0)), (x0, y0), mask)


def _hairline(canvas: Image.Image, box: tuple[int, int, int, int], color: RGB) -> None:
    """A one-pixel outline so a light print does not bleed into a light mat."""
    ImageDraw.Draw(canvas).rectangle(list(box), outline=_scale(color, 0.55), width=1)


def _highlight(size: tuple[int, int], strength: float = 0.20) -> Image.Image:
    """A diagonal sheen, as if a sheet of glass were over the wrap."""
    w, h = size
    gx = np.linspace(1.0, 0.0, max(w, 2), dtype=np.float32)[None, :]
    gy = np.linspace(1.0, 0.0, max(h, 2), dtype=np.float32)[:, None]
    field = np.clip((gx + gy) / 2.0, 0, 1) ** 2.2
    alpha = (field * strength * 255).astype(np.uint8)
    layer = np.zeros((h, w, 4), dtype=np.uint8)
    layer[..., :3] = 255
    layer[..., 3] = alpha
    return Image.fromarray(layer, "RGBA")


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def should_mat(image_size: tuple[int, int], screen_size: tuple[int, int],
               tolerance: float) -> bool:
    """Mat when leaving the picture uncropped would waste screen area."""
    if tolerance < 0:
        return True
    si = image_size[0] / max(image_size[1], 1)
    ss = screen_size[0] / max(screen_size[1], 1)
    diff = 1 - (si / ss if ss > si else ss / si)
    return diff > tolerance


def apply(
    images: Sequence[Image.Image],
    screen_size: tuple[int, int],
    style: MatStyle,
    *,
    seed: str | None = None,
) -> Image.Image:
    """Compose one or two images onto a mat filling ``screen_size``."""
    sw, sh = screen_size
    rng = random.Random(seed or "picframe3")
    chosen = style.resolved_style(rng)

    outer = style.outer_color
    inner = style.inner_color
    if outer is None or inner is None:
        auto_outer, auto_inner = auto_colors(images[0],
                                             auto_inner=style.auto_inner_color)
        outer = outer or auto_outer
        inner = inner or auto_inner
    if chosen == "polaroid":
        outer = style.outer_color or (248, 246, 242)

    unit = min(sw, sh) / 1080.0
    grain_seed = int(hashlib.sha1((seed or "0").encode()).hexdigest()[:8], 16) % 10_000
    canvas = _board((sw, sh), outer, style.texture, grain_seed, style.texture_file)

    border = max(8, int(style.outer_border * unit))
    bevel = max(2, int(style.bevel_width * unit)) if chosen.endswith("bevel") else 0
    double = chosen.startswith("double")
    wrap = chosen == "float_wrap"
    core = max(3, int(style.inner_border * unit)) if (double or wrap) else 0
    floating = chosen.startswith("float") or chosen == "polaroid"
    shadow = style.shadow and (floating or wrap)

    if chosen == "float":
        shadow = False
    if chosen == "polaroid":
        border = max(border, int(min(sw, sh) * 0.055))

    gap = max(10, int(min(sw, sh) * 0.02)) if len(images) > 1 else 0
    frame = border + core + bevel * (2 if double else 1 if bevel else 0)

    avail_w = sw - 2 * frame - gap * (len(images) - 1)
    avail_h = sh - 2 * frame
    if chosen == "polaroid":
        avail_h -= int(border * 1.5)              # deeper lower edge
    if avail_w <= 32 or avail_h <= 32:            # tiny panel: drop the frame
        border = core = bevel = frame = 0
        avail_w, avail_h = sw, sh

    per_w = max(1, avail_w // len(images))
    scaled: list[Image.Image] = []
    for im in images:
        factor = min(per_w / im.width, avail_h / im.height)
        scaled.append(im.resize((max(1, int(im.width * factor)),
                                 max(1, int(im.height * factor))), Image.LANCZOS))
    total_w = sum(s.width for s in scaled) + gap * (len(scaled) - 1)
    max_h = max(s.height for s in scaled)

    x = (sw - total_w) // 2
    y = (sh - max_h) // 2
    if chosen == "polaroid":
        y = frame
    placements = []
    for s in scaled:
        placements.append((s, (x, y + (max_h - s.height) // 2)))
        x += s.width + gap

    inner_board: Image.Image | None = None
    if core and (double or wrap):
        inner_board = _board((sw, sh), inner, style.texture and style.inner_texture,
                             grain_seed + 7, style.texture_file)

    for img, pos in placements:
        photo_box = (pos[0], pos[1], pos[0] + img.width, pos[1] + img.height)
        core_box = (photo_box[0] - bevel - core, photo_box[1] - bevel - core,
                    photo_box[2] + bevel + core, photo_box[3] + bevel + core)

        if shadow:
            _drop_shadow(canvas, core_box if wrap else photo_box,
                         radius=max(6, int(min(sw, sh) * 0.012)))

        if core and inner_board is not None:
            # Reveal the inner board only inside the core opening.
            mask = Image.new("L", (sw, sh), 0)
            ImageDraw.Draw(mask).rectangle(list(core_box), fill=255)
            canvas.paste(inner_board, (0, 0), mask)
            if bevel:
                _bevel(canvas, core_box, bevel, inner)
            if wrap:
                sheen = _highlight((core_box[2] - core_box[0], core_box[3] - core_box[1]))
                canvas.paste(Image.new("RGB", sheen.size, (255, 255, 255)),
                             (core_box[0], core_box[1]), sheen)

        if bevel:
            outer_box = (photo_box[0] - bevel, photo_box[1] - bevel,
                         photo_box[2] + bevel, photo_box[3] + bevel)
            _bevel(canvas, outer_box, bevel, inner if core else outer)

        canvas.paste(img, pos)
        if not floating:
            _inner_shadow(canvas, photo_box, radius=max(3, int(6 * unit)))
        _hairline(canvas, (photo_box[0] - 1, photo_box[1] - 1,
                           photo_box[2], photo_box[3]), inner)

    return canvas.convert("RGB")
