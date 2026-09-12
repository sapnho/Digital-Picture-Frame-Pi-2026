"""GL textures built from PIL images or numpy arrays.

Photographs are uploaded as ``GL_SRGB8_ALPHA8`` so the GPU decodes them to
linear light when sampling.  Every blend, fade and mat composite then happens
in linear space and is encoded back to sRGB once, at the end of the fragment
shader.  pi3d blended sRGB values directly, which is why crossfades there go
through a visibly dark midpoint; this does not.
"""

from __future__ import annotations

import ctypes
import logging

from . import gl

_log = logging.getLogger(__name__)

_MAX_TEXTURE_SIZE: int | None = None


def _pixels(data):
    """A pointer ctypes can pass as ``const void *`` without copying.

    ``bytes`` goes straight through, which matters: the data has already been
    copied once by ``image.tobytes()`` (or by the video decoder), and building a
    ``ctypes`` array from it copied every byte a second time -- several
    megabytes per upload, thirty times a second on the video path.  A writable
    buffer such as a ``bytearray`` is wrapped in place; only a read-only buffer
    that is not ``bytes`` has to be copied, because ``from_buffer`` refuses it.
    """
    if isinstance(data, bytes):
        return data
    try:
        return (ctypes.c_ubyte * len(data)).from_buffer(data)
    except TypeError:  # pragma: no cover - read-only memoryview and friends
        return (ctypes.c_ubyte * len(data)).from_buffer_copy(data)


def max_texture_size() -> int:
    global _MAX_TEXTURE_SIZE
    if _MAX_TEXTURE_SIZE is None:
        try:
            _MAX_TEXTURE_SIZE = gl.get_int(gl.MAX_TEXTURE_SIZE) or 2048
        except Exception:  # pragma: no cover
            _MAX_TEXTURE_SIZE = 2048
    return _MAX_TEXTURE_SIZE


class Texture:
    __slots__ = ("id", "width", "height", "srgb", "target")

    def __init__(self, width: int, height: int, *, srgb: bool = False,
                 target: int = gl.TEXTURE_2D):
        self.id = gl.gen(gl.glGenTextures)
        self.width = width
        self.height = height
        self.srgb = srgb
        self.target = target

    # -- construction ------------------------------------------------------
    @classmethod
    def from_image(cls, image, *, srgb: bool = True, mipmap: bool = True,
                   anisotropy: float = 8.0) -> Texture:
        """Upload a PIL image.  Converts to RGBA and flips to GL orientation."""
        from PIL import Image

        if image.mode != "RGBA":
            image = image.convert("RGBA")
        limit = max_texture_size()
        if image.width > limit or image.height > limit:
            scale = min(limit / image.width, limit / image.height)
            new = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
            _log.info("image %dx%d exceeds GL_MAX_TEXTURE_SIZE %d; resizing to %dx%d",
                      image.width, image.height, limit, *new)
            image = image.resize(new, Image.LANCZOS)
        image = image.transpose(Image.FLIP_TOP_BOTTOM)
        data = image.tobytes()
        return cls.from_bytes(image.width, image.height, data, srgb=srgb,
                              mipmap=mipmap, anisotropy=anisotropy)

    @classmethod
    def from_array(cls, array, *, srgb: bool = False, mipmap: bool = False,
                   flip: bool = True) -> Texture:
        import numpy as np

        arr = np.ascontiguousarray(array[::-1] if flip else array, dtype=np.uint8)
        h, w = arr.shape[:2]
        if arr.ndim == 2:
            # A greyscale frame has no channel axis at all, and indexing one
            # that is not there is an IndexError rather than a bad picture.
            arr = arr[:, :, None]
        channels = arr.shape[2]
        if channels in (1, 3):
            rgba = np.empty((h, w, 4), dtype=np.uint8)
            rgba[..., :3] = arr                      # broadcasts for greyscale
            rgba[..., 3] = 255
            arr = rgba
        elif channels != 4:
            raise ValueError(f"expected 1, 3 or 4 channels, got {channels}")
        return cls.from_bytes(w, h, arr.tobytes(), srgb=srgb, mipmap=mipmap)

    @classmethod
    def from_bytes(cls, width: int, height: int, data: bytes, *, srgb: bool = False,
                   mipmap: bool = True, anisotropy: float = 8.0) -> Texture:
        tex = cls(width, height, srgb=srgb)
        gl.glBindTexture(gl.TEXTURE_2D, tex.id)
        gl.glPixelStorei(gl.UNPACK_ALIGNMENT, 1)
        internal = gl.SRGB8_ALPHA8 if srgb else gl.RGBA8
        gl.glTexImage2D(gl.TEXTURE_2D, 0, internal, width, height, 0,
                        gl.RGBA, gl.UNSIGNED_BYTE, _pixels(data))
        tex._configure(mipmap, anisotropy)
        gl.glBindTexture(gl.TEXTURE_2D, 0)
        return tex

    @classmethod
    def solid(cls, rgba=(0, 0, 0, 255)) -> Texture:
        return cls.from_bytes(1, 1, bytes(rgba), srgb=False, mipmap=False)

    def _configure(self, mipmap: bool, anisotropy: float) -> None:
        if mipmap:
            gl.glGenerateMipmap(gl.TEXTURE_2D)
            gl.glTexParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR)
        else:
            gl.glTexParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
        gl.glTexParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
        gl.glTexParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
        if anisotropy > 1.0 and "GL_EXT_texture_filter_anisotropic" in _gl_extensions():
            gl.glTexParameterf(gl.TEXTURE_2D, gl.TEXTURE_MAX_ANISOTROPY_EXT, anisotropy)

    @classmethod
    def empty(cls, width: int, height: int, *, srgb: bool = True) -> Texture:
        """Allocate storage for a texture that will be refilled every frame."""
        tex = cls(width, height, srgb=srgb)
        gl.glBindTexture(gl.TEXTURE_2D, tex.id)
        internal = gl.SRGB8_ALPHA8 if srgb else gl.RGBA8
        gl.glTexImage2D(gl.TEXTURE_2D, 0, internal, width, height, 0,
                        gl.RGBA, gl.UNSIGNED_BYTE, None)
        tex._configure(mipmap=False, anisotropy=1.0)
        gl.glBindTexture(gl.TEXTURE_2D, 0)
        return tex

    def update(self, data: bytes, width: int | None = None,
               height: int | None = None) -> None:
        """Replace the pixels in place -- the video path, called per frame.

        ``glTexSubImage2D`` into existing storage avoids reallocating a
        multi-megabyte texture thirty times a second.
        """
        w = width or self.width
        h = height or self.height
        gl.glBindTexture(gl.TEXTURE_2D, self.id)
        gl.glPixelStorei(gl.UNPACK_ALIGNMENT, 1)
        pixels = _pixels(data)
        if (w, h) != (self.width, self.height):
            internal = gl.SRGB8_ALPHA8 if self.srgb else gl.RGBA8
            gl.glTexImage2D(gl.TEXTURE_2D, 0, internal, w, h, 0,
                            gl.RGBA, gl.UNSIGNED_BYTE, pixels)
            self.width, self.height = w, h
        else:
            gl.glTexSubImage2D(gl.TEXTURE_2D, 0, 0, 0, w, h,
                               gl.RGBA, gl.UNSIGNED_BYTE, pixels)
        gl.glBindTexture(gl.TEXTURE_2D, 0)

    # -- use ---------------------------------------------------------------
    def bind(self, unit: int = 0) -> None:
        gl.glActiveTexture(gl.TEXTURE0 + unit)
        gl.glBindTexture(self.target, self.id)

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 1.0

    def close(self) -> None:
        if self.id:
            gl.delete(gl.glDeleteTextures, self.id)
            self.id = 0

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Texture {self.width}x{self.height}{' sRGB' if self.srgb else ''}>"


_EXT_CACHE: set[str] | None = None


def _gl_extensions() -> set[str]:
    global _EXT_CACHE
    if _EXT_CACHE is None:
        try:
            _EXT_CACHE = gl.extensions()
        except Exception:  # pragma: no cover
            _EXT_CACHE = set()
    return _EXT_CACHE
