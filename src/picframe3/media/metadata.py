"""Photo and video metadata extraction.

Reads EXIF, IPTC and XMP through Pillow only.  ``picframe`` pulled in
``exifread``, ``IPTCInfo3`` and ``defusedxml`` for the same job; Pillow already
parses all three containers and is a dependency regardless.

Everything returns ``None`` rather than raising: a picture frame must never
stop because one file in ten thousand has a malformed APP1 segment.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any

from PIL import ExifTags, Image, IptcImagePlugin

_log = logging.getLogger(__name__)

# Big enough for any panorama a camera or a phone produces (a 120 MP image is
# 30 m of 4K screen), small enough that a decompression bomb dropped into the
# guest share cannot claim 1.5 GB on a Pi and take the frame down with it.
Image.MAX_IMAGE_PIXELS = 120_000_000

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif", ".jfif",
}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".mpg", ".mpeg", ".3gp"}

_EXIF_TAGS = {v: k for k, v in ExifTags.TAGS.items()}
_GPS_TAGS = {v: k for k, v in ExifTags.GPSTAGS.items()}

# IPTC record/dataset pairs we care about
_IPTC_TITLE = (2, 5)
_IPTC_CAPTION = (2, 120)
_IPTC_KEYWORDS = (2, 25)
_IPTC_CITY = (2, 90)
_IPTC_COUNTRY = (2, 101)


@dataclass
class PhotoMeta:
    path: str
    width: int = 0
    height: int = 0
    orientation: int = 1
    taken_at: float | None = None        # POSIX timestamp, local wall clock
    make: str | None = None
    model: str | None = None
    lens: str | None = None
    f_number: float | None = None
    exposure_time: str | None = None
    iso: int | None = None
    focal_length: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    title: str | None = None
    caption: str | None = None
    tags: list[str] = field(default_factory=list)
    rating: int | None = None
    is_video: bool = False
    duration: float | None = None

    @property
    def is_portrait(self) -> bool:
        w, h = self.display_size
        return h > w

    @property
    def display_size(self) -> tuple[int, int]:
        """Size after the EXIF orientation has been applied."""
        if self.orientation in (5, 6, 7, 8):
            return self.height, self.width
        return self.width, self.height

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        for enc in ("utf-8", "latin-1"):
            try:
                value = value.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:  # pragma: no cover
            return None
    text = str(value).replace("\x00", "").strip()
    return text or None


def _to_float(value: Any) -> float | None:
    try:
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return float(value[0]) / float(value[1]) if value[1] else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _parse_exif_datetime(text: Any, offset: Any = None) -> float | None:
    text = _clean(text)
    if not text:
        return None
    text = text.replace("/", ":").replace("-", ":", 2)
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y:%m:%d"):
        try:
            dt = datetime.strptime(text[: len(fmt) + 4], fmt)
        except ValueError:
            continue
        tz = _parse_offset(offset)
        if tz is not None:
            dt = dt.replace(tzinfo=tz)
        return dt.timestamp()
    return None


def _parse_offset(offset: Any):
    text = _clean(offset)
    if not text:
        return None
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", text)
    if not m:
        return None
    from datetime import timedelta

    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


def _gps_to_degrees(value, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in value)
    except (TypeError, ValueError):
        return None
    deg = d + m / 60.0 + s / 3600.0
    if _clean(ref) in ("S", "W"):
        deg = -deg
    return round(deg, 7)


def _format_exposure(value) -> str | None:
    f = _to_float(value)
    if f is None:
        return None
    if f >= 1:
        return f"{f:g}s"
    frac = Fraction(f).limit_denominator(8000)
    return f"1/{round(1 / f)}s" if frac.numerator == 1 or f < 1 else f"{frac}s"


def read(path: str, *, load_iptc: bool = True, load_xmp: bool = True) -> PhotoMeta | None:
    """Extract metadata without decoding the full image.

    Returns ``None`` only when the file cannot be opened as an image at all --
    it is missing, or the plugin for its format is not installed.  Anything
    that goes wrong after that is per-container and costs only what that
    container held: a malformed APP1 segment used to discard the size, the
    date, the tags and the caption together and make the whole file
    unindexable, so it was read again, and failed again, on every single scan.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTENSIONS:
        return _read_video(path)
    meta = PhotoMeta(path=path, is_video=False)
    try:
        with Image.open(path) as img:
            meta.width, meta.height = img.size
            for stage, reader in (("EXIF", _read_exif),
                                  ("IPTC", _read_iptc if load_iptc else None),
                                  ("XMP", _read_xmp if load_xmp else None)):
                if reader is None:
                    continue
                try:
                    reader(img, meta)
                except Exception as exc:
                    # Absent, not fatal: what the other containers gave is
                    # still worth indexing.
                    _log.debug("%s unreadable in %s: %s", stage, path, exc)
    except Exception as exc:
        _log.debug("metadata read failed for %s: %s", path, exc)
        return None
    if meta.taken_at is None:
        try:
            meta.taken_at = os.path.getmtime(path)
        except OSError:
            pass
    return meta


def _read_exif(img: Image.Image, meta: PhotoMeta) -> None:
    try:
        exif = img.getexif()
    except Exception:
        return
    if not exif:
        return
    meta.orientation = int(exif.get(_EXIF_TAGS.get("Orientation", 274), 1) or 1)
    meta.make = _clean(exif.get(_EXIF_TAGS.get("Make", 271)))
    meta.model = _clean(exif.get(_EXIF_TAGS.get("Model", 272)))
    meta.title = meta.title or _clean(exif.get(_EXIF_TAGS.get("ImageDescription", 270)))
    try:
        ifd = exif.get_ifd(0x8769)
    except Exception:
        ifd = {}
    meta.taken_at = (
        _parse_exif_datetime(ifd.get(36867), ifd.get(36881))
        or _parse_exif_datetime(ifd.get(36868), ifd.get(36882))
        or _parse_exif_datetime(exif.get(306), ifd.get(36880))
    )
    meta.f_number = _to_float(ifd.get(33437))
    meta.exposure_time = _format_exposure(ifd.get(33434))
    iso = ifd.get(34855) or ifd.get(34867)
    if isinstance(iso, (tuple, list)) and iso:
        iso = iso[0]
    try:
        meta.iso = int(iso) if iso is not None else None
    except (TypeError, ValueError):
        meta.iso = None
    meta.focal_length = _to_float(ifd.get(37386))
    meta.lens = _clean(ifd.get(42036)) or _clean(ifd.get(0xA434))
    try:
        gps = exif.get_ifd(0x8825)
    except Exception:
        gps = {}
    if gps:
        meta.latitude = _gps_to_degrees(gps.get(_GPS_TAGS.get("GPSLatitude", 2)),
                                        gps.get(_GPS_TAGS.get("GPSLatitudeRef", 1)))
        meta.longitude = _gps_to_degrees(gps.get(_GPS_TAGS.get("GPSLongitude", 4)),
                                         gps.get(_GPS_TAGS.get("GPSLongitudeRef", 3)))


def _read_iptc(img: Image.Image, meta: PhotoMeta) -> None:
    try:
        info = IptcImagePlugin.getiptcinfo(img)
    except Exception:
        return
    if not info:
        return
    meta.title = _clean(info.get(_IPTC_TITLE)) or meta.title
    meta.caption = _clean(info.get(_IPTC_CAPTION)) or meta.caption
    kw = info.get(_IPTC_KEYWORDS)
    if kw:
        values = kw if isinstance(kw, (list, tuple)) else [kw]
        meta.tags = sorted({t for t in (_clean(v) for v in values) if t})


def _read_xmp(img: Image.Image, meta: PhotoMeta) -> None:
    try:
        xmp = img.getxmp()
    except Exception:
        return
    if not xmp:
        return
    flat: dict[str, Any] = {}

    def walk(node, prefix=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{prefix}.{k}" if prefix else k)
        elif isinstance(node, list):
            for v in node:
                walk(v, prefix)
        else:
            flat.setdefault(prefix, node)

    walk(xmp)
    for key, value in flat.items():
        low = key.lower()
        if meta.rating is None and low.endswith("rating"):
            try:
                meta.rating = int(float(value))
            except (TypeError, ValueError):
                pass
        elif meta.title is None and low.endswith((".title.alt.li", "dc:title")):
            meta.title = _clean(value)
        elif meta.caption is None and low.endswith((".description.alt.li",)):
            meta.caption = _clean(value)
    if not meta.tags:
        subjects = [v for k, v in flat.items() if "subject" in k.lower()]
        meta.tags = sorted({t for t in (_clean(v) for v in subjects) if t})


def _read_video(path: str) -> PhotoMeta | None:
    """Probe a video's dimensions, rotation and duration via GStreamer."""
    meta = PhotoMeta(path=path, is_video=True)
    try:
        from .video import probe

        info = probe(path)
        if info:
            meta.width = info.get("width", 0)
            meta.height = info.get("height", 0)
            meta.duration = info.get("duration")
            meta.orientation = info.get("orientation", 1)
            meta.taken_at = info.get("taken_at")
    except Exception as exc:
        _log.debug("video probe failed for %s: %s", path, exc)
    if meta.taken_at is None:
        try:
            meta.taken_at = os.path.getmtime(path)
        except OSError:
            pass
    return meta


def is_supported(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in IMAGE_EXTENSIONS or ext in VIDEO_EXTENSIONS


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS
