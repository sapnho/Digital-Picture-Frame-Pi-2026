"""GLSL program objects with cached uniform locations."""

from __future__ import annotations

import ctypes
import logging

from . import gl

_log = logging.getLogger(__name__)


class ShaderError(RuntimeError):
    pass


def _compile(kind: int, source: str, label: str) -> int:
    shader = gl.glCreateShader(kind)
    src = source.encode()
    arr = (ctypes.c_char_p * 1)(src)
    length = (ctypes.c_int * 1)(len(src))
    gl.glShaderSource(shader, 1, arr, length)
    gl.glCompileShader(shader)
    status = ctypes.c_int()
    gl.glGetShaderiv(shader, gl.COMPILE_STATUS, ctypes.byref(status))
    if not status.value:
        log_len = ctypes.c_int()
        gl.glGetShaderiv(shader, gl.INFO_LOG_LENGTH, ctypes.byref(log_len))
        buf = ctypes.create_string_buffer(max(log_len.value, 1))
        gl.glGetShaderInfoLog(shader, len(buf), None, buf)
        numbered = "\n".join(f"{i + 1:3d}| {line}" for i, line in enumerate(source.splitlines()))
        raise ShaderError(f"{label} failed to compile:\n{buf.value.decode()}\n{numbered}")
    return shader


class Program:
    """A linked vertex+fragment program."""

    def __init__(self, vertex_src: str, fragment_src: str, label: str = "program"):
        self.label = label
        vs = _compile(gl.VERTEX_SHADER, vertex_src, f"{label}.vert")
        fs = _compile(gl.FRAGMENT_SHADER, fragment_src, f"{label}.frag")
        self.id = gl.glCreateProgram()
        gl.glAttachShader(self.id, vs)
        gl.glAttachShader(self.id, fs)
        gl.glLinkProgram(self.id)
        status = ctypes.c_int()
        gl.glGetProgramiv(self.id, gl.LINK_STATUS, ctypes.byref(status))
        if not status.value:
            log_len = ctypes.c_int()
            gl.glGetProgramiv(self.id, gl.INFO_LOG_LENGTH, ctypes.byref(log_len))
            buf = ctypes.create_string_buffer(max(log_len.value, 1))
            gl.glGetProgramInfoLog(self.id, len(buf), None, buf)
            raise ShaderError(f"{label} failed to link: {buf.value.decode()}")
        gl.glDeleteShader(vs)
        gl.glDeleteShader(fs)
        self._uniforms: dict[str, int] = {}
        self._attribs: dict[str, int] = {}

    def use(self) -> None:
        gl.glUseProgram(self.id)

    def uniform(self, name: str) -> int:
        loc = self._uniforms.get(name)
        if loc is None:
            loc = gl.glGetUniformLocation(self.id, name.encode())
            self._uniforms[name] = loc
        return loc

    def attrib(self, name: str) -> int:
        loc = self._attribs.get(name)
        if loc is None:
            loc = gl.glGetAttribLocation(self.id, name.encode())
            self._attribs[name] = loc
        return loc

    # -- setters (silently ignore uniforms the compiler optimised away) -----
    def set1i(self, name: str, value: int) -> None:
        loc = self.uniform(name)
        if loc >= 0:
            gl.glUniform1i(loc, int(value))

    def set1f(self, name: str, value: float) -> None:
        loc = self.uniform(name)
        if loc >= 0:
            gl.glUniform1f(loc, float(value))

    def set2f(self, name: str, x: float, y: float) -> None:
        loc = self.uniform(name)
        if loc >= 0:
            gl.glUniform2f(loc, float(x), float(y))

    def set4f(self, name: str, x: float, y: float, z: float, w: float) -> None:
        loc = self.uniform(name)
        if loc >= 0:
            gl.glUniform4f(loc, float(x), float(y), float(z), float(w))

    def close(self) -> None:
        if getattr(self, "id", 0):
            gl.glDeleteProgram(self.id)
            self.id = 0
