"""ctypes binding for libdrm's mode-setting API.

This is the layer that lets the frame own the screen outright: no X server, no
Wayland compositor, no desktop session.  The app opens ``/dev/dri/cardN``,
picks the connected output and its preferred mode, and page-flips buffers in
sync with the display's vblank.

It is also where display power lives.  ``picframe`` shelled out to ``vcgencmd``,
``xset`` or ``wlr-randr`` depending on the environment; here the connector's
DPMS property is set directly, which works the same on every Pi and needs no
external tool.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import select
import time
from collections.abc import Iterator
from dataclasses import dataclass

_log = logging.getLogger(__name__)

lib = ctypes.CDLL(ctypes.util.find_library("drm") or "libdrm.so.2")

DRM_DISPLAY_MODE_LEN = 32
DRM_PROP_NAME_LEN = 32

# drmModeConnection
DRM_MODE_CONNECTED = 1
DRM_MODE_DISCONNECTED = 2
DRM_MODE_UNKNOWNCONNECTION = 3

DRM_MODE_TYPE_PREFERRED = 1 << 3
DRM_MODE_PAGE_FLIP_EVENT = 0x01
#: Flip as soon as the scanout engine can rather than at the next vblank.  Only
#: usable when the driver advertises DRM_CAP_ASYNC_PAGE_FLIP; asking for it
#: without that capability fails the ioctl outright.
DRM_MODE_PAGE_FLIP_ASYNC = 0x02
DRM_MODE_FLAG_INTERLACE = 1 << 4

DRM_CAP_ASYNC_PAGE_FLIP = 0x7

DRM_MODE_DPMS_ON = 0
DRM_MODE_DPMS_STANDBY = 1
DRM_MODE_DPMS_SUSPEND = 2
DRM_MODE_DPMS_OFF = 3

CONNECTOR_TYPES = {
    0: "Unknown", 1: "VGA", 2: "DVI-I", 3: "DVI-D", 4: "DVI-A", 5: "Composite",
    6: "SVIDEO", 7: "LVDS", 8: "Component", 9: "DIN", 10: "DP", 11: "HDMI-A",
    12: "HDMI-B", 13: "TV", 14: "eDP", 15: "Virtual", 16: "DSI", 17: "DPI",
    18: "WRITEBACK", 19: "SPI", 20: "USB",
}


# --------------------------------------------------------------------------
# Structures
# --------------------------------------------------------------------------

class ModeInfo(ctypes.Structure):
    _fields_ = [
        ("clock", ctypes.c_uint32),
        ("hdisplay", ctypes.c_uint16), ("hsync_start", ctypes.c_uint16),
        ("hsync_end", ctypes.c_uint16), ("htotal", ctypes.c_uint16),
        ("hskew", ctypes.c_uint16),
        ("vdisplay", ctypes.c_uint16), ("vsync_start", ctypes.c_uint16),
        ("vsync_end", ctypes.c_uint16), ("vtotal", ctypes.c_uint16),
        ("vscan", ctypes.c_uint16),
        ("vrefresh", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("name", ctypes.c_char * DRM_DISPLAY_MODE_LEN),
    ]

    @property
    def refresh_hz(self) -> float:
        """Exact refresh from the timings; ``vrefresh`` is rounded to an int."""
        denom = self.htotal * self.vtotal
        if not denom:
            return float(self.vrefresh)
        hz = (self.clock * 1000.0) / denom
        if self.flags & DRM_MODE_FLAG_INTERLACE:
            hz *= 2
        return hz


class Res(ctypes.Structure):
    _fields_ = [
        ("count_fbs", ctypes.c_int), ("fbs", ctypes.POINTER(ctypes.c_uint32)),
        ("count_crtcs", ctypes.c_int), ("crtcs", ctypes.POINTER(ctypes.c_uint32)),
        ("count_connectors", ctypes.c_int), ("connectors", ctypes.POINTER(ctypes.c_uint32)),
        ("count_encoders", ctypes.c_int), ("encoders", ctypes.POINTER(ctypes.c_uint32)),
        ("min_width", ctypes.c_uint32), ("max_width", ctypes.c_uint32),
        ("min_height", ctypes.c_uint32), ("max_height", ctypes.c_uint32),
    ]


class Connector(ctypes.Structure):
    _fields_ = [
        ("connector_id", ctypes.c_uint32),
        ("encoder_id", ctypes.c_uint32),
        ("connector_type", ctypes.c_uint32),
        ("connector_type_id", ctypes.c_uint32),
        ("connection", ctypes.c_int),
        ("mmWidth", ctypes.c_uint32), ("mmHeight", ctypes.c_uint32),
        ("subpixel", ctypes.c_int),
        ("count_modes", ctypes.c_int),
        ("modes", ctypes.POINTER(ModeInfo)),
        ("count_props", ctypes.c_int),
        ("props", ctypes.POINTER(ctypes.c_uint32)),
        ("prop_values", ctypes.POINTER(ctypes.c_uint64)),
        ("count_encoders", ctypes.c_int),
        ("encoders", ctypes.POINTER(ctypes.c_uint32)),
    ]

    @property
    def name(self) -> str:
        return f"{CONNECTOR_TYPES.get(self.connector_type, 'Unknown')}-{self.connector_type_id}"


class Encoder(ctypes.Structure):
    _fields_ = [
        ("encoder_id", ctypes.c_uint32),
        ("encoder_type", ctypes.c_uint32),
        ("crtc_id", ctypes.c_uint32),
        ("possible_crtcs", ctypes.c_uint32),
        ("possible_clones", ctypes.c_uint32),
    ]


class Crtc(ctypes.Structure):
    _fields_ = [
        ("crtc_id", ctypes.c_uint32),
        ("buffer_id", ctypes.c_uint32),
        ("x", ctypes.c_uint32), ("y", ctypes.c_uint32),
        ("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
        ("mode_valid", ctypes.c_int),
        ("mode", ModeInfo),
        ("gamma_size", ctypes.c_int),
    ]


class PropertyRes(ctypes.Structure):
    _fields_ = [
        ("prop_id", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("name", ctypes.c_char * DRM_PROP_NAME_LEN),
        ("count_values", ctypes.c_int),
        ("values", ctypes.POINTER(ctypes.c_uint64)),
        ("count_enums", ctypes.c_int),
        ("enums", ctypes.c_void_p),
        ("count_blobs", ctypes.c_int),
        ("blob_ids", ctypes.POINTER(ctypes.c_uint32)),
    ]


PAGE_FLIP_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p
)
VBLANK_CB = PAGE_FLIP_CB


def _noop_event(fd, seq, sec, usec, data):  # pragma: no cover - C callback
    """The event only has to be dequeued; its timestamp tells us nothing new."""
    return None


class EventContext(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_int),
        ("vblank_handler", VBLANK_CB),
        ("page_flip_handler", PAGE_FLIP_CB),
        ("page_flip_handler2", ctypes.c_void_p),
        ("sequence_handler", ctypes.c_void_p),
    ]


# Building a libffi closure costs an mmap and an icache flush, and the two here
# were being rebuilt for every single page flip -- sixty times a second for the
# whole life of the frame.  They never change, so they are built once and the
# context they live in is reused; keeping the module-level references alive is
# also what stops ctypes from collecting the trampolines out from under libdrm.
_VBLANK_HANDLER = VBLANK_CB(_noop_event)
_PAGE_FLIP_HANDLER = PAGE_FLIP_CB(_noop_event)
_EVENT_CONTEXT = EventContext(
    version=2,
    vblank_handler=_VBLANK_HANDLER,
    page_flip_handler=_PAGE_FLIP_HANDLER,
    page_flip_handler2=None,
    sequence_handler=None,
)


# --------------------------------------------------------------------------
# Prototypes
# --------------------------------------------------------------------------
lib.drmModeGetResources.restype = ctypes.POINTER(Res)
lib.drmModeGetResources.argtypes = [ctypes.c_int]
lib.drmModeFreeResources.argtypes = [ctypes.POINTER(Res)]
lib.drmModeGetConnector.restype = ctypes.POINTER(Connector)
lib.drmModeGetConnector.argtypes = [ctypes.c_int, ctypes.c_uint32]
lib.drmModeGetConnectorCurrent.restype = ctypes.POINTER(Connector)
lib.drmModeGetConnectorCurrent.argtypes = [ctypes.c_int, ctypes.c_uint32]
lib.drmModeFreeConnector.argtypes = [ctypes.POINTER(Connector)]
lib.drmModeGetEncoder.restype = ctypes.POINTER(Encoder)
lib.drmModeGetEncoder.argtypes = [ctypes.c_int, ctypes.c_uint32]
lib.drmModeFreeEncoder.argtypes = [ctypes.POINTER(Encoder)]
lib.drmModeGetCrtc.restype = ctypes.POINTER(Crtc)
lib.drmModeGetCrtc.argtypes = [ctypes.c_int, ctypes.c_uint32]
lib.drmModeFreeCrtc.argtypes = [ctypes.POINTER(Crtc)]
lib.drmModeSetCrtc.restype = ctypes.c_int
lib.drmModeSetCrtc.argtypes = [
    ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_int, ctypes.POINTER(ModeInfo),
]
lib.drmModeAddFB2.restype = ctypes.c_int
lib.drmModeAddFB2.argtypes = [
    ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
]
lib.drmModeRmFB.restype = ctypes.c_int
lib.drmModeRmFB.argtypes = [ctypes.c_int, ctypes.c_uint32]
lib.drmModePageFlip.restype = ctypes.c_int
lib.drmModePageFlip.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_uint32, ctypes.c_void_p]
lib.drmHandleEvent.restype = ctypes.c_int
lib.drmHandleEvent.argtypes = [ctypes.c_int, ctypes.POINTER(EventContext)]
lib.drmModeGetProperty.restype = ctypes.POINTER(PropertyRes)
lib.drmModeGetProperty.argtypes = [ctypes.c_int, ctypes.c_uint32]
lib.drmModeFreeProperty.argtypes = [ctypes.POINTER(PropertyRes)]
lib.drmModeConnectorSetProperty.restype = ctypes.c_int
lib.drmModeConnectorSetProperty.argtypes = [ctypes.c_int, ctypes.c_uint32,
                                            ctypes.c_uint32, ctypes.c_uint64]
lib.drmSetMaster.restype = ctypes.c_int
lib.drmSetMaster.argtypes = [ctypes.c_int]
lib.drmDropMaster.restype = ctypes.c_int
lib.drmDropMaster.argtypes = [ctypes.c_int]
lib.drmIsMaster.restype = ctypes.c_int
lib.drmIsMaster.argtypes = [ctypes.c_int]
lib.drmGetDeviceNameFromFd2.restype = ctypes.c_char_p
lib.drmGetDeviceNameFromFd2.argtypes = [ctypes.c_int]
# Old libdrm builds may not export drmGetCap; a missing capability query only
# means "assume the feature is absent", which is the safe answer anyway.
_drm_get_cap = getattr(lib, "drmGetCap", None)
if _drm_get_cap is not None:
    _drm_get_cap.restype = ctypes.c_int
    _drm_get_cap.argtypes = [ctypes.c_int, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64)]


# --------------------------------------------------------------------------
# Pythonic layer
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FlipResult:
    """What one ``page_flip`` call actually achieved.

    Both halves matter to the caller and neither implies the other: a flip can
    be refused while an older one completes, and a flip can be queued while
    nothing completes.  ``queued`` says whether the kernel took the new buffer,
    ``completions`` how many earlier flips were confirmed finished.
    """

    queued: bool
    completions: int = 0


@dataclass
class Output:
    connector_id: int
    name: str
    mode: ModeInfo
    crtc_id: int
    encoder_id: int
    mm_size: tuple[int, int]

    @property
    def width(self) -> int:
        return self.mode.hdisplay

    @property
    def height(self) -> int:
        return self.mode.vdisplay


class DrmDevice:
    """An open DRM master on a card node."""

    #: Total time a flip event is waited for before the frame is given up on.
    #: One ``select`` timeout is not enough to conclude anything -- a signal or
    #: a momentarily busy driver can cut the wait short -- but waiting forever
    #: would hang the whole application on a wedged GPU, so the wait is bounded.
    flip_deadline = 3.0

    def __init__(self, path: str | None = None):
        self.path = path or self._autodetect()
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_CLOEXEC)
        except PermissionError as exc:
            raise PermissionError(
                f"cannot open {self.path}: add the user to the 'video' and 'render' groups"
            ) from exc
        self._saved_crtc: Crtc | None = None
        self._dpms_prop: int | None = None
        self._dpms_connector: int | None = None
        #: True between queueing a flip and collecting its completion event.
        #: Without it a timed-out wait leaves the event in the fd, and the next
        #: wait returns instantly on that stale event while the buffer it
        #: belongs to is still being scanned out.
        self._flip_pending = False
        #: When that flip was queued, so a completion that never arrives can be
        #: written off rather than freezing the frame for good.
        self._flip_since = 0.0

    @staticmethod
    def _autodetect() -> str:
        import glob

        cards = sorted(glob.glob("/dev/dri/card*"))
        if not cards:
            raise FileNotFoundError("no /dev/dri/card* nodes; is a KMS driver loaded?")
        # Prefer a card that actually has a connected output.
        for card in cards:
            try:
                fd = os.open(card, os.O_RDWR | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                res = lib.drmModeGetResources(fd)
                if res:
                    try:
                        for i in range(res.contents.count_connectors):
                            conn = lib.drmModeGetConnector(fd, res.contents.connectors[i])
                            if conn:
                                connected = conn.contents.connection == DRM_MODE_CONNECTED
                                lib.drmModeFreeConnector(conn)
                                if connected:
                                    return card
                    finally:
                        lib.drmModeFreeResources(res)
            finally:
                os.close(fd)
        return cards[0]

    # -- discovery ---------------------------------------------------------
    def outputs(self, include_unknown: bool = True) -> list[Output]:
        """Connected outputs with their preferred mode.

        ``include_unknown`` keeps connectors whose state the driver reports as
        "unknown" -- composite out and some DSI panels do this, and treating
        them as disconnected is a known regression in wlroots-based stacks.
        """
        res = lib.drmModeGetResources(self.fd)
        if not res:
            raise RuntimeError(f"drmModeGetResources failed on {self.path}")
        found: list[Output] = []
        try:
            r = res.contents
            for i in range(r.count_connectors):
                cptr = lib.drmModeGetConnector(self.fd, r.connectors[i])
                if not cptr:
                    continue
                try:
                    c = cptr.contents
                    ok = c.connection == DRM_MODE_CONNECTED or (
                        include_unknown and c.connection == DRM_MODE_UNKNOWNCONNECTION
                    )
                    if not ok or c.count_modes == 0:
                        continue
                    mode = self._pick_mode(c)
                    crtc_id = self._pick_crtc(r, c)
                    if crtc_id is None:
                        _log.warning("no free CRTC for connector %s", c.name)
                        continue
                    found.append(
                        Output(
                            connector_id=c.connector_id,
                            name=c.name,
                            mode=ModeInfo.from_buffer_copy(mode),
                            crtc_id=crtc_id,
                            encoder_id=c.encoder_id,
                            mm_size=(c.mmWidth, c.mmHeight),
                        )
                    )
                finally:
                    lib.drmModeFreeConnector(cptr)
        finally:
            lib.drmModeFreeResources(res)
        return found

    @staticmethod
    def _pick_mode(c: Connector) -> ModeInfo:
        best = c.modes[0]
        for i in range(c.count_modes):
            m = c.modes[i]
            if m.type & DRM_MODE_TYPE_PREFERRED:
                return m
            if (m.hdisplay * m.vdisplay, m.refresh_hz) > (best.hdisplay * best.vdisplay, best.refresh_hz):
                best = m
        return best

    def _pick_crtc(self, r: Res, c: Connector) -> int | None:
        if c.encoder_id:
            eptr = lib.drmModeGetEncoder(self.fd, c.encoder_id)
            if eptr:
                try:
                    if eptr.contents.crtc_id:
                        return eptr.contents.crtc_id
                finally:
                    lib.drmModeFreeEncoder(eptr)
        for i in range(c.count_encoders):
            eptr = lib.drmModeGetEncoder(self.fd, c.encoders[i])
            if not eptr:
                continue
            try:
                possible = eptr.contents.possible_crtcs
            finally:
                lib.drmModeFreeEncoder(eptr)
            for j in range(r.count_crtcs):
                if possible & (1 << j):
                    return r.crtcs[j]
        return None

    def find_output(self, name: str | None) -> Output:
        outs = self.outputs()
        if not outs:
            raise RuntimeError(f"{self.path} has no connected output")
        if name:
            for o in outs:
                if o.name.lower() == name.lower():
                    return o
            raise RuntimeError(
                f"connector {name!r} not found; available: {', '.join(o.name for o in outs)}"
            )
        return outs[0]

    # -- capabilities ------------------------------------------------------
    def get_cap(self, cap: int) -> int:
        if _drm_get_cap is None:
            return 0
        value = ctypes.c_uint64()
        if _drm_get_cap(self.fd, cap, ctypes.byref(value)) != 0:
            return 0
        return value.value

    @property
    def supports_async_flip(self) -> bool:
        """Whether the driver accepts DRM_MODE_PAGE_FLIP_ASYNC.

        Asking for an async flip on a driver that does not support it makes the
        ioctl fail, so this has to be checked rather than attempted.
        """
        return bool(self.get_cap(DRM_CAP_ASYNC_PAGE_FLIP))

    # -- master ------------------------------------------------------------
    def become_master(self) -> None:
        if lib.drmIsMaster(self.fd):
            return
        if lib.drmSetMaster(self.fd) != 0:
            raise PermissionError(
                "could not become DRM master -- another compositor owns the display. "
                "Stop the desktop session (or boot Raspberry Pi OS Lite) and retry."
            )

    def drop_master(self) -> None:
        lib.drmDropMaster(self.fd)

    # -- modeset / flip ----------------------------------------------------
    def add_framebuffer(self, width: int, height: int, fourcc: int,
                        handle: int, pitch: int, offset: int = 0) -> int:
        handles = (ctypes.c_uint32 * 4)(handle, 0, 0, 0)
        pitches = (ctypes.c_uint32 * 4)(pitch, 0, 0, 0)
        offsets = (ctypes.c_uint32 * 4)(offset, 0, 0, 0)
        fb = ctypes.c_uint32()
        rc = lib.drmModeAddFB2(self.fd, width, height, fourcc, handles, pitches,
                               offsets, ctypes.byref(fb), 0)
        if rc != 0:
            raise OSError(-rc, f"drmModeAddFB2 failed for {width}x{height} fmt {fourcc:#x}")
        return fb.value

    def remove_framebuffer(self, fb_id: int) -> None:
        lib.drmModeRmFB(self.fd, fb_id)

    def save_crtc(self, crtc_id: int) -> None:
        ptr = lib.drmModeGetCrtc(self.fd, crtc_id)
        if ptr:
            self._saved_crtc = Crtc.from_buffer_copy(ptr.contents)
            lib.drmModeFreeCrtc(ptr)

    def set_crtc(self, out: Output, fb_id: int) -> None:
        conns = (ctypes.c_uint32 * 1)(out.connector_id)
        rc = lib.drmModeSetCrtc(self.fd, out.crtc_id, fb_id, 0, 0, conns, 1,
                                ctypes.byref(out.mode))
        if rc != 0:
            raise OSError(-rc, "drmModeSetCrtc failed")

    def restore_crtc(self, out: Output) -> None:
        if self._saved_crtc is None:
            return
        s = self._saved_crtc
        conns = (ctypes.c_uint32 * 1)(out.connector_id)
        mode = ctypes.byref(s.mode) if s.mode_valid else None
        lib.drmModeSetCrtc(self.fd, s.crtc_id, s.buffer_id, s.x, s.y, conns, 1, mode)

    def page_flip(self, crtc_id: int, fb_id: int, timeout: float = 1.0, *,
                  asynchronous: bool = False, wait: bool = True) -> FlipResult:
        """Queue a flip and report what happened, as a :class:`FlipResult`.

        The caller needs the completion count rather than just "did it work":
        a GBM buffer may only be handed back once the flip that replaced it *on
        screen* has been confirmed, and a flip that was merely queued proves
        nothing about the buffer it displaced.

        Any completion still outstanding from an earlier flip is collected
        first.  The kernel queues exactly one event per flip, so leaving one in
        the fd would make the next wait return immediately on the stale event
        and report a flip that is still in the air.

        ``asynchronous`` asks the driver to flip as soon as it can instead of
        at the next vblank, and ``wait=False`` returns once the flip is queued
        -- together they are the ``vsync: false`` path, where the completion is
        collected at the start of the following frame instead.
        """
        collected = 0
        if self._flip_pending:
            collected += self._collect_flip(timeout)
            if self._flip_pending:
                # Still in the air: the CRTC will not take another flip, and
                # trying anyway would only earn an EBUSY.
                return FlipResult(queued=False, completions=collected)
        flags = DRM_MODE_PAGE_FLIP_EVENT
        if asynchronous:
            flags |= DRM_MODE_PAGE_FLIP_ASYNC
        rc = lib.drmModePageFlip(self.fd, crtc_id, fb_id, flags, None)
        if rc != 0:
            if -rc in (16, 11):  # EBUSY / EAGAIN -- a flip is still pending
                return FlipResult(queued=False, completions=collected)
            raise OSError(-rc, "drmModePageFlip failed")
        self._flip_pending = True
        self._flip_since = time.monotonic()
        if wait:
            collected += self._collect_flip(timeout)
        return FlipResult(queued=True, completions=collected)

    def wait_flip(self, timeout: float = 1.0) -> bool:
        """Block until the pending flip completes.  True if one was collected."""
        return self._collect_flip(timeout) > 0

    def _collect_flip(self, timeout: float = 1.0) -> int:
        """Dequeue the outstanding flip event, retrying up to ``timeout``.

        A single ``select`` timeout used to be treated as "this flip is lost",
        which was both premature and unrecoverable: the event stayed queued and
        every later wait read it instead of the one it was waiting for.

        The caller's timeout is honoured rather than raised to
        ``flip_deadline``, because this blocks the loop that also serves HTTP,
        MQTT and the video tick, and because the shutdown path deliberately
        asks for a tenth of a second.  ``flip_deadline`` is the *give-up*
        point: once a flip has been outstanding that long, waiting for it
        again on every frame would freeze the picture for good, so the flip is
        written off, the pending flag is cleared and the next frame is allowed
        to try.  A driver that then delivers the stale event early costs one
        frame of tearing; refusing to flip ever again costs the appliance.
        """
        if not self._flip_pending:
            return 0
        if self._flip_since and time.monotonic() - self._flip_since > self.flip_deadline:
            _log.warning("writing off a page flip that never completed after "
                         "%.1fs; resuming", self.flip_deadline)
            self._flip_pending = False
            self._flip_since = 0.0
            return 0
        step = max(0.001, min(timeout, 0.25))
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                _log.debug("no page-flip event within %.2fs; still pending", timeout)
                return 0
            try:
                ready, _, _ = select.select([self.fd], [], [], min(step, remaining))
            except InterruptedError:  # pragma: no cover - signal during select
                continue
            if not ready:
                _log.debug("page flip still in flight after %.1fs; waiting", step)
                continue
            lib.drmHandleEvent(self.fd, ctypes.byref(_EVENT_CONTEXT))
            self._flip_pending = False
            self._flip_since = 0.0
            return 1

    @property
    def flip_pending(self) -> bool:
        return self._flip_pending

    # -- power -------------------------------------------------------------
    def _find_dpms(self, connector_id: int) -> int | None:
        if self._dpms_prop is not None and self._dpms_connector == connector_id:
            return self._dpms_prop
        cptr = lib.drmModeGetConnector(self.fd, connector_id)
        if not cptr:
            return None
        try:
            c = cptr.contents
            for i in range(c.count_props):
                pptr = lib.drmModeGetProperty(self.fd, c.props[i])
                if not pptr:
                    continue
                try:
                    if pptr.contents.name == b"DPMS":
                        self._dpms_prop = c.props[i]
                        self._dpms_connector = connector_id
                        return self._dpms_prop
                finally:
                    lib.drmModeFreeProperty(pptr)
        finally:
            lib.drmModeFreeConnector(cptr)
        return None

    def set_dpms(self, connector_id: int, on: bool) -> bool:
        prop = self._find_dpms(connector_id)
        if prop is None:
            return False
        value = DRM_MODE_DPMS_ON if on else DRM_MODE_DPMS_OFF
        return lib.drmModeConnectorSetProperty(self.fd, connector_id, prop, value) == 0

    def get_dpms(self, connector_id: int) -> bool | None:
        cptr = lib.drmModeGetConnector(self.fd, connector_id)
        if not cptr:
            return None
        try:
            c = cptr.contents
            for i in range(c.count_props):
                pptr = lib.drmModeGetProperty(self.fd, c.props[i])
                if not pptr:
                    continue
                try:
                    if pptr.contents.name == b"DPMS":
                        return c.prop_values[i] == DRM_MODE_DPMS_ON
                finally:
                    lib.drmModeFreeProperty(pptr)
        finally:
            lib.drmModeFreeConnector(cptr)
        return None

    def close(self) -> None:
        if getattr(self, "fd", None) is not None and self.fd >= 0:
            # Closing the fd discards any queued event with it, so nothing is
            # left pending for a later device on the same card to trip over.
            self._flip_pending = False
            self._flip_since = 0.0
            os.close(self.fd)
            self.fd = -1


def list_outputs() -> Iterator[tuple[str, Output]]:
    """Enumerate every output on every card.  Used by ``picframe3 doctor``."""
    import glob

    for card in sorted(glob.glob("/dev/dri/card*")):
        try:
            dev = DrmDevice(card)
        except OSError:
            continue
        try:
            for out in dev.outputs():
                yield card, out
        except Exception:  # pragma: no cover
            continue
        finally:
            dev.close()
