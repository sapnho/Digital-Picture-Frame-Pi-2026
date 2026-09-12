"""ctypes binding for the OpenGL ES 3.0 subset the frame renderer uses.

Deliberately small.  Everything here is an entry point that is actually called
by :mod:`picframe3.gfx.renderer`; nothing is bound "just in case".
"""

from __future__ import annotations

import ctypes
import logging
from collections.abc import Sequence

from .egl import _LazyLib

_log = logging.getLogger(__name__)

# The GLES library is opened on the first call, not on import, and the soname
# is a fallback chain rather than one exact name.  Both matter for the same
# reason: anything that merely imports the graphics package -- the overlay
# layout tests, the settings page, ``picframe3 transitions``, ``picframe3
# doctor``, the packaging check in CI -- must work on a machine with no Mesa
# at all.  ``doctor`` in particular exists to say that GLES is missing, which
# it cannot do if importing it is what fails.
lib = _LazyLib("libGLESv2.so.2", "libGLESv2.so", on_open=lambda handle: _describe(handle))

# --- enums ---------------------------------------------------------------
FALSE = 0
TRUE = 1
NO_ERROR = 0

COLOR_BUFFER_BIT = 0x00004000
DEPTH_BUFFER_BIT = 0x00000100

TRIANGLE_STRIP = 0x0005
TRIANGLES = 0x0004

FLOAT = 0x1406
UNSIGNED_BYTE = 0x1401
UNSIGNED_SHORT = 0x1403
UNSIGNED_INT = 0x1405

ARRAY_BUFFER = 0x8892
ELEMENT_ARRAY_BUFFER = 0x8893
STATIC_DRAW = 0x88E4
DYNAMIC_DRAW = 0x88E8

FRAGMENT_SHADER = 0x8B30
VERTEX_SHADER = 0x8B31
COMPILE_STATUS = 0x8B81
LINK_STATUS = 0x8B82
INFO_LOG_LENGTH = 0x8B84

TEXTURE_2D = 0x0DE1
TEXTURE_EXTERNAL_OES = 0x8D65
TEXTURE0 = 0x84C0
TEXTURE_MAG_FILTER = 0x2800
TEXTURE_MIN_FILTER = 0x2801
TEXTURE_WRAP_S = 0x2802
TEXTURE_WRAP_T = 0x2803
TEXTURE_MAX_ANISOTROPY_EXT = 0x84FE
NEAREST = 0x2600
LINEAR = 0x2601
LINEAR_MIPMAP_LINEAR = 0x2703
CLAMP_TO_EDGE = 0x812F
MIRRORED_REPEAT = 0x8370

RGBA = 0x1908
RGB = 0x1907
RED = 0x1903
RG = 0x8227
RGBA8 = 0x8058
SRGB8_ALPHA8 = 0x8C43
R8 = 0x8229
RG8 = 0x822B
UNPACK_ALIGNMENT = 0x0CF5
UNPACK_ROW_LENGTH = 0x0CF2
PACK_ALIGNMENT = 0x0D05

FRAMEBUFFER = 0x8D40
COLOR_ATTACHMENT0 = 0x8CE0
FRAMEBUFFER_COMPLETE = 0x8CD5
RENDERBUFFER = 0x8D41

BLEND = 0x0BE2
SRC_ALPHA = 0x0302
ONE_MINUS_SRC_ALPHA = 0x0303
ONE = 1
DEPTH_TEST = 0x0B71
CULL_FACE = 0x0B44
DITHER = 0x0BD0
SCISSOR_TEST = 0x0C11
FRAMEBUFFER_SRGB_EXT = 0x8DB9

VENDOR = 0x1F00
RENDERER = 0x1F01
VERSION = 0x1F02
SHADING_LANGUAGE_VERSION = 0x8B8C
EXTENSIONS = 0x1F03
NUM_EXTENSIONS = 0x821D
MAX_TEXTURE_SIZE = 0x0D33


class GLError(RuntimeError):
    pass


# --- prototypes ----------------------------------------------------------
_P = ctypes.POINTER

_protos = {
    "glGetError": (ctypes.c_uint, []),
    "glGetString": (ctypes.c_char_p, [ctypes.c_uint]),
    "glGetStringi": (ctypes.c_char_p, [ctypes.c_uint, ctypes.c_uint]),
    "glGetIntegerv": (None, [ctypes.c_uint, _P(ctypes.c_int)]),
    "glViewport": (None, [ctypes.c_int] * 4),
    "glScissor": (None, [ctypes.c_int] * 4),
    "glClearColor": (None, [ctypes.c_float] * 4),
    "glClear": (None, [ctypes.c_uint]),
    "glEnable": (None, [ctypes.c_uint]),
    "glDisable": (None, [ctypes.c_uint]),
    "glBlendFunc": (None, [ctypes.c_uint, ctypes.c_uint]),
    "glBlendFuncSeparate": (None, [ctypes.c_uint] * 4),
    "glFinish": (None, []),
    "glFlush": (None, []),
    "glPixelStorei": (None, [ctypes.c_uint, ctypes.c_int]),
    "glReadPixels": (None, [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]),
    # shaders
    "glCreateShader": (ctypes.c_uint, [ctypes.c_uint]),
    "glShaderSource": (None, [ctypes.c_uint, ctypes.c_int, _P(ctypes.c_char_p), _P(ctypes.c_int)]),
    "glCompileShader": (None, [ctypes.c_uint]),
    "glGetShaderiv": (None, [ctypes.c_uint, ctypes.c_uint, _P(ctypes.c_int)]),
    "glGetShaderInfoLog": (None, [ctypes.c_uint, ctypes.c_int, _P(ctypes.c_int), ctypes.c_char_p]),
    "glDeleteShader": (None, [ctypes.c_uint]),
    "glCreateProgram": (ctypes.c_uint, []),
    "glAttachShader": (None, [ctypes.c_uint, ctypes.c_uint]),
    "glLinkProgram": (None, [ctypes.c_uint]),
    "glGetProgramiv": (None, [ctypes.c_uint, ctypes.c_uint, _P(ctypes.c_int)]),
    "glGetProgramInfoLog": (None, [ctypes.c_uint, ctypes.c_int, _P(ctypes.c_int), ctypes.c_char_p]),
    "glUseProgram": (None, [ctypes.c_uint]),
    "glDeleteProgram": (None, [ctypes.c_uint]),
    "glGetUniformLocation": (ctypes.c_int, [ctypes.c_uint, ctypes.c_char_p]),
    "glGetAttribLocation": (ctypes.c_int, [ctypes.c_uint, ctypes.c_char_p]),
    "glUniform1i": (None, [ctypes.c_int, ctypes.c_int]),
    "glUniform1f": (None, [ctypes.c_int, ctypes.c_float]),
    "glUniform2f": (None, [ctypes.c_int, ctypes.c_float, ctypes.c_float]),
    "glUniform3f": (None, [ctypes.c_int] + [ctypes.c_float] * 3),
    "glUniform4f": (None, [ctypes.c_int] + [ctypes.c_float] * 4),
    "glUniformMatrix4fv": (None, [ctypes.c_int, ctypes.c_int, ctypes.c_ubyte, _P(ctypes.c_float)]),
    # buffers / arrays
    "glGenBuffers": (None, [ctypes.c_int, _P(ctypes.c_uint)]),
    "glBindBuffer": (None, [ctypes.c_uint, ctypes.c_uint]),
    "glBufferData": (None, [ctypes.c_uint, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint]),
    "glDeleteBuffers": (None, [ctypes.c_int, _P(ctypes.c_uint)]),
    "glGenVertexArrays": (None, [ctypes.c_int, _P(ctypes.c_uint)]),
    "glBindVertexArray": (None, [ctypes.c_uint]),
    "glDeleteVertexArrays": (None, [ctypes.c_int, _P(ctypes.c_uint)]),
    "glEnableVertexAttribArray": (None, [ctypes.c_uint]),
    "glVertexAttribPointer": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
                                     ctypes.c_ubyte, ctypes.c_int, ctypes.c_void_p]),
    "glDrawArrays": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_int]),
    # textures
    "glGenTextures": (None, [ctypes.c_int, _P(ctypes.c_uint)]),
    "glBindTexture": (None, [ctypes.c_uint, ctypes.c_uint]),
    "glActiveTexture": (None, [ctypes.c_uint]),
    "glTexParameteri": (None, [ctypes.c_uint, ctypes.c_uint, ctypes.c_int]),
    "glTexParameterf": (None, [ctypes.c_uint, ctypes.c_uint, ctypes.c_float]),
    "glTexImage2D": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]),
    "glTexSubImage2D": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]),
    "glTexStorage2D": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_uint, ctypes.c_int, ctypes.c_int]),
    "glGenerateMipmap": (None, [ctypes.c_uint]),
    "glDeleteTextures": (None, [ctypes.c_int, _P(ctypes.c_uint)]),
    # framebuffers
    "glGenFramebuffers": (None, [ctypes.c_int, _P(ctypes.c_uint)]),
    "glBindFramebuffer": (None, [ctypes.c_uint, ctypes.c_uint]),
    "glFramebufferTexture2D": (None, [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_int]),
    "glCheckFramebufferStatus": (ctypes.c_uint, [ctypes.c_uint]),
    "glDeleteFramebuffers": (None, [ctypes.c_int, _P(ctypes.c_uint)]),
}

def _describe(handle) -> None:
    """Bind every prototype, once, when the library is actually opened."""
    for name, (ret, args) in _protos.items():
        try:
            fn = getattr(handle, name)
        except AttributeError:  # pragma: no cover - driver dependent
            continue
        fn.restype = ret
        fn.argtypes = args
        globals()[name] = fn


def __getattr__(name: str):
    """``gl.glClear`` opens the library and hands back the bound entry point.

    PEP 562: this runs only for names the module does not already define, so
    after the first call every entry point is an ordinary module global and
    costs nothing.
    """
    if name in _protos:
        lib.open()
        if name in globals():
            return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def check_error(context: str = "") -> None:
    err = lib.glGetError()
    if err != NO_ERROR:
        raise GLError(f"GL error {hex(err)}{' in ' + context if context else ''}")


def get_string(name: int) -> str:
    val = lib.glGetString(name)
    return val.decode() if val else ""


def get_int(name: int) -> int:
    out = ctypes.c_int()
    lib.glGetIntegerv(name, ctypes.byref(out))
    return out.value


def extensions() -> set[str]:
    """GLES 3 replaced the single ``GL_EXTENSIONS`` string with indexed queries."""
    try:
        count = get_int(NUM_EXTENSIONS)
        if count:
            return {lib.glGetStringi(EXTENSIONS, i).decode() for i in range(count)}
    except Exception:  # pragma: no cover
        pass
    return set(get_string(EXTENSIONS).split())


#: picframe3's shaders are ES 3.00.  The Raspberry Pi 4, 400 and 5 provide
#: OpenGL ES 3.1 through Mesa's v3d driver; the Pi 2, 3 and Zero 2 W have
#: VideoCore IV and stop at 2.0.
REQUIRED_ES_VERSION = (3, 0)


def parse_es_version(version_string: str) -> tuple[int, int] | None:
    """Pull (major, minor) out of a GL_VERSION string.

    Real examples this has to survive:
        "OpenGL ES 3.1 Mesa 23.2.1-1~bpo12+rpt3"
        "OpenGL ES 3.2 Mesa 25.2.8"
        "OpenGL ES-CM 1.1"
    """
    import re

    match = re.search(r"OpenGL ES(?:-\w+)?\s+(\d+)\.(\d+)", version_string or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def es_version_ok(version_string: str) -> bool:
    found = parse_es_version(version_string)
    return found is not None and found >= REQUIRED_ES_VERSION


def gen(fn) -> int:
    out = ctypes.c_uint()
    fn(1, ctypes.byref(out))
    return out.value


def delete(fn, name: int) -> None:
    if name:
        fn(1, ctypes.byref(ctypes.c_uint(name)))


def floats(values: Sequence[float]):
    return (ctypes.c_float * len(values))(*values)
