"""Video playback through GStreamer.

``picframe`` shelled out to VLC and let it paint its own window on top of the
pi3d output.  That works only where a window system exists, gives no control
over the handover between photo and video, and means two separate rendering
stacks fighting for the screen.

Here the video is decoded by GStreamer into frames that are uploaded as GL
textures and drawn by the same renderer as the photographs -- so a video
crossfades in and out exactly like a picture, keeps the overlays on top, and
needs no compositor.

GStreamer is the Raspberry Pi's native media stack: it uses the V4L2 stateless
decoder for HEVC on a Pi 5 and the hardware H.264 decoder on a Pi 4, both
through ``decodebin3``, with no configuration.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

_gst_ready: bool | None = None
Gst: Any = None
GstApp: Any = None


def available() -> bool:
    """True when PyGObject and the GStreamer typelibs are installed."""
    global _gst_ready, Gst, GstApp
    if _gst_ready is not None:
        return _gst_ready
    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        from gi.repository import Gst as _Gst
        from gi.repository import GstApp as _GstApp

        if not _Gst.is_initialized():
            _Gst.init(None)
        Gst, GstApp = _Gst, _GstApp
        _gst_ready = True
    except Exception as exc:
        _log.info("GStreamer unavailable (%s); videos will be skipped", exc)
        _gst_ready = False
    return _gst_ready


def _uri(path: str) -> str:
    from urllib.parse import quote

    return "file://" + quote(os.path.abspath(path))


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

def probe(path: str, timeout: float = 5.0) -> dict | None:
    """Dimensions, duration and rotation, without decoding the whole file."""
    if not available():
        return None
    try:
        import gi

        gi.require_version("GstPbutils", "1.0")
        from gi.repository import GstPbutils

        disc = GstPbutils.Discoverer.new(int(timeout * Gst.SECOND))
        info = disc.discover_uri(_uri(path))
        streams = info.get_video_streams()
        if not streams:
            return None
        v = streams[0]
        out = {
            "width": v.get_width(),
            "height": v.get_height(),
            "duration": info.get_duration() / Gst.SECOND if info.get_duration() else None,
            "orientation": 1,
            "taken_at": None,
        }
        tags = info.get_tags()
        if tags is not None:
            ok, value = tags.get_string("image-orientation")
            if ok:
                out["orientation"] = {
                    "rotate-0": 1, "rotate-180": 3, "rotate-90": 6, "rotate-270": 8,
                }.get(value, 1)
            ok, dt = tags.get_date_time("datetime")
            if ok and dt is not None:
                try:
                    out["taken_at"] = dt.to_unix()
                except Exception:
                    pass
        return out
    except Exception as exc:
        _log.debug("discoverer failed for %s: %s", path, exc)
        return None


def poster_frame(path: str, size: tuple[int, int], position: float = 0.1,
                 timeout: float = 8.0):
    """Grab a representative frame as a PIL image (used as the slide's poster)."""
    if not available():
        return None
    from PIL import Image

    w, h = size
    desc = (
        f'uridecodebin uri="{_uri(path)}" ! videoconvert ! videoscale '
        f"! video/x-raw,format=RGB,width={w},height={h},pixel-aspect-ratio=1/1 "
        f"! appsink name=sink max-buffers=1 drop=false sync=false"
    )
    pipeline = None
    try:
        pipeline = Gst.parse_launch(desc)
        sink = pipeline.get_by_name("sink")
        pipeline.set_state(Gst.State.PAUSED)
        pipeline.get_state(int(timeout * Gst.SECOND))
        dur_ok, duration = pipeline.query_duration(Gst.Format.TIME)
        if dur_ok and duration > 0:
            pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                int(duration * position),
            )
            pipeline.get_state(int(timeout * Gst.SECOND))
        sample = sink.emit("pull-preroll")
        if sample is None:
            return None
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            caps = sample.get_caps().get_structure(0)
            vw, vh = caps.get_value("width"), caps.get_value("height")
            stride = ((vw * 3) + 3) & ~3          # GStreamer pads rows to 4 bytes
            data = bytes(info.data)
            return Image.frombytes("RGB", (vw, vh), data, "raw", "RGB", stride, 1)
        finally:
            buf.unmap(info)
    except Exception as exc:
        _log.warning("could not extract a poster frame from %s: %s", path, exc)
        return None
    finally:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)


# --------------------------------------------------------------------------
# Playback
# --------------------------------------------------------------------------

@dataclass
class VideoFrame:
    data: bytes
    width: int
    height: int
    pts: float


class VideoPlayer:
    """Decodes into RGBA frames the renderer uploads.

    No GLib main loop is used: the bus is polled and frames are pulled from the
    render loop, which keeps everything on one thread and makes the timing of
    the photo/video handover explicit.
    """

    def __init__(self, size: tuple[int, int], *, mute: bool = False,
                 loop: bool = False, fit: str = "contain"):
        if not available():
            raise RuntimeError("GStreamer is not available")
        self.size = size
        self.mute = mute
        self.loop = loop
        self.fit = fit
        self._pipeline = None
        self._sink = None
        self._path: str | None = None
        self._eos = False
        self._error: str | None = None
        self._lock = threading.Lock()
        self._started = 0.0
        self.frame_size: tuple[int, int] = size

    # -- control -----------------------------------------------------------
    def play(self, path: str) -> bool:
        self.stop()
        w, h = self.size
        # videoscale with add-borders keeps the aspect ratio and pads, so the
        # texture handed to the renderer is always exactly screen sized and no
        # special case for video is needed anywhere else.
        borders = "false" if self.fit == "cover" else "true"
        sink_desc = (
            f"videoconvert ! videoscale add-borders={borders} "
            f"! video/x-raw,format=RGBA,width={w},height={h},pixel-aspect-ratio=1/1 "
            f"! appsink name=sink max-buffers=2 drop=true sync=true"
        )
        try:
            # The sink bin is built here rather than inside a parse_launch
            # string so the appsink reference is valid immediately; playbin3
            # does not expose its children until it reaches PAUSED.
            sink_bin = Gst.parse_bin_from_description(sink_desc, True)
            appsink = sink_bin.get_by_name("sink")
            if appsink is None:
                raise RuntimeError("appsink missing from the video sink bin")
            pipeline = Gst.ElementFactory.make("playbin3", None) or \
                Gst.ElementFactory.make("playbin", None)
            if pipeline is None:
                raise RuntimeError("neither playbin3 nor playbin is installed")
            pipeline.set_property("uri", _uri(path))
            pipeline.set_property("video-sink", sink_bin)
            # GST_PLAY_FLAG_VIDEO | GST_PLAY_FLAG_AUDIO
            pipeline.set_property("flags", 0x00000001 | 0x00000002)
        except Exception as exc:
            self._error = str(exc)
            _log.error("cannot build video pipeline: %s", exc)
            return False
        self._pipeline = pipeline
        self._sink = appsink
        if self.mute:
            try:
                pipeline.set_property("mute", True)
            except TypeError:  # pragma: no cover - very old playbin
                pass
        self._path = path
        self._eos = False
        self._error = None
        pipeline.set_state(Gst.State.PLAYING)
        self._started = time.monotonic()
        _log.info("playing video %s", os.path.basename(path))
        return True

    def pause(self, paused: bool = True) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.PAUSED if paused else Gst.State.PLAYING)

    def stop(self) -> None:
        with self._lock:
            if self._pipeline is not None:
                self._pipeline.set_state(Gst.State.NULL)
                self._pipeline = None
                self._sink = None
            self._path = None
            self._eos = False

    def close(self) -> None:
        self.stop()

    # -- state -------------------------------------------------------------
    @property
    def playing(self) -> bool:
        return self._pipeline is not None and not self._eos

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def position(self) -> float:
        if self._pipeline is None:
            return 0.0
        ok, pos = self._pipeline.query_position(Gst.Format.TIME)
        return pos / Gst.SECOND if ok else 0.0

    @property
    def duration(self) -> float:
        if self._pipeline is None:
            return 0.0
        ok, dur = self._pipeline.query_duration(Gst.Format.TIME)
        return dur / Gst.SECOND if ok else 0.0

    def set_volume(self, value: float) -> None:
        if self._pipeline is not None:
            self._pipeline.set_property("volume", max(0.0, min(1.0, value)))

    # -- frames ------------------------------------------------------------
    def poll(self) -> VideoFrame | None:
        """Non-blocking: returns the next decoded frame, or None."""
        if self._pipeline is None:
            return None
        self._pump_bus()
        if self._eos or self._sink is None:
            return None
        sample = self._sink.emit("try-pull-sample", 0)
        if sample is None:
            return None
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            struct = sample.get_caps().get_structure(0)
            w, h = struct.get_value("width"), struct.get_value("height")
            self.frame_size = (w, h)
            return VideoFrame(
                data=bytes(info.data),
                width=w,
                height=h,
                pts=buf.pts / Gst.SECOND if buf.pts != Gst.CLOCK_TIME_NONE else 0.0,
            )
        finally:
            buf.unmap(info)

    def _pump_bus(self) -> None:
        bus = self._pipeline.get_bus()
        while True:
            msg = bus.pop_filtered(
                Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING
            )
            if msg is None:
                return
            if msg.type == Gst.MessageType.EOS:
                if self.loop and self._path:
                    self._pipeline.seek_simple(
                        Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0
                    )
                else:
                    self._eos = True
                    _log.debug("video reached end of stream")
            elif msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                self._error = str(err)
                self._eos = True
                _log.warning("video error: %s (%s)", err, debug)
            else:
                warn, _ = msg.parse_warning()
                _log.debug("video warning: %s", warn)
