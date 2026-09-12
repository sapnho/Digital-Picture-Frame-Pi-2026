"""Turn a file on disk into exactly one display-sized image, ready to upload.

All of the expensive, quality-sensitive work happens here on the CPU, once per
slide -- which on a picture frame is once every few minutes.  The GPU then only
has to run the transition.  That split is what keeps the frame at a steady
refresh on a Pi 4 while still using LANCZOS resampling and real Gaussian blur.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from PIL import Image, ImageFile, ImageFilter, ImageOps

from . import mat as mat_module
from .metadata import PhotoMeta

_log = logging.getLogger(__name__)

ImageFile.LOAD_TRUNCATED_IMAGES = True        # a damaged JPEG must not stall the show

FIT_MODES = ("cover", "contain", "blur", "mat", "auto")


@dataclass
class PrepareOptions:
    fit: str = "auto"
    background: tuple[int, int, int] = (0, 0, 0)
    blur_amount: float = 20.0
    blur_zoom: float = 1.06
    blur_dim: float = 0.55            # 0 = black edges, 1 = full-strength mirror
    mat_style: mat_module.MatStyle = None  # type: ignore[assignment]
    portrait_pairs: bool = False
    kenburns_headroom: float = 1.0    # render larger than the screen for panning
    upscale_limit: float = 2.5        # never enlarge a small image more than this

    def __post_init__(self) -> None:
        if self.mat_style is None:
            self.mat_style = mat_module.MatStyle()


def _register_extra_formats() -> None:
    """HEIF/AVIF from iPhones, and RAW if the user installed the optional dep."""
    try:
        import pillow_heif  # type: ignore

        pillow_heif.register_heif_opener()
        try:
            pillow_heif.register_avif_opener()
        except Exception:
            pass
    except ImportError:
        _log.debug("pillow-heif not installed; .heic/.heif files will be skipped")


_register_extra_formats()


def open_oriented(path: str) -> Image.Image | None:
    """Open an image and bake in its EXIF orientation."""
    try:
        img = Image.open(path)
        img.load()
    except Exception as exc:
        _log.warning("cannot open %s: %s", path, exc)
        return None
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return img


def _fit_cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(img, size, method=Image.LANCZOS, centering=(0.5, 0.42))


def _fit_contain(img: Image.Image, size: tuple[int, int],
                 background: tuple[int, int, int]) -> Image.Image:
    canvas = Image.new("RGB", size, background)
    scaled = ImageOps.contain(img, size, method=Image.LANCZOS)
    canvas.paste(scaled, ((size[0] - scaled.width) // 2, (size[1] - scaled.height) // 2))
    return canvas


def _fit_blur(img: Image.Image, size: tuple[int, int], opts: PrepareOptions) -> Image.Image:
    """Letterbox against a blurred, zoomed copy of the picture itself."""
    # Blur at a small size and enlarge: visually identical to a huge-radius
    # Gaussian, and roughly two orders of magnitude cheaper.
    small_w = 96
    small_h = max(1, round(small_w * size[1] / size[0]))
    back = ImageOps.fit(img, (small_w, small_h), method=Image.BILINEAR)
    back = back.filter(ImageFilter.GaussianBlur(opts.blur_amount / 8.0))
    back = back.resize(size, Image.BICUBIC)
    if opts.blur_zoom > 1.0:
        zw, zh = int(size[0] * opts.blur_zoom), int(size[1] * opts.blur_zoom)
        back = back.resize((zw, zh), Image.BICUBIC).crop(
            ((zw - size[0]) // 2, (zh - size[1]) // 2,
             (zw - size[0]) // 2 + size[0], (zh - size[1]) // 2 + size[1])
        )
    if opts.blur_dim < 1.0:
        dark = Image.new("RGB", size, (0, 0, 0))
        back = Image.blend(dark, back, max(0.0, min(1.0, opts.blur_dim)))
    scaled = ImageOps.contain(img, size, method=Image.LANCZOS)
    back.paste(scaled, ((size[0] - scaled.width) // 2, (size[1] - scaled.height) // 2))
    return back


def pair_portraits(a: Image.Image, b: Image.Image, gap: int = 12,
                   background: tuple[int, int, int] = (0, 0, 0)) -> Image.Image:
    """Two portrait images side by side, matched on height rather than width.

    picframe matched on width, which crops the taller of the pair.  Matching
    height keeps both pictures whole and lets the gap absorb the difference.
    """
    target_h = min(a.height, b.height)
    def scale(im: Image.Image) -> Image.Image:
        w = max(1, round(im.width * target_h / im.height))
        return im.resize((w, target_h), Image.LANCZOS)

    a2, b2 = scale(a), scale(b)
    canvas = Image.new("RGB", (a2.width + b2.width + gap, target_h), background)
    canvas.paste(a2, (0, 0))
    canvas.paste(b2, (a2.width + gap, 0))
    return canvas


def prepare(
    metas: Sequence[PhotoMeta],
    screen_size: tuple[int, int],
    opts: PrepareOptions,
    *,
    images: Sequence[Image.Image] | None = None,
) -> Image.Image | None:
    """Produce the final RGB image for one slide.

    ``metas`` holds one photo, or two when portrait pairing is active.
    """
    loaded: list[Image.Image] = list(images) if images else []
    if not loaded:
        for m in metas:
            img = open_oriented(m.path)
            if img is None:
                return None
            loaded.append(img)
    if not loaded:
        return None

    sw, sh = screen_size
    if opts.kenburns_headroom > 1.0:
        sw = int(sw * opts.kenburns_headroom)
        sh = int(sh * opts.kenburns_headroom)
    target = (sw, sh)

    style = opts.mat_style
    fit = opts.fit
    if fit == "auto":
        fit = "mat" if mat_module.should_mat(
            loaded[0].size, target, style.tolerance
        ) else "cover"

    if len(loaded) > 1 and fit != "mat":
        merged = pair_portraits(loaded[0], loaded[1], background=opts.background)
        loaded = [merged]

    # Guard against blowing up a thumbnail to fill a 4K panel.
    src = loaded[0]
    if fit != "mat":
        scale = max(target[0] / src.width, target[1] / src.height)
        if scale > opts.upscale_limit:
            fit = "blur" if fit == "cover" else fit

    if fit == "mat":
        out = mat_module.apply(loaded, target, style, seed=metas[0].path if metas else None)
    elif fit == "contain":
        out = _fit_contain(src, target, opts.background)
    elif fit == "blur":
        out = _fit_blur(src, target, opts)
    else:
        out = _fit_cover(src, target)

    # Say, on the record, how this picture was laid out.  "Why was that one
    # cropped?" is the single most common question about a frame, and the
    # honest answer -- which of the five paths above ran -- is otherwise
    # invisible from the outside.
    out.info["picframe3_fit"] = fit

    for img in loaded:
        if img is not out:
            try:
                img.close()
            except Exception:
                pass
    return out


def placeholder(screen_size: tuple[int, int], message: str = "No pictures found",
                subtitle: str = "") -> Image.Image:
    """Shown when the library is empty -- no shipped JPEG needed."""
    from PIL import ImageDraw

    from ..gfx import textstyle  # local import keeps media/ importable headless

    w, h = screen_size
    img = Image.new("RGB", (w, h), (16, 18, 22))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(h - 1, 1)
        draw.line([(0, y), (w, y)], fill=(int(16 + 14 * t), int(18 + 16 * t), int(22 + 22 * t)))
    font = textstyle.load_font(None, max(18, h // 18))
    small = textstyle.load_font(None, max(12, h // 36))
    tw = draw.textlength(message, font=font)
    draw.text(((w - tw) / 2, h / 2 - h / 14), message, font=font, fill=(235, 235, 240))
    if subtitle:
        sw_ = draw.textlength(subtitle, font=small)
        draw.text(((w - sw_) / 2, h / 2 + h / 40), subtitle, font=small, fill=(150, 155, 165))
    return img
