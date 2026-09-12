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
6. release the buffer that was on screen before this one -- but only once the
   flip that replaced it has actually been confirmed
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
        mode: str = "",
    ):
        # Every handle the partial teardown below may have to give back, so
        # that a failure half way through this constructor does not leave the
        # process holding DRM master.  Falling back to the headless backend
        # while still being master means a black screen and a dead console,
        # with nothing on the machine able to take the display back.
        self._drm = None
        self._gbm_dev = None
        self._gbm_surf = None
        self._dpy = None
        self._ctx = None
        self._surf = None
        try:
            self._setup(device, connector, vsync, width, height, mode)
        except BaseException:
            self._teardown()
            raise

    def _setup(
        self,
        device: str | None,
        connector: str | None,
        vsync: bool,
        width: int | None,
        height: int | None,
        mode: str = "",
    ) -> None:
        self._drm = drm.DrmDevice(device)
        self._drm.become_master()
        self.output = self._drm.find_output(connector, mode)
        self._vsync = vsync
        # ``vsync: false`` only means anything if the driver can flip outside
        # the vblank; without the capability the flip is queued normally and
        # simply not waited for, which is still cheaper than a full modeset.
        self._async_flip = self._drm.supports_async_flip
        if not vsync and not self._async_flip:
            _log.info(
                "vsync:false requested but the driver does not advertise async "
                "page flips; presenting on the vblank without waiting for it"
            )

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
        #: The buffer currently being scanned out.  It stays locked until a
        #: later flip has been *confirmed* to have replaced it.
        self._front_bo: int | None = None
        #: Buffers handed to KMS whose flip has not completed yet, oldest
        #: first.  Releasing one of these would give GBM back a buffer the
        #: display engine is still reading from.
        self._queued_bos: list[int] = []
        self._modeset_done = False
        #: False while the connector is switched off.  A disabled CRTC refuses
        #: every page flip, so there is nothing to present until it is back.
        self._powered = True
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
        try:
            self._present(bo)
        except BaseException:
            # A locked buffer that is never handed back is gone for good.  The
            # GBM surface holds three or four; once they are all locked,
            # eglSwapBuffers fails with EGL_BAD_ALLOC on every frame from then
            # on and the screen stays black long after whatever caused it --
            # only a restart brings the picture back.  Whatever goes wrong
            # between locking a buffer and queueing it may cost this frame, but
            # it must not cost the appliance.
            if bo not in self._queued_bos and bo != self._front_bo:
                # Unless it did reach the queue: releasing a buffer that is
                # already booked as queued or on screen would hand GBM the
                # same buffer twice.
                gbm.lib.gbm_surface_release_buffer(self._gbm_surf, bo)
            raise

    def _present(self, bo: int) -> None:
        if not self._powered:
            # The CRTC is disabled, so the kernel would refuse this flip -- and
            # a frame that cannot be shown is not worth a framebuffer, a queue
            # entry or a log line.  The buffer goes straight back; the picture
            # is drawn again by the modeset that wakes the panel.
            gbm.lib.gbm_surface_release_buffer(self._gbm_surf, bo)
            return

        fb_id = self._framebuffer_for(bo)

        if not self._modeset_done:
            # The first frame -- and the first one after the panel was woken --
            # needs a modeset to light the output up; drmModeSetCrtc is
            # synchronous, so the buffer is on screen by the time it returns
            # and counts as a confirmed presentation.
            if self._drm.flip_pending:
                # A flip queued just before the panel went dark never completes
                # on a disabled CRTC, and drmModeSetCrtc answers EBUSY while
                # one is still in the air.
                self._drm.wait_flip(0.2)
            try:
                self._drm.set_crtc(self.output, fb_id)
            except OSError:
                # Leaving _modeset_done False means the next frame tries again,
                # which is what a panel still waking up needs; raising here
                # would only cost the render loop a frame and a traceback.
                _log.warning("modeset failed; retrying on the next frame", exc_info=True)
                gbm.lib.gbm_surface_release_buffer(self._gbm_surf, bo)
                return
            self._modeset_done = True
            self._queued_bos.append(bo)
            self._flip_completed()
            return

        if self._vsync:
            result = self._drm.page_flip(self.output.crtc_id, fb_id)
        else:
            # Presenting an unsynced frame used to mean a full drmModeSetCrtc,
            # which is slower than a flip, reprograms the whole CRTC and still
            # waits for the vblank internally -- the opposite of what the
            # setting promises.  An async flip is the actual "show it now"
            # primitive; the completion is collected at the start of the next
            # frame instead of blocking this one.
            result = self._drm.page_flip(
                self.output.crtc_id, fb_id,
                asynchronous=self._async_flip, wait=False,
            )
        # Completions collected here always belong to buffers queued earlier,
        # so they are accounted for before this frame's buffer joins the queue.
        for _ in range(result.completions):
            self._flip_completed()
        if result.queued:
            self._queued_bos.append(bo)
        else:
            # The kernel refused the flip, so this buffer never reached the
            # display and giving it straight back keeps the small GBM pool from
            # running dry.  The frame is simply dropped; the next one redraws.
            gbm.lib.gbm_surface_release_buffer(self._gbm_surf, bo)

    def _flip_completed(self) -> None:
        """The oldest queued buffer is now the one on screen.

        Only at this point may the buffer it replaced go back to GBM.  The old
        code released the previous front buffer whether the flip had completed
        or not, which handed a buffer the scanout engine was still reading back
        to the allocator -- tearing, or a frame drawn over the live picture.
        """
        if not self._queued_bos:
            return
        shown = self._queued_bos.pop(0)
        if self._front_bo is not None and self._front_bo != shown:
            gbm.lib.gbm_surface_release_buffer(self._gbm_surf, self._front_bo)
        self._front_bo = shown

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
        if self._drm is None:
            # A constructor that failed has already given everything back.
            return
        # If the schedule turned the panel off, DPMS is still off here.  Once
        # master is dropped nothing owns the connector any more, so the monitor
        # would stay dark with no way back short of a reboot -- including for
        # the console the user drops back to.  Turning it on must never be able
        # to stop the rest of the teardown, hence the bare guard.
        try:
            self.set_power(True)
        except Exception:  # pragma: no cover - driver dependent
            _log.debug("could not switch DPMS back on during shutdown", exc_info=True)
        # A flip still in the air makes the legacy drmModeSetCrtc fail with
        # EBUSY, so its event is collected first -- and it is what tells us the
        # buffers below are no longer being read.
        try:
            self._drm.wait_flip(0.1)
        except Exception:  # pragma: no cover - driver dependent
            pass
        try:
            self._drm.restore_crtc(self.output)
        except Exception:  # pragma: no cover
            pass
        for fb in self._fb_cache.values():
            self._drm.remove_framebuffer(fb)
        self._fb_cache.clear()
        # The CRTC has been restored, so nothing of ours is being scanned out
        # any more and every buffer still held -- the front one plus anything
        # whose flip was never confirmed -- can go back to GBM.
        for bo in self._queued_bos:
            gbm.lib.gbm_surface_release_buffer(self._gbm_surf, bo)
        self._queued_bos.clear()
        if self._front_bo is not None:
            gbm.lib.gbm_surface_release_buffer(self._gbm_surf, self._front_bo)
            self._front_bo = None
        self._teardown()

    def _teardown(self) -> None:
        """Give back EGL, GBM and DRM, each step independent of the others.

        This runs both from ``close()`` and from a constructor that failed part
        way through, so every step has to cope with the handle not existing.
        """
        # Truthiness, not "is not None": a failed eglCreateWindowSurface hands
        # back a perfectly real object that merely wraps NULL.
        if self._dpy:
            try:
                egl.libegl.eglMakeCurrent(self._dpy, None, None, None)
                if self._surf:
                    egl.libegl.eglDestroySurface(self._dpy, self._surf)
                if self._ctx:
                    egl.libegl.eglDestroyContext(self._dpy, self._ctx)
                egl.libegl.eglTerminate(self._dpy)
            except Exception:  # pragma: no cover - driver dependent
                _log.debug("EGL teardown failed", exc_info=True)
        self._surf = self._ctx = self._dpy = None
        if self._gbm_surf:
            gbm.lib.gbm_surface_destroy(self._gbm_surf)
        self._gbm_surf = None
        if self._gbm_dev:
            gbm.lib.gbm_device_destroy(self._gbm_dev)
        self._gbm_dev = None
        if self._drm is not None:
            try:
                self._drm.drop_master()
            finally:
                self._drm.close()
            self._drm = None

    # -- power -------------------------------------------------------------
    def set_power(self, on: bool) -> bool:
        """Switch the panel off and -- the harder half -- back on again.

        Switching off is what DPMS is for: the connector is powered down and
        the HDMI link goes with it, which is why the television reports *no
        cable* rather than *no signal*.  Coming back is not symmetrical.  With
        the link gone the set has to re-acquire it, and on vc4 the DPMS
        property alone frequently does not produce that: the property reads
        back as on, the CRTC is active again, and the panel keeps showing "no
        cable" because nothing re-drove the mode.

        So waking up asks for both.  DPMS goes on, and the next frame is made
        to re-run ``drmModeSetCrtc`` -- the same call that lit the screen at
        startup, and the one legacy path that reprograms the whole CRTC and
        re-establishes the link.  It costs one modeset per wake-up.
        """
        ok = self._drm.set_dpms(self.output.connector_id, on)
        if not ok:
            # No DPMS property on this connector.  Say so honestly, so the
            # caller can fall back to the backlight or to the shader -- and
            # leave _powered alone, or a frame would never be presented again.
            return False
        self._powered = bool(on)
        if on:
            self._modeset_done = False
        return True

    def get_power(self) -> bool | None:
        return self._drm.get_dpms(self.output.connector_id)
