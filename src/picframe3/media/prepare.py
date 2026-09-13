"""Turn a file on disk into exactly one display-sized image, ready to upload.

All of the expensive, quality-sensitive work happens here on the CPU, once per
slide -- which on a picture frame is once every few minutes.  The GPU then only
has to run the transition.  That split is what keeps the frame at a steady
refresh on a Pi 4 while still using LANCZOS resampling and real Gaussian blur.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFile, ImageFilter, ImageOps

from . import mat as mat_module
from . import shrink as shrink_module
from .metadata import PhotoMeta

_log = logging.getLogger(__name__)

ImageFile.LOAD_TRUNCATED_IMAGES = True        # a damaged JPEG must not stall the show

FIT_MODES = ("cover", "contain", "blur", "mat", "auto")

#: What ``auto`` may choose between for a picture that does not match the panel.
AUTO_FITS = ("mat", "blur", "contain", "cover")

#: Largest picture the slideshow will decode, in pixels, when nothing says
#: otherwise.  A 4K panel is 8.3 megapixels; this is the same number the web
#: interface already refuses to decode past, so the frame has one answer to
#: "how big a picture will you open" rather than two.
DEFAULT_MAX_DECODE_PIXELS = 64_000_000


class TooLargeToDecode(Exception):
    """A picture refused by ``max_decode_pixels``.

    Its own exception rather than the plain ``None`` an unreadable file
    returns, because the slideshow owes the two opposite answers.  A file that
    will not decode is hidden, so the frame stops offering it; this one is
    perfectly good and is being refused by a setting, and hiding it would mean
    that raising the setting did not bring it back -- ``hidden`` is cleared
    only when a file's bytes change.
    """

    def __init__(self, path: str, size: tuple[int, int], max_pixels: int):
        self.path, self.size, self.max_pixels = path, size, max_pixels
        super().__init__(
            f"{path} is {size[0]}\u00d7{size[1]}, past the "
            f"{max_pixels / 1_000_000:g} megapixel decode limit"
        )


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
    #: What ``fit="auto"`` may do with a picture that does not match the panel.
    fit_choices: tuple[str, ...] = ("mat",)
    #: Largest picture to decode, in pixels, measured after any DCT scaling.
    #: 0 turns the limit off.  See :func:`open_oriented`.
    max_decode_pixels: int = DEFAULT_MAX_DECODE_PIXELS
    #: Keep a panel-sized copy of a picture past that limit and show it,
    #: instead of leaving the picture out of the slideshow.
    shrink_oversized: bool = True

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


def _within(size: tuple[int, int], max_pixels: int) -> tuple[int, int]:
    """*size* scaled down until it fits in *max_pixels*, keeping its shape."""
    pixels = max(1, size[0] * size[1])
    if max_pixels <= 0 or pixels <= max_pixels:
        return size
    scale = (max_pixels / pixels) ** 0.5
    return (max(1, int(size[0] * scale)), max(1, int(size[1] * scale)))


def open_oriented(path: str, *, target: tuple[int, int] | None = None,
                  max_pixels: int = 0, shrink: bool = True) -> Image.Image | None:
    """Open an image at a sensible size, with its EXIF orientation baked in.

    ``target`` is the largest size this slide can need.  A JPEG or a HEIF is
    then decoded straight to the smallest DCT scale that still covers it, which
    on a 45 megapixel phone photograph is most of a second saved and a few
    hundred megabytes never allocated.  Nothing is lost -- the picture is
    resampled down to the panel a few lines later anyway -- and ``draft`` never
    enlarges, so a small picture is untouched and the upscale guard below still
    sees its real size.

    ``max_pixels`` is the ceiling after that, and it is there for the formats
    with no such shortcut.  A PNG, a TIFF or a BMP is decoded whole or not at
    all: a 200 megapixel flatbed scan dropped into the watched folder is 600 MB
    of RGB on a machine with 4 GB and a 4K texture already resident.  Past the
    ceiling the picture is opened once in a child process and a panel-sized
    copy is kept -- see :mod:`picframe3.media.shrink` -- so the photograph is
    still shown and nothing is moved or deleted.  Only when that copy cannot be
    made, or ``shrink`` is off, is the picture refused; the alternative to
    refusing it is the whole frame, because the kernel does not kill the scan,
    it kills us.  0 turns the ceiling off.
    """
    try:
        img = Image.open(path)
        if target:
            # A documented no-op on every format that cannot do it, so the
            # formats that can are the only ones that notice.
            img.draft("RGB", target)
        if max_pixels > 0 and img.width * img.height > max_pixels:
            size = (img.width, img.height)
            img.close()
            copy = None
            if shrink:
                copy = shrink_module.cached_copy(
                    path, size, target or _within(size, max_pixels))
            if copy is None:
                raise TooLargeToDecode(path, size, max_pixels)
            # The copy is already panel-sized and its orientation is baked in,
            # so it needs neither the draft above nor anything below it beyond
            # the usual mode conversion.
            img = Image.open(copy)
        img.load()
    except TooLargeToDecode:
        raise
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


def pick_auto_fit(choices: Sequence[str], seed: str = "") -> str:
    """Which treatment ``auto`` gives one mismatched picture.

    Seeded from the file path rather than left to chance, so a given
    photograph always comes up looking the same -- a frame that showed the
    same picture matted one day and blurred the next would read as a fault.
    """
    wanted = [c for c in (choices or ()) if c in AUTO_FITS]
    if not wanted:
        return "mat"
    if len(wanted) == 1:
        return wanted[0]
    return random.Random(seed).choice(wanted)


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
    # Worked out before anything is opened, because it is what the decoder is
    # told to aim for: every path below wants the picture at the panel's size
    # or smaller, so there is no reason to unpack it any larger.
    sw, sh = screen_size
    if opts.kenburns_headroom > 1.0:
        sw = int(sw * opts.kenburns_headroom)
        sh = int(sh * opts.kenburns_headroom)
    target = (sw, sh)

    loaded: list[Image.Image] = list(images) if images else []
    if not loaded:
        for m in metas:
            img = open_oriented(m.path, target=target,
                                max_pixels=opts.max_decode_pixels,
                                shrink=opts.shrink_oversized)
            if img is None:
                return None
            loaded.append(img)
    if not loaded:
        return None

    style = opts.mat_style
    fit = opts.fit
    if fit == "auto":
        if mat_module.should_mat(loaded[0].size, target, style.tolerance):
            fit = pick_auto_fit(opts.fit_choices, metas[0].path if metas else "")
        else:
            # Already the panel's shape: filling the screen crops nothing, so
            # there is no treatment to choose between.
            fit = "cover"

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


#: The picture shown when the library is empty.  A picture frame with nothing
#: in it should still have something in it; the drawn screen below is the
#: fallback, not the default.  ``viewer.no_files_img`` replaces this file.
NO_FILES_FILE = Path(__file__).resolve().parents[1] / "data" / "no_pictures.jpg"

#: Values of ``viewer.no_files_img`` that mean "draw the plain screen instead".
NO_FILES_OFF = frozenset({"none", "off", "-"})


def no_files_screen(screen_size: tuple[int, int], source: str = "", *,
                    message: str = "No pictures yet",
                    subtitle: str = "") -> Image.Image:
    """The empty-library screen at ``screen_size``.

    ``source`` is ``viewer.no_files_img``: empty uses the picture that ships
    with the frame, a path uses that file, ``none`` draws the plain screen.  A
    file that cannot be read falls back to the drawing rather than to black --
    an empty frame showing nothing at all just looks broken.
    """
    wanted = str(source or "").strip()
    if wanted.lower() not in NO_FILES_OFF:
        path = Path(wanted).expanduser() if wanted else NO_FILES_FILE
        image = open_oriented(str(path), target=screen_size)
        if image is not None:
            try:
                return _fit_contain(image.convert("RGB"), screen_size, (0, 0, 0))
            finally:
                image.close()
        _log.info("no_files_img %s unreadable; drawing the empty screen instead", path)
    return placeholder(screen_size, message, subtitle)


def placeholder(screen_size: tuple[int, int], message: str = "No pictures found",
                subtitle: str = "") -> Image.Image:
    """The drawn empty screen -- the fallback when there is no picture to show."""
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
