"""GLSL program objects with cached uniform locations."""

from __future__ import annotations

import ctypes
import logging

from . import gl

_log = logging.getLogger(__name__)


class ShaderError(RuntimeError):
    pass


def _compile(kind: int, source: str, label: str) -> int:
    """Compile one shader, or raise ``ShaderError`` leaving nothing behind.

    A shader that fails to compile still exists as a GL object.  Since a failed
    transition is something the renderer recovers from rather than dies of, the
    name has to be deleted here or every failed attempt would leak one.
    """
    shader = gl.glCreateShader(kind)
    if not shader:
        raise ShaderError(f"{label}: glCreateShader returned 0")
    ok = False
    try:
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
        ok = True
        return shader
    finally:
        if not ok:
            gl.glDeleteShader(shader)


class Program:
    """A linked vertex+fragment program."""

    def __init__(self, vertex_src: str, fragment_src: str, label: str = "program"):
        self.label = label
        # Set first so a failure anywhere below still leaves a Program whose
        # close() is harmless, and so the cleanup here can tell "no program
        # object yet" from "one that has to be deleted".
        self.id = 0
        self._uniforms: dict[str, int] = {}
        self._attribs: dict[str, int] = {}
        vs = fs = 0
        try:
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
        except Exception:
            # A link failure leaves a perfectly real program object behind, and
            # the caller may well go on to try another shader, so it has to be
            # deleted rather than left to the process exit.
            self.close()
            raise
        finally:
            # The shaders are attached; deleting the names now means they go
            # away with the program, on the success and the failure path alike.
            if vs:
                gl.glDeleteShader(vs)
            if fs:
                gl.glDeleteShader(fs)

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
