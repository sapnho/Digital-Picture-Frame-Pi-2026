"""Offscreen backend: surfaceless EGL rendering into a framebuffer object.

This is what makes the renderer testable.  Every frame the production path
draws on a Pi can be drawn here and read back as pixels, so transitions, mats,
text layout and colour handling are all verifiable without hardware.
"""

from __future__ import annotations

import logging

from . import egl, gl
from .backend import Backend, DisplayInfo

_log = logging.getLogger(__name__)


class HeadlessBackend(Backend):
    def __init__(self, width: int = 1920, height: int = 1080):
        exts = egl.client_extensions()
        if "EGL_MESA_platform_surfaceless" not in exts and "EGL_KHR_platform_gbm" not in exts:
            raise RuntimeError("EGL has no surfaceless platform; install mesa EGL drivers")

        self._dpy = egl.get_platform_display(egl.PLATFORM_SURFACELESS_MESA, None)
        if not self._dpy:
            raise RuntimeError("eglGetPlatformDisplay(surfaceless) returned no display")
        egl.initialize(self._dpy)
        egl.check(egl.libegl.eglBindAPI(egl.OPENGL_ES_API), "eglBindAPI")

        config = egl.choose_config(
            self._dpy,
            [
                egl.SURFACE_TYPE, egl.PBUFFER_BIT,
                egl.RENDERABLE_TYPE, egl.OPENGL_ES3_BIT,
                egl.RED_SIZE, 8, egl.GREEN_SIZE, 8, egl.BLUE_SIZE, 8, egl.ALPHA_SIZE, 8,
            ],
        )
        self._ctx = egl.create_context(self._dpy, config, 3, 0)
        egl.make_current(self._dpy, None, None, self._ctx)

        self.info = DisplayInfo(
            width=width,
            height=height,
            refresh_hz=60.0,
            name=gl.get_string(gl.RENDERER) or "offscreen",
            backend="headless",
        )

        # Colour target the renderer draws into.
        self._tex = gl.gen(gl.glGenTextures)
        gl.glBindTexture(gl.TEXTURE_2D, self._tex)
        gl.glTexImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, width, height, 0,
                        gl.RGBA, gl.UNSIGNED_BYTE, None)
        gl.glTexParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
        gl.glTexParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
        self._fbo = gl.gen(gl.glGenFramebuffers)
        gl.glBindFramebuffer(gl.FRAMEBUFFER, self._fbo)
        gl.glFramebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, self._tex, 0)
        status = gl.glCheckFramebufferStatus(gl.FRAMEBUFFER)
        if status != gl.FRAMEBUFFER_COMPLETE:
            raise RuntimeError(f"offscreen framebuffer incomplete: {hex(status)}")
        gl.glBindTexture(gl.TEXTURE_2D, 0)
        _log.info("headless backend ready on %s", self.info.name)

    # -- Backend -----------------------------------------------------------
    def make_current(self) -> None:
        egl.make_current(self._dpy, None, None, self._ctx)

    def begin_frame(self) -> None:
        gl.glBindFramebuffer(gl.FRAMEBUFFER, self._fbo)
        gl.glViewport(0, 0, self.info.width, self.info.height)

    def end_frame(self) -> None:
        gl.lib.glFlush()

    def close(self) -> None:
        gl.delete(gl.glDeleteFramebuffers, self._fbo)
        gl.delete(gl.glDeleteTextures, self._tex)
        egl.libegl.eglMakeCurrent(self._dpy, None, None, None)
        egl.libegl.eglDestroyContext(self._dpy, self._ctx)
        egl.libegl.eglTerminate(self._dpy)

    # -- Test helpers ------------------------------------------------------
    def read_pixels(self):
        """The colour buffer as an HxWx4 uint8 array."""
        gl.glBindFramebuffer(gl.FRAMEBUFFER, self._fbo)
        return self.capture()

    def save_png(self, path) -> None:
        from PIL import Image

        Image.fromarray(self.read_pixels(), "RGBA").convert("RGB").save(path)
