"""Minimal, dependency-free ctypes binding for EGL 1.5.

Why hand-rolled bindings instead of PyOpenGL / moderngl?

* ``moderngl`` requires a desktop OpenGL 3.3 core context.  Mesa's ``v3d``
  driver on the Raspberry Pi 4/5 tops out at desktop GL 3.1 while exposing
  OpenGL **ES** 3.1, so moderngl cannot create a context there at all.
* ``PyOpenGL`` has partial and slow GLES coverage and adds a large dependency
  for the handful of entry points a picture frame actually needs.

A picture frame needs perhaps thirty GL calls.  Binding them directly keeps the
runtime dependency-free, keeps the hot loop free of per-call Python overhead in
PyOpenGL's wrapper machinery, and makes the EGL/DRM interaction explicit --
which is exactly the part that has to be correct on a headless Pi.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
from collections.abc import Iterable

_log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Library handles
# --------------------------------------------------------------------------

def _load(*names: str) -> ctypes.CDLL:
    last: OSError | None = None
    for name in names:
        try:
            return ctypes.CDLL(name)
        except OSError as exc:  # pragma: no cover - platform dependent
            last = exc
    found = ctypes.util.find_library(names[0].split(".so")[0].removeprefix("lib"))
    if found:
        return ctypes.CDLL(found)
    raise OSError(f"cannot load any of {names}: {last}")


class _LazyLib:
    """A shared library that is opened the first time something is called.

    Importing a module must not require Mesa to be installed.  ``picframe3
    doctor`` exists to *report* that EGL is missing and cannot do that if the
    import is what fails; ``picframe3 transitions``, ``--version``, the
    settings page, the overlay layout tests and the packaging check in CI all
    reach into the graphics package without ever drawing a frame.  Loading the
    library on the first actual call keeps all of them working on a machine
    that has no graphics stack at all, and changes nothing on the Pi.
    """

    def __init__(self, *names: str, on_open=None):
        self._names = names
        self._on_open = on_open
        self._lib: ctypes.CDLL | None = None

    def open(self) -> ctypes.CDLL:
        if self._lib is None:
            lib = _load(*self._names)
            if self._on_open is not None:
                self._on_open(lib)          # signatures, once
            self._lib = lib
        return self._lib

    @property
    def loaded(self) -> bool:
        return self._lib is not None

    def __getattr__(self, name: str):
        return getattr(self.open(), name)


libegl = _LazyLib("libEGL.so.1", "libEGL.so", on_open=lambda lib: _describe(lib))

# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------

EGLDisplay = ctypes.c_void_p
EGLConfig = ctypes.c_void_p
EGLContext = ctypes.c_void_p
EGLSurface = ctypes.c_void_p
EGLImage = ctypes.c_void_p
EGLNativeWindow = ctypes.c_void_p
EGLint = ctypes.c_int32
EGLAttrib = ctypes.c_ssize_t

NONE = 0x3038
FALSE = 0
TRUE = 1
NO_CONTEXT = None
NO_SURFACE = None
NO_DISPLAY = None

# Config attributes
BUFFER_SIZE = 0x3020
ALPHA_SIZE = 0x3021
BLUE_SIZE = 0x3022
GREEN_SIZE = 0x3023
RED_SIZE = 0x3024
DEPTH_SIZE = 0x3025
STENCIL_SIZE = 0x3026
SURFACE_TYPE = 0x3033
NATIVE_VISUAL_ID = 0x302E
RENDERABLE_TYPE = 0x3040
SAMPLES = 0x3031
SAMPLE_BUFFERS = 0x3032

# Surface type bits
PBUFFER_BIT = 0x0001
WINDOW_BIT = 0x0004

# Renderable type bits
OPENGL_ES2_BIT = 0x0004
OPENGL_ES3_BIT = 0x00000040

# Pbuffer attributes
WIDTH = 0x3057
HEIGHT = 0x3056

# Context attributes
CONTEXT_MAJOR_VERSION = 0x3098
CONTEXT_MINOR_VERSION = 0x30FB

# APIs
OPENGL_ES_API = 0x30A0

# Query strings
VENDOR = 0x3053
VERSION = 0x3054
EXTENSIONS = 0x3055
CLIENT_APIS = 0x308D

# Platforms
PLATFORM_SURFACELESS_MESA = 0x31DD
PLATFORM_GBM_KHR = 0x31D7

# dma-buf import (EGL_EXT_image_dma_buf_import)
LINUX_DMA_BUF_EXT = 0x3270
LINUX_DRM_FOURCC_EXT = 0x3271
DMA_BUF_PLANE0_FD_EXT = 0x3272
DMA_BUF_PLANE0_OFFSET_EXT = 0x3273
DMA_BUF_PLANE0_PITCH_EXT = 0x3274
DMA_BUF_PLANE1_FD_EXT = 0x3275
DMA_BUF_PLANE1_OFFSET_EXT = 0x3276
DMA_BUF_PLANE1_PITCH_EXT = 0x3277
DMA_BUF_PLANE2_FD_EXT = 0x3278
DMA_BUF_PLANE2_OFFSET_EXT = 0x3279
DMA_BUF_PLANE2_PITCH_EXT = 0x327A
IMAGE_PRESERVED_KHR = 0x30D2

_ERRORS = {
    0x3000: "EGL_SUCCESS",
    0x3001: "EGL_NOT_INITIALIZED",
    0x3002: "EGL_BAD_ACCESS",
    0x3003: "EGL_BAD_ALLOC",
    0x3004: "EGL_BAD_ATTRIBUTE",
    0x3005: "EGL_BAD_CONFIG",
    0x3006: "EGL_BAD_CONTEXT",
    0x3007: "EGL_BAD_CURRENT_SURFACE",
    0x3008: "EGL_BAD_DISPLAY",
    0x3009: "EGL_BAD_MATCH",
    0x300A: "EGL_BAD_NATIVE_PIXMAP",
    0x300B: "EGL_BAD_NATIVE_WINDOW",
    0x300C: "EGL_BAD_PARAMETER",
    0x300D: "EGL_BAD_SURFACE",
    0x300E: "EGL_CONTEXT_LOST",
}


class EGLError(RuntimeError):
    """An EGL entry point reported failure."""


# --------------------------------------------------------------------------
# Prototypes
# --------------------------------------------------------------------------

def _describe(lib: ctypes.CDLL) -> None:
    """Signatures for every entry point, applied when the library opens."""
    lib.eglGetError.restype = EGLint
    lib.eglGetProcAddress.restype = ctypes.c_void_p
    lib.eglGetProcAddress.argtypes = [ctypes.c_char_p]
    lib.eglGetDisplay.restype = EGLDisplay
    lib.eglGetDisplay.argtypes = [ctypes.c_void_p]
    lib.eglInitialize.restype = ctypes.c_uint
    lib.eglInitialize.argtypes = [EGLDisplay, ctypes.POINTER(EGLint), ctypes.POINTER(EGLint)]
    lib.eglTerminate.argtypes = [EGLDisplay]
    lib.eglQueryString.restype = ctypes.c_char_p
    lib.eglQueryString.argtypes = [EGLDisplay, EGLint]
    lib.eglBindAPI.restype = ctypes.c_uint
    lib.eglBindAPI.argtypes = [ctypes.c_uint]
    lib.eglChooseConfig.restype = ctypes.c_uint
    lib.eglChooseConfig.argtypes = [
        EGLDisplay, ctypes.POINTER(EGLint), ctypes.POINTER(EGLConfig), EGLint, ctypes.POINTER(EGLint)
    ]
    lib.eglGetConfigAttrib.restype = ctypes.c_uint
    lib.eglGetConfigAttrib.argtypes = [EGLDisplay, EGLConfig, EGLint, ctypes.POINTER(EGLint)]
    lib.eglCreateContext.restype = EGLContext
    lib.eglCreateContext.argtypes = [EGLDisplay, EGLConfig, EGLContext, ctypes.POINTER(EGLint)]
    lib.eglDestroyContext.argtypes = [EGLDisplay, EGLContext]
    lib.eglCreateWindowSurface.restype = EGLSurface
    lib.eglCreateWindowSurface.argtypes = [EGLDisplay, EGLConfig, EGLNativeWindow, ctypes.POINTER(EGLint)]
    lib.eglCreatePbufferSurface.restype = EGLSurface
    lib.eglCreatePbufferSurface.argtypes = [EGLDisplay, EGLConfig, ctypes.POINTER(EGLint)]
    lib.eglDestroySurface.argtypes = [EGLDisplay, EGLSurface]
    lib.eglMakeCurrent.restype = ctypes.c_uint
    lib.eglMakeCurrent.argtypes = [EGLDisplay, EGLSurface, EGLSurface, EGLContext]
    lib.eglSwapBuffers.restype = ctypes.c_uint
    lib.eglSwapBuffers.argtypes = [EGLDisplay, EGLSurface]
    lib.eglSwapInterval.restype = ctypes.c_uint
    lib.eglSwapInterval.argtypes = [EGLDisplay, EGLint]


def check(ok, what: str) -> None:
    if not ok:
        code = libegl.eglGetError()
        raise EGLError(f"{what} failed: {_ERRORS.get(code, hex(code))}")


def proc(name: str, restype, argtypes):
    """Resolve an EGL/GL extension entry point, or return ``None``."""
    addr = libegl.eglGetProcAddress(name.encode())
    if not addr:
        return None
    return ctypes.CFUNCTYPE(restype, *argtypes)(addr)


def int_array(values: Iterable[int]):
    vals = list(values)
    return (EGLint * len(vals))(*vals)


def attrib_array(values: Iterable[int]):
    vals = list(values)
    return (EGLAttrib * len(vals))(*vals)


# --------------------------------------------------------------------------
# Convenience wrappers
# --------------------------------------------------------------------------

def client_extensions() -> set[str]:
    raw = libegl.eglQueryString(None, EXTENSIONS)
    return set(raw.decode().split()) if raw else set()


def get_platform_display(platform: int, native: ctypes.c_void_p | None) -> EGLDisplay:
    """``eglGetPlatformDisplay`` with a graceful fall back to the EXT form."""
    exts = client_extensions()
    if "EGL_EXT_platform_base" in exts:
        fn = proc(
            "eglGetPlatformDisplayEXT",
            EGLDisplay,
            [ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(EGLint)],
        )
        if fn is not None:
            return EGLDisplay(fn(platform, native, None))
    try:
        libegl.eglGetPlatformDisplay.restype = EGLDisplay
        libegl.eglGetPlatformDisplay.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(EGLAttrib)]
        return EGLDisplay(libegl.eglGetPlatformDisplay(platform, native, None))
    except AttributeError:  # pragma: no cover - very old EGL
        return EGLDisplay(libegl.eglGetDisplay(native))


def display_extensions(dpy: EGLDisplay) -> set[str]:
    raw = libegl.eglQueryString(dpy, EXTENSIONS)
    return set(raw.decode().split()) if raw else set()


def initialize(dpy: EGLDisplay) -> tuple[int, int]:
    major, minor = EGLint(), EGLint()
    check(libegl.eglInitialize(dpy, ctypes.byref(major), ctypes.byref(minor)), "eglInitialize")
    return major.value, minor.value


def choose_config(dpy: EGLDisplay, attribs: Iterable[int]) -> EGLConfig:
    arr = int_array(list(attribs) + [NONE])
    cfg = EGLConfig()
    num = EGLint()
    check(
        libegl.eglChooseConfig(dpy, arr, ctypes.byref(cfg), 1, ctypes.byref(num)),
        "eglChooseConfig",
    )
    if num.value < 1:
        raise EGLError("eglChooseConfig returned no matching configuration")
    return cfg


def choose_config_matching(dpy: EGLDisplay, attribs: Iterable[int], visual_id: int) -> EGLConfig:
    """Pick the config whose native visual matches ``visual_id``.

    GBM requires the EGL config's ``EGL_NATIVE_VISUAL_ID`` to equal the DRM
    fourcc of the buffer object, otherwise ``eglCreateWindowSurface`` succeeds
    but the scanout shows garbage.
    """
    arr = int_array(list(attribs) + [NONE])
    num = EGLint()
    check(libegl.eglChooseConfig(dpy, arr, None, 0, ctypes.byref(num)), "eglChooseConfig(count)")
    if num.value < 1:
        raise EGLError("eglChooseConfig returned no matching configuration")
    configs = (EGLConfig * num.value)()
    check(
        libegl.eglChooseConfig(dpy, arr, configs, num.value, ctypes.byref(num)),
        "eglChooseConfig(list)",
    )
    for i in range(num.value):
        value = EGLint()
        if libegl.eglGetConfigAttrib(dpy, configs[i], NATIVE_VISUAL_ID, ctypes.byref(value)):
            if value.value == visual_id:
                return EGLConfig(configs[i])
    _log.warning("no EGL config matched DRM format %#x; using the first candidate", visual_id)
    return EGLConfig(configs[0])


def create_context(dpy: EGLDisplay, config: EGLConfig, gles_major: int = 3, gles_minor: int = 0) -> EGLContext:
    attribs = int_array([CONTEXT_MAJOR_VERSION, gles_major, CONTEXT_MINOR_VERSION, gles_minor, NONE])
    ctx = EGLContext(libegl.eglCreateContext(dpy, config, None, attribs))
    if not ctx:
        raise EGLError(f"eglCreateContext(ES {gles_major}.{gles_minor}) failed")
    return ctx


def make_current(dpy, draw, read, ctx) -> None:
    check(libegl.eglMakeCurrent(dpy, draw, read, ctx), "eglMakeCurrent")


def swap_buffers(dpy, surface) -> None:
    check(libegl.eglSwapBuffers(dpy, surface), "eglSwapBuffers")
