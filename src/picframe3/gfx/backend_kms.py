"""Direct-to-display backend: DRM/KMS modesetting with GBM buffers.

The frame becomes the only thing on the screen.  There is no compositor to
schedule against and no desktop session to keep alive, which removes an entire
class of failure -- the wlroots/labwc GPU resets, the "which display server am
I on" configuration branches, and the several hundred megabytes a desktop
session costs on a 1 GB Pi.

Frame lifecycle:

1. draw with GLES into the GBM surface's back buffer
2. ``eglSwapBuffers`` -- hands the buffer to GBM
3. ``gbm_surface_lock_front_buffer`` -- take ownership of the finished buffer
4. wrap it in a DRM framebuffer (cached per buffer object)
5. ``drmModePageFlip`` and wait for the vblank event
6. release the buffer that was on screen before this one
"""

from __future__ import annotations

import ctypes
import logging

from . import drm, egl, gbm, gl
from .backend import Backend, DisplayInfo

_log = logging.getLogger(__name__)


class KmsBackend(Backend):
    def __init__(
        self,
        device: str | None = None,
        connector: str | None = None,
        vsync: bool = True,
        width: int | None = None,
        height: int | None = None,
    ):
        self._drm = drm.DrmDevice(device)
        self._drm.become_master()
        self.output = self._drm.find_output(connector)
        self._vsync = vsync

        w, h = self.output.width, self.output.height
        if width and height and (width, height) != (w, h):
            _log.warning(
                "requested %dx%d but the mode is %dx%d; rendering at the native mode",
                width, height, w, h,
            )

        self._gbm_dev = gbm.create_device(self._drm.fd)
        self._format = gbm.FORMAT_XRGB8888
        self._gbm_surf = gbm.create_surface(
            self._gbm_dev, w, h, self._format, gbm.USE_SCANOUT | gbm.USE_RENDERING
        )

        if "EGL_KHR_platform_gbm" not in egl.client_extensions() and \
           "EGL_MESA_platform_gbm" not in egl.client_extensions():
            raise RuntimeError("EGL does not advertise the GBM platform")
        self._dpy = egl.get_platform_display(egl.PLATFORM_GBM_KHR, self._gbm_dev)
        if not self._dpy:
            raise RuntimeError("eglGetPlatformDisplay(GBM) returned no display")
        egl.initialize(self._dpy)
        egl.check(egl.libegl.eglBindAPI(egl.OPENGL_ES_API), "eglBindAPI")

        config = egl.choose_config_matching(
            self._dpy,
            [
                egl.SURFACE_TYPE, egl.WINDOW_BIT,
                egl.RENDERABLE_TYPE, egl.OPENGL_ES3_BIT,
                egl.RED_SIZE, 8, egl.GREEN_SIZE, 8, egl.BLUE_SIZE, 8, egl.ALPHA_SIZE, 0,
            ],
            visual_id=self._format,
        )
        try:
            self._ctx = egl.create_context(self._dpy, config, 3, 0)
        except egl.EGLError as exc:
            # Falling back to a GLES 2 context would only move the failure to
            # the first shader compile, with a far less useful message: every
            # shader here is ES 3.00.  Say what is actually wrong.
            raise RuntimeError(
                "this GPU does not provide OpenGL ES 3.0, which picframe3 needs. "
                "The Raspberry Pi 4, 400 and 5 do (Mesa's v3d driver); the Pi 2, 3 "
                "and Zero 2 W have VideoCore IV, which stops at ES 2.0."
            ) from exc

        self._surf = egl.EGLSurface(
            egl.libegl.eglCreateWindowSurface(self._dpy, config, self._gbm_surf, None)
        )
        if not self._surf:
            raise RuntimeError("eglCreateWindowSurface on the GBM surface failed")
        egl.make_current(self._dpy, self._surf, self._surf, self._ctx)
        egl.libegl.eglSwapInterval(self._dpy, 1 if vsync else 0)

        self.info = DisplayInfo(
            width=w,
            height=h,
            refresh_hz=self.output.mode.refresh_hz,
            name=self.output.name,
            backend="kms",
            physical_mm=self.output.mm_size,
        )

        self._fb_cache: dict[int, int] = {}      # gbm_bo pointer -> drm fb id
        self._front_bo: int | None = None
        self._modeset_done = False
        self._drm.save_crtc(self.output.crtc_id)
        _log.info(
            "KMS backend on %s: %s %dx%d@%.2fHz (%s)",
            self._drm.path, self.output.name, w, h,
            self.info.refresh_hz, gl.get_string(gl.RENDERER),
        )

    # -- Backend -----------------------------------------------------------
    def make_current(self) -> None:
        egl.make_current(self._dpy, self._surf, self._surf, self._ctx)

    def begin_frame(self) -> None:
        gl.glBindFramebuffer(gl.FRAMEBUFFER, 0)
        gl.glViewport(0, 0, self.info.width, self.info.height)

    def end_frame(self) -> None:
        egl.swap_buffers(self._dpy, self._surf)
        bo = gbm.lib.gbm_surface_lock_front_buffer(self._gbm_surf)
        if not bo:
            _log.error("gbm_surface_lock_front_buffer returned NULL; dropping frame")
            return
        fb_id = self._framebuffer_for(bo)

        if not self._modeset_done:
            self._drm.set_crtc(self.output, fb_id)
            self._modeset_done = True
        elif self._vsync:
            self._drm.page_flip(self.output.crtc_id, fb_id)
        else:
            self._drm.set_crtc(self.output, fb_id)

        if self._front_bo is not None and self._front_bo != bo:
            gbm.lib.gbm_surface_release_buffer(self._gbm_surf, self._front_bo)
        self._front_bo = bo

    def _framebuffer_for(self, bo: int) -> int:
        cached = self._fb_cache.get(bo)
        if cached is not None:
            return cached
        handle = gbm.lib.gbm_bo_get_handle(ctypes.c_void_p(bo)).u32
        stride = gbm.lib.gbm_bo_get_stride(ctypes.c_void_p(bo))
        fmt = gbm.lib.gbm_bo_get_format(ctypes.c_void_p(bo))
        fb_id = self._drm.add_framebuffer(self.info.width, self.info.height, fmt, handle, stride)
        self._fb_cache[bo] = fb_id
        return fb_id

    def close(self) -> None:
        try:
            self._drm.restore_crtc(self.output)
        except Exception:  # pragma: no cover
            pass
        for fb in self._fb_cache.values():
            self._drm.remove_framebuffer(fb)
        self._fb_cache.clear()
        if self._front_bo is not None:
            gbm.lib.gbm_surface_release_buffer(self._gbm_surf, self._front_bo)
            self._front_bo = None
        egl.libegl.eglMakeCurrent(self._dpy, None, None, None)
        egl.libegl.eglDestroySurface(self._dpy, self._surf)
        egl.libegl.eglDestroyContext(self._dpy, self._ctx)
        egl.libegl.eglTerminate(self._dpy)
        gbm.lib.gbm_surface_destroy(self._gbm_surf)
        gbm.lib.gbm_device_destroy(self._gbm_dev)
        self._drm.drop_master()
        self._drm.close()

    # -- power -------------------------------------------------------------
    def set_power(self, on: bool) -> bool:
        return self._drm.set_dpms(self.output.connector_id, on)

    def get_power(self) -> bool | None:
        return self._drm.get_dpms(self.output.connector_id)
