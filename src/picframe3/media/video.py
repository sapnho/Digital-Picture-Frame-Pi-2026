"""Video playback through GStreamer.

``picframe`` shelled out to VLC and let it paint its own window on top of the
pi3d output.  That works only where a window system exists, gives no control
over the handover between photo and video, and means two separate rendering
stacks fighting for the screen.

Here the video is decoded by GStreamer into frames that are uploaded as GL
textures and drawn by the same renderer as the photographs -- so a video
crossfades in and out exactly like a picture, keeps the overlays on top, and
needs no compositor.

GStreamer is the Raspberry Pi's native media stack: it reaches the V4L2
decoders through ``decodebin3`` with no configuration -- the H.264 block and
the HEVC block on a Pi 4, the HEVC block on a Pi 5 -- and falls back to the CPU
for anything those cannot take.  What the boards can and cannot do is written
down in ``_hw_decode_note`` below, which says so in the log rather than leaving
a stuttering picture unexplained.
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
# What the board can decode
# --------------------------------------------------------------------------

_PI_MODEL: str | None = None


def pi_model() -> str:
    """The board name out of the device tree, cached; empty off a Pi."""
    global _PI_MODEL
    if _PI_MODEL is None:
        _PI_MODEL = ""
        for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
            try:
                with open(path, "rb") as fh:
                    _PI_MODEL = fh.read().decode(errors="replace").strip("\x00 \n")
                break
            except OSError:
                continue
    return _PI_MODEL


def _hw_decode_note(width: int, height: int, codec: str = "",
                    model: str | None = None) -> str | None:
    """Why this clip will stutter on this board, or None if it will not.

    A Pi 4 decodes H.264 up to 1080p60 and HEVC up to 4Kp60; there is no 4K
    H.264 decoder on the chip, so such a file goes to the CPU and arrives at a
    few frames a second.  Even where the decoder copes, a 4K frame is 33 MB of
    RGBA that the pipeline writes, the player copies and the renderer uploads
    -- some 4 GB/s at 30 fps, which is about all the memory bandwidth a Pi 4
    has, and the 4K scanout wants its share too.  So on a Pi 4 the ceiling for
    video is 1080p whatever the codec.  A Pi 5 has no H.264 decoder at all but
    a CPU fast enough to do it in software, and three times the bandwidth.
    """
    model = pi_model() if model is None else model
    if "Raspberry Pi 4" not in model and "Raspberry Pi 400" not in model:
        return None
    if width <= 1920 and height <= 1080:
        return None
    what = f"{width}x{height}"
    if "h264" in codec or "avc" in codec:
        why = ("the Pi 4 has no H.264 decoder above 1080p, so this is decoded "
               "on the CPU")
    elif "h265" in codec or "hevc" in codec:
        why = ("the Pi 4 decodes HEVC in hardware, but a frame this size costs "
               "more memory bandwidth than the board has")
    else:
        why = "a frame this size is beyond what the Pi 4 can move per frame"
    return (f"{what} video: {why}. Expect stutter -- 1080p plays smoothly on "
            f"this board, 4K wants a Pi 5.")

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
        caps = v.get_caps()
        note = _hw_decode_note(out["width"], out["height"],
                               caps.to_string().lower() if caps else "")
        if note:
            _log.warning("%s: %s", os.path.basename(path), note)
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
    deadline = int(timeout * Gst.SECOND)
    try:
        pipeline = Gst.parse_launch(desc)
        sink = pipeline.get_by_name("sink")
        pipeline.set_state(Gst.State.PAUSED)
        # The return of get_state() is the whole point of calling it.  A file
        # whose codec is missing or whose moov atom is damaged never reaches
        # PAUSED, and the ASYNC or FAILURE that says so was thrown away here --
        # after which "pull-preroll", which blocks until a preroll arrives and
        # has no timeout, waited for a frame that was never coming.  That is a
        # loader thread gone for good, and after a handful of broken files the
        # frame has no loader threads left.
        change, _state, _pending = pipeline.get_state(deadline)
        if change != Gst.StateChangeReturn.SUCCESS:
            _log.info("no poster frame from %s: the pipeline did not preroll (%s)",
                      path, change)
            return None
        dur_ok, duration = pipeline.query_duration(Gst.Format.TIME)
        if dur_ok and duration > 0:
            pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                int(duration * position),
            )
            change, _state, _pending = pipeline.get_state(deadline)
            if change != Gst.StateChangeReturn.SUCCESS:
                _log.debug("seek into %s did not settle; using the first frame", path)
        # try-pull-preroll is the same call with a deadline on it.
        sample = sink.emit("try-pull-preroll", deadline)
        if sample is None:
            _log.info("no poster frame from %s: nothing prerolled within %.0fs",
                      path, timeout)
            return None
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            caps = sample.get_caps().get_structure(0)
            vw, vh = caps.get_value("width"), caps.get_value("height")
            stride = ((vw * 3) + 3) & ~3          # GStreamer pads rows to 4 bytes
            # frombytes copies into the new image, so the mapped buffer can be
            # handed over as it is: one copy of a full frame, not two.
            return Image.frombytes("RGB", (vw, vh), info.data, "raw", "RGB", stride, 1)
        finally:
            buf.unmap(info)
    except Exception as exc:
        _log.warning("could not extract a poster frame from %s: %s", path, exc)
        return None
    finally:
        # Always, on every path: a pipeline left in PAUSED keeps the hardware
        # decoder, and there is exactly one of those on a Pi.
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
                 loop: bool = False, fit: str = "contain",
                 max_seconds: float = 0.0):
        if not available():
            raise RuntimeError("GStreamer is not available")
        self.size = size
        self.mute = mute
        self.loop = loop
        self.fit = fit
        #: Wall-clock seconds of playback after which the player stops itself;
        #: 0 means "play to the end".  The player enforces this, rather than the
        #: slideshow timer, because only the player knows when it has really
        #: finished -- the frame waits for that signal.  With ``loop`` on, this
        #: is the window the video repeats inside.
        self.max_seconds = float(max_seconds)
        self._pipeline = None
        self._sink = None
        self._path: str | None = None
        self._eos = False
        self._error: str | None = None
        self._lock = threading.Lock()
        self._started = 0.0
        self._paused = False
        self._paused_at = 0.0
        #: Whether the decoder has already been handed back (see _finish).  The
        #: pipeline object is kept afterwards so that ``playing``, ``elapsed``
        #: and the rest still answer for the video that has just ended.
        self._released = False
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
            # GST_PLAY_FLAG_VIDEO = 0x1, GST_PLAY_FLAG_AUDIO = 0x2.  Muting
            # playbin only turns the volume down: the audio branch is still
            # built, and on a Pi with no audio device configured autoaudiosink
            # fails to start and takes the whole pipeline into ERROR -- a muted
            # video that does not play at all.  With the flag cleared no audio
            # sink is created in the first place, and nothing can fail in it.
            flags = 0x00000001 if self.mute else 0x00000001 | 0x00000002
            pipeline.set_property("flags", flags)
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
        self._released = False
        pipeline.set_state(Gst.State.PLAYING)
        self._started = time.monotonic()
        self._paused = False
        _log.info("playing video %s", os.path.basename(path))
        return True

    def pause(self, paused: bool = True) -> None:
        if self._pipeline is None or self._released:
            return
        # A paused frame is not playing, so the time it spends paused must not
        # count against ``max_seconds``.
        if paused and not self._paused:
            self._paused_at = time.monotonic()
        elif not paused and self._paused:
            self._started += time.monotonic() - self._paused_at
        self._paused = paused
        self._pipeline.set_state(Gst.State.PAUSED if paused else Gst.State.PLAYING)

    def stop(self) -> None:
        with self._lock:
            if self._pipeline is not None:
                self._pipeline.set_state(Gst.State.NULL)
                self._pipeline = None
                self._sink = None
            self._path = None
            self._eos = False
            self._paused = False
            self._released = False

    def close(self) -> None:
        self.stop()

    # -- state -------------------------------------------------------------
    @property
    def playing(self) -> bool:
        return self._pipeline is not None and not self._eos

    @property
    def elapsed(self) -> float:
        """Seconds since playback started, across repeats."""
        if self._pipeline is None:
            return 0.0
        if self._paused:
            return self._paused_at - self._started
        return time.monotonic() - self._started

    def expired(self) -> bool:
        return self.max_seconds > 0 and self.elapsed >= self.max_seconds

    def _finish(self) -> None:
        """Stop feeding frames and give the decoder back.

        The last frame stays on screen regardless -- it was uploaded as a
        texture and the renderer keeps it until the crossfade replaces it, so
        nothing here is what holds the picture.  What the pipeline does hold in
        PAUSED is the V4L2 decoder, of which a Pi has one: leaving it there
        until the next ``play()`` meant a still picture between two videos kept
        the hardware decoder busy, and the next video could fail to start
        because of it.  NULL releases it and the frozen frame is unaffected.
        """
        self._eos = True
        if self._pipeline is not None and not self._released:
            self._pipeline.set_state(Gst.State.NULL)
            self._released = True

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
        if not self._eos and self.expired():
            self._finish()
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
            # The copy stays.  info.data is only valid until the unmap in the
            # finally below, and the caller uploads the frame after poll() has
            # returned, so handing out the mapped memory would be a read of
            # freed buffer -- a crash, not a slow texture.  Removing the copy
            # means moving the GL upload inside the map, which is a renderer
            # change; the poster path above avoids it because PIL copies for us.
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
                # Looping only repeats while there is time left in the window;
                # without that check the frame would sit on one video forever.
                if self.loop and self._path and not self.expired():
                    self._pipeline.seek_simple(
                        Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0
                    )
                else:
                    self._finish()
                    _log.debug("video reached end of stream")
            elif msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                self._error = str(err)
                self._eos = True
                _log.warning("video error: %s (%s)", err, debug)
            else:
                warn, _ = msg.parse_warning()
                _log.debug("video warning: %s", warn)
