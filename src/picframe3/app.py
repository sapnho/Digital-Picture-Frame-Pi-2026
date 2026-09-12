"""The application: wiring, the render loop, and the slideshow state machine."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from datetime import datetime
from typing import Any

from . import __version__, uischema
from .config import Config
from .control.power import BacklightControl, PowerSchedule
from .events import Action, Bus, Command, State
from .gfx import Renderer, Slide, Texture, create_backend, transitions
from .gfx import overlays as overlay_builders
from .gfx.textstyle import TextStyle
from .health import Health
from .library.db import Library, Record
from .library.playlist import ANY_OTHER, Filters, Playlist
from .library.removed import RemovalLog
from .library.scanner import Scanner
from .media import PrepareOptions, SlideLoader, no_files_screen
from .media import geocode as geocode_module
from .media.geocode import Geocoder
from .media.mat import MatStyle
from .network import NetworkWatch

_log = logging.getLogger(__name__)


def _encode_png(pixels) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(pixels, "RGBA").convert("RGB").save(buf, "PNG", compress_level=3)
    return buf.getvalue()

class PicFrame:
    def __init__(self, config: Config):
        self.config = config
        self.bus = Bus()
        self.started = time.monotonic()
        self._stop = asyncio.Event()

        self.backend = None
        self.renderer: Renderer | None = None
        self.library: Library | None = None
        self.removals: RemovalLog | None = None
        self.scanner: Scanner | None = None
        self.playlist: Playlist | None = None
        self.loader: SlideLoader | None = None
        self.geocoder: Geocoder | None = None
        self.video = None
        self.backlight = BacklightControl()
        self.power = PowerSchedule(config.power.schedule, config.power.dim_schedule)
        self.health = Health(
            interval=config.health.interval,
            disk_path=self._health_disk_path(),
            enabled=config.health.enabled,
        )
        self.network = NetworkWatch(
            enabled=config.network.enabled,
            target=config.network.target,
            interface=config.network.interface,
            interval=config.network.interval,
            failures=config.network.failures,
            attempts=config.network.attempts,
            timeout=config.network.timeout,
            cooldown=config.network.cooldown,
            settle=config.network.settle,
            repair=config.network.repair,
        )

        self.current: list[Record] = []
        #: How the picture on screen was laid out (cover/contain/blur/mat).
        self._current_fit = ""
        #: Settings changed that only a fresh process will pick up, and whether
        #: anything has been changed but not yet written to the config file.
        self._restart_needed: set[str] = set()
        self._config_dirty = False
        #: Set by the restart command; the CLI reads it after the loop ends.
        self.restart_requested = False
        self.paused = bool(config.slideshow.paused)
        self.show_info = True
        self.display_on = True
        self._next_change_at = 0.0
        self._slide_started = 0.0
        self._clock_minute: str | None = None
        self._info_until = 0.0
        self._dirty = True
        self._frames = 0
        self._last_frame_at = 0.0
        self._fps = 0.0
        self._fps_window = time.monotonic()
        self._scanning = False
        self._video_started = False
        self._overlay_mtime: float | None = None
        self._placeholder_shown = False
        self._location_pending: set[int] = set()
        self._capture_request: asyncio.Future | None = None
        self._location_backfill_at = 0.0

    def _health_disk_path(self) -> str:
        """Which filesystem the free-space reading is about.

        The photographs', not the root filesystem: on a frame fed from a USB
        stick or a network share those are different disks, and the one that
        fills up is the one with the pictures on it.
        """
        configured = self.config.health.disk_path
        if configured:
            return os.path.expanduser(configured)
        folders = self.config.library.picture_folders
        return os.path.expanduser(folders[0]) if folders else ""

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def setup(self) -> None:
        cfg = self.config
        self.backend = create_backend(
            cfg.display.backend,
            device=cfg.display.device,
            connector=cfg.display.connector,
            vsync=cfg.display.vsync,
        )
        self.backend.make_current()
        self.renderer = Renderer(
            self.backend,
            background=tuple(cfg.display.background),
            transition=cfg.slideshow.transition,
            transition_time=cfg.slideshow.transition_time,
        )
        self.renderer.brightness = cfg.display.brightness
        self.renderer.rotate = cfg.display.rotate
        self.renderer.transition_pool = transitions.resolve_pool(
            cfg.slideshow.transition_choices)

        self.library = Library(cfg.library.database)
        self.removals = RemovalLog(cfg.library.deleted_folder)
        self.health.snapshot()   # kicks off the first reading on its own thread
        if cfg.geo.enabled:
            self.geocoder = Geocoder(
                os.path.expanduser(cfg.geo.cache),
                contact=cfg.geo.contact,
                enabled=cfg.geo.enabled,
                key_order=geocode_module.key_order_for(cfg.geo.detail, cfg.geo.key_order),
                suppress=cfg.geo.suppress,
                language=cfg.geo.language,
            )
        self.scanner = Scanner(
            self.library,
            cfg.library.picture_folders,
            follow_links=cfg.library.follow_links,
            geocoder=self.geocoder,
            include_videos=cfg.library.include_videos,
            ignore_hidden=cfg.library.ignore_hidden,
            exclude=cfg.library.exclude,
        )

        filters = Filters.from_dict(self.library.get_state("playlist_filters") or {})
        if cfg.library.subfolder:
            filters.subfolder = cfg.library.subfolder
        filters.include_videos = cfg.library.include_videos
        self.playlist = Playlist(
            self.library,
            order=cfg.slideshow.order if cfg.slideshow.shuffle else "name",
            filters=filters,
            recent_days=cfg.slideshow.recent_days,
            reshuffle_after=cfg.slideshow.reshuffle_after,
            portrait_pairs=cfg.slideshow.portrait_pairs,
        )
        self.loader = SlideLoader(
            (self.backend.width, self.backend.height), self._prepare_options()
        )
        _log.info("picframe3 %s ready on %s", __version__, self.backend.describe())

    def _prepare_options(self) -> PrepareOptions:
        v = self.config.viewer
        return PrepareOptions(
            fit=v.fit,
            fit_choices=tuple(v.fit_choices or ("mat",)),
            background=tuple(int(c * 255) for c in self.config.display.background[:3]),
            blur_amount=v.blur_amount,
            blur_zoom=v.blur_zoom,
            blur_dim=v.blur_dim,
            upscale_limit=v.upscale_limit,
            mat_style=MatStyle(
                style=v.mat_style,
                outer_color=tuple(v.mat_outer_color) if v.mat_outer_color else None,
                inner_color=tuple(v.mat_inner_color) if v.mat_inner_color else None,
                outer_border=v.mat_outer_border,
                inner_border=v.mat_inner_border,
                texture=v.mat_texture,
                inner_texture=v.mat_inner_texture,
                auto_inner_color=v.mat_auto_inner_color,
                bevel_width=v.mat_bevel_width,
                tolerance=v.mat_tolerance,
            ),
            portrait_pairs=self.config.slideshow.portrait_pairs,
            kenburns_headroom=(
                self.config.slideshow.kenburns_zoom
                if self.config.slideshow.kenburns else 1.0
            ),
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run(self) -> int:
        self.setup()
        tasks = [
            asyncio.create_task(self._command_loop(), name="commands"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance"),
        ]
        tasks.extend(await self._start_services())
        try:
            await self._render_loop()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.shutdown()
        return 0

    async def _start_services(self) -> list[asyncio.Task]:
        tasks: list[asyncio.Task] = []
        cfg = self.config
        if cfg.http.enabled:
            try:
                from .control.http import HttpServer

                server = HttpServer(self, cfg.http)
                tasks.append(asyncio.create_task(server.run(), name="http"))
            except Exception as exc:
                _log.error("web interface disabled: %s", exc)
        if cfg.mqtt.enabled:
            try:
                from .control.mqtt import MqttBridge

                bridge = MqttBridge(self, cfg.mqtt)
                tasks.append(asyncio.create_task(bridge.run(), name="mqtt"))
            except Exception as exc:
                _log.error("MQTT disabled: %s", exc)
        if cfg.input.keyboard or cfg.input.touch or cfg.input.mouse:
            try:
                from .control.inputs import InputWatcher

                watcher = InputWatcher(self, cfg.input)
                tasks.append(asyncio.create_task(watcher.run(), name="input"))
            except Exception as exc:
                _log.info("input devices unavailable: %s", exc)
        # Always started, even when the watcher is off: it checks nothing
        # while disabled, and switching it on in the settings then takes effect
        # at the next check rather than at the next restart.
        tasks.append(asyncio.create_task(self.network.run(), name="network"))
        if cfg.input.gpio_buttons:
            try:
                from .control.gpio import GpioButtons

                buttons = GpioButtons(self, cfg.input)
                tasks.append(asyncio.create_task(buttons.run(), name="gpio"))
            except Exception as exc:
                _log.info("GPIO buttons unavailable: %s", exc)
        return tasks

    async def _render_loop(self) -> None:
        assert self.renderer is not None
        await self._advance(initial=True)
        while not self._stop.is_set():
            now = time.monotonic()
            self._tick_video()
            self._tick_overlays(now)

            if (not self.paused and self.display_on
                    and now >= self._next_change_at):
                await self._advance()
                now = time.monotonic()

            if self._dirty or self._animating(now):
                self.renderer.draw(after_draw=self._capture_hook())
                self._dirty = False
                self._frames += 1
                self._last_frame_at = time.monotonic()

            await asyncio.sleep(self._sleep_for(time.monotonic()))

    # ------------------------------------------------------------------
    # Screenshots
    # ------------------------------------------------------------------
    def _capture_hook(self):
        """A hook for the renderer, or None when nobody is waiting for a frame."""
        request = self._capture_request
        if request is None or request.done():
            return None

        def grab() -> None:
            try:
                request.set_result(self.backend.capture())
            except Exception as exc:  # pragma: no cover - driver dependent
                if not request.done():
                    request.set_exception(exc)

        return grab

    async def screenshot(self, timeout: float = 15.0) -> bytes:
        """Exactly what is on the screen right now, as a PNG.

        There is no desktop here and therefore no screenshot tool, so the frame
        provides its own: the next frame it draws is read back before it is
        presented.  That makes "it looks wrong on the frame" something you can
        attach to a message rather than describe.

        The timeout is generous because the render loop can be busy for a
        second or two preparing a slide, and a capture that fails while the
        frame is merely busy would be worse than useless.
        """
        loop = asyncio.get_running_loop()
        request: asyncio.Future = loop.create_future()
        self._capture_request = request
        self._dirty = True                      # make sure a frame is drawn
        try:
            pixels = await asyncio.wait_for(request, timeout)
        finally:
            if self._capture_request is request:
                self._capture_request = None
        return await loop.run_in_executor(None, _encode_png, pixels)

    def _animating(self, now: float) -> bool:
        r = self.renderer
        if r is None:
            return False
        if r.in_transition:
            return True
        if self._video_playing() or self._video_pending():
            return True
        if (self.config.slideshow.kenburns and r.current is not None
                and r.current.kenburns
                and now - r.current.started < r.current.duration):
            return True
        # A caption fading in or out is the one thing that still moves on an
        # otherwise finished frame.  Without this the loop would idle at two
        # wake-ups a second and a 1.5 s fade would arrive in three visible
        # steps instead of a fade.
        if r.has_overlay("info") and self._info_fade(now)[1]:
            return True
        return False

    def _sleep_for(self, now: float) -> float:
        cfg = self.config.display
        if self._animating(now):
            # On KMS the page flip already blocked until the vblank, so all
            # that is left to wait is the rest of the frame budget.  Sleeping a
            # whole frame *on top of* the flip would beat against the panel's
            # refresh -- 50 ms one frame, 67 ms the next -- and that uneven
            # cadence is exactly what an even fade shows up.
            period = 1.0 / max(cfg.fps_limit, 1.0)
            return max(0.0, period - (now - self._last_frame_at))
        # Nothing is moving.  On KMS the last flipped frame stays on screen, so
        # the loop can idle almost completely -- it only has to wake often
        # enough to notice a command or the next slide becoming due.
        until_change = max(0.0, self._next_change_at - now)
        return min(0.5, max(0.05, until_change))

    # ------------------------------------------------------------------
    # Slides
    # ------------------------------------------------------------------
    async def _advance(self, *, initial: bool = False, backwards: bool = False) -> None:
        assert self.playlist and self.loader and self.renderer
        self._stop_video()
        group = self.playlist.previous() if backwards else self.playlist.next()
        if not group:
            self._show_placeholder()
            self._next_change_at = time.monotonic() + 10.0
            return

        metas = [r.as_meta() for r in group]
        for record, meta in zip(group, metas, strict=False):
            if record.location:
                meta.tags = meta.tags or []
        prepared = None
        if not backwards and self.loader.prefetched_for(metas):
            prepared = await self.loader.take_prefetched()
        if prepared is None:
            self.loader.cancel_prefetch()
            prepared = await self.loader.load(metas)
        if prepared is None:
            _log.warning("skipping unreadable file %s", metas[0].path)
            self.library.set_hidden(group[0].id, True)   # stop retrying forever
            self.playlist.refresh()
            await self._advance(initial=initial)
            return

        self._placeholder_shown = False
        self.current = group
        self._current_fit = prepared.fit
        kb = self.config.slideshow.kenburns and not prepared.is_video
        texture = Texture.from_image(
            prepared.image, srgb=True, mipmap=not prepared.is_video
        )
        duration = self._slide_duration(group)
        if kb:
            slide = Slide.with_random_pan(
                texture, duration=duration, zoom=self.config.slideshow.kenburns_zoom
            )
        else:
            slide = Slide(texture, fit="cover", duration=duration)
        slide.meta = prepared
        self.renderer.show(
            slide,
            transition=self.config.slideshow.transition,
            duration=0.0 if initial else self.config.slideshow.transition_time,
        )
        self._slide_started = time.monotonic()
        self._next_change_at = self._slide_started + duration
        self._video_started = False
        self._dirty = True

        for record in group:
            self.library.mark_played(record.id, self.playlist.round)
        self._build_info_overlay(group)
        self._info_until = self._slide_started + self.config.viewer.text_seconds
        self._request_location(group)

        upcoming = self.playlist.peek()
        if upcoming:
            self.loader.prefetch([r.as_meta() for r in upcoming])
        self._publish()

    def _request_location(self, group: list[Record]) -> None:
        """Resolve the place name for a picture that has coordinates but no name.

        picframe did this at display time, and that turns out to be the right
        moment: it means switching geocoding on later fills the library in as
        you watch it, rather than requiring a full re-index that a scan would
        skip anyway because none of the files changed.
        """
        if self.geocoder is None or self.scanner is None:
            return
        record = group[0]
        if record.location or record.latitude is None or record.longitude is None:
            return
        if record.id in self._location_pending:
            return
        self._location_pending.add(record.id)
        asyncio.create_task(self._resolve_location(record))

    async def _resolve_location(self, record: Record) -> None:
        try:
            loop = asyncio.get_running_loop()
            location = await loop.run_in_executor(
                None, self.scanner.resolve_location, record.latitude, record.longitude
            )
            if not location:
                return
            self.library.set_location(record.id, location)
            record.location = location
            if self.current and self.current[0].id == record.id:
                self._build_info_overlay(self.current)
                self._dirty = True
                self._publish()
        except Exception as exc:  # pragma: no cover - network
            _log.debug("could not resolve a place name: %s", exc)
        finally:
            self._location_pending.discard(record.id)

    def _restyle_locations(self) -> None:
        """Rewrite every place name after the wording settings change.

        The geocache holds Nominatim's raw replies, so this is a pass over the
        index and the cache with no network at all -- which is the whole point
        of caching the reply rather than the formatted string.
        """
        if self.geocoder is None:
            return
        cfg = self.config.geo
        self.geocoder.set_style(
            geocode_module.key_order_for(cfg.detail, cfg.key_order), cfg.suppress)
        changed = 0
        for file_id, lat, lon in self.library.with_position():
            name = self.geocoder.lookup(lat, lon, cached_only=True)
            record = self.library.get(file_id)
            if record is not None and (record.location or "") != (name or ""):
                self.library.set_location(file_id, name)
                changed += 1
        _log.info("place names rewritten as %r (%d changed)", cfg.detail, changed)
        if self.current:
            for record in self.current:
                fresh = self.library.get(record.id)
                if fresh is not None:
                    record.location = fresh.location
            self._build_info_overlay(self.current)
            self._dirty = True

    def _slide_duration(self, group: list[Record]) -> float:
        """How long this slide is expected to stay up.

        For a photograph this is the interval and that is the whole story.  For
        a video it is only an estimate -- the fade in, then the film -- used for
        the first deadline and for what the API reports.  What actually ends a
        video slide is the player saying it has finished, so a video runs for as
        long as it plays, however long the interval for a still picture is.
        """
        base = float(self.config.slideshow.interval)
        record = group[0]
        if not record.is_video:
            return base
        cfg = self.config.slideshow
        window = self._video_window()
        if cfg.video_loop:
            length = window
        else:
            length = float(record.duration or 0.0)
            if window > 0:
                length = min(length, window) if length else window
        if length <= 0:                      # duration unknown: the EOS decides
            return base
        return length + cfg.transition_time

    def _video_window(self) -> float:
        """The cap handed to the player: seconds of playback, 0 = to the end.

        ``video_loop`` repeats a short clip, so it needs a window to repeat
        inside -- ``video_max_seconds`` when set, otherwise the still-picture
        interval.  Without looping the window is just the cap, and 0 means the
        film plays out in full.
        """
        cfg = self.config.slideshow
        limit = float(cfg.video_max_seconds)
        if cfg.video_loop and limit <= 0:
            return float(cfg.interval)
        return max(0.0, limit)

    def _show_placeholder(self) -> None:
        # A filter that matches nothing is the one way a working frame goes
        # blank, and "No pictures yet" would send its owner looking at the
        # folder, the scan and the SD card.  Say which it is.
        filtered = (self.playlist is not None and self.playlist.filters.active
                    and (self.library.stats().get("files", 0) if self.library else 0))
        message = "Nothing matches the filter" if filtered else "No pictures yet"
        subtitle = (self.playlist.filters.describe() if filtered
                    else "Looking in " + ", ".join(self.config.library.picture_folders))
        if self._placeholder_shown == (message, subtitle) or self.renderer is None:
            return
        image = no_files_screen(
            (self.backend.width, self.backend.height),
            self.config.viewer.no_files_img,
            message=message,
            subtitle=subtitle,
        )
        self.renderer.show(Slide(Texture.from_image(image, srgb=True)), transition="fade")
        self._placeholder_shown = (message, subtitle)
        self.current = []
        self._dirty = True

    # ------------------------------------------------------------------
    # Video
    # ------------------------------------------------------------------
    def _video_playing(self) -> bool:
        return self.video is not None and self.video.playing

    def _video_pending(self) -> bool:
        """A video is on screen but has not been set going yet."""
        r = self.renderer
        if r is None or r.current is None or self._video_started:
            return False
        prepared = getattr(r.current, "meta", None)
        return bool(prepared is not None and getattr(prepared, "is_video", False))

    def _hold_slide(self) -> None:
        """Keep the slide up: the video decides when it is over, not the clock."""
        self._next_change_at = time.monotonic() + 0.5

    def _end_slide(self) -> None:
        self._next_change_at = min(self._next_change_at, time.monotonic())

    def _tick_video(self) -> None:
        renderer = self.renderer
        if renderer is None or renderer.current is None:
            return
        prepared = getattr(renderer.current, "meta", None)
        if prepared is None or not getattr(prepared, "is_video", False):
            return
        if not self._video_started:
            # The poster frame fades in first and the film starts on a fully
            # opaque picture -- a video running underneath a crossfade both
            # looks wrong and throws away its opening second.
            self._hold_slide()
            if renderer.in_transition:
                return
            self._start_video(prepared.video_path)
            return
        if self.video is None:               # poster only: no player available
            return
        frame = self.video.poll()
        if frame is not None:
            renderer.current.flip_v = True
            renderer.current.texture.update(frame.data, frame.width, frame.height)
            self._dirty = True
        if self.video.playing:
            # However long a still picture is given, a video gets its own
            # length: the deadline is pushed ahead for as long as it runs.
            self._hold_slide()
        else:
            self._end_slide()

    def _start_video(self, path: str | None) -> None:
        if not path:
            return
        try:
            from .media.video import VideoPlayer, available

            if not available():
                _log.info("GStreamer missing; showing the poster frame only")
                self._video_started = True
                # Nothing will move, so the poster is a still picture and gets
                # a still picture's time.
                self._next_change_at = (time.monotonic()
                                        + float(self.config.slideshow.interval))
                return
            if self.video is None:
                self.video = VideoPlayer(
                    (self.backend.width, self.backend.height),
                    mute=self.config.slideshow.video_mute,
                    loop=self.config.slideshow.video_loop,
                    max_seconds=self._video_window(),
                )
            else:
                # The player outlives a slide, so re-read the settings: they can
                # change under it from the web UI or MQTT.
                self.video.mute = self.config.slideshow.video_mute
                self.video.loop = self.config.slideshow.video_loop
                self.video.max_seconds = self._video_window()
            self.video.play(path)
            self._video_started = True
        except Exception as exc:
            _log.warning("cannot play %s: %s", path, exc)
            self._video_started = True
            self._end_slide()

    def _stop_video(self) -> None:
        if self.video is not None:
            self.video.stop()
        self._video_started = False

    # ------------------------------------------------------------------
    # Overlays
    # ------------------------------------------------------------------
    def _text_style(self, size: int, justify: str = "L", opacity: float = 1.0) -> TextStyle:
        v = self.config.viewer
        return TextStyle(
            font_path=v.font, size=size, justify=justify, opacity=opacity,
            margin_x=v.text_margin_x, margin_y=v.text_margin_y,
        )

    def _build_info_overlay(self, group: list[Record]) -> None:
        assert self.renderer is not None
        if not self.show_info or not self.config.viewer.show_text:
            self.renderer.set_overlay("info", None)
            return
        columns = [
            overlay_builders.format_info_lines(
                record.as_meta(),
                self.config.viewer.show_text,
                date_format=self.config.viewer.date_format,
                location=record.location,
                paused=self.paused and index == 0,
            )
            for index, record in enumerate(group[:2])
        ]
        lines = columns if len(columns) > 1 else columns[0]
        placement = overlay_builders.info_bar(
            lines,
            (self.backend.width, self.backend.height),
            self._text_style(self.config.viewer.text_size,
                             self.config.viewer.text_justify,
                             self.config.viewer.text_opacity),
            scrim_opacity=self.config.viewer.text_scrim,
            separator=self.config.viewer.text_separator,
        )
        if placement is None:
            self.renderer.set_overlay("info", None)
            return
        self.renderer.set_overlay("info", placement.image, x=placement.x, y=placement.y,
                                  alpha=0.0, z=10)

    @staticmethod
    def _ease(t: float) -> float:
        """Smoothstep: flat at both ends, steepest in the middle.

        A straight ramp starts and stops abruptly, which reads as a snap even
        at 30 fps; easing the two ends is what makes the caption look like it
        is arriving rather than being switched on.
        """
        t = max(0.0, min(1.0, t))
        return t * t * (3.0 - 2.0 * t)

    def _info_fade(self, now: float) -> tuple[float, bool]:
        """Caption opacity right now, and whether it is still on the move.

        The second value drives the render loop: while it is true the loop has
        to keep drawing, because nothing else on the frame is changing.
        """
        fade = max(0.001, min(1.5, self.config.viewer.text_seconds / 4))
        start = self._slide_started + self.config.slideshow.transition_time
        end = self._info_until
        if self.paused:
            return 1.0, False
        if now < start:
            # Wake up shortly before the fade is due, so its first step is not
            # whatever the idle sleep happens to land on.
            return 0.0, (start - now) <= 1.0
        if now < start + fade:
            return self._ease((now - start) / fade), True
        if now < end - fade:
            return 1.0, False
        if now < end:
            return self._ease((end - now) / fade), True
        return 0.0, False

    def _tick_overlays(self, now: float) -> None:
        renderer = self.renderer
        if renderer is None:
            return
        # Info text: fade in after the transition, hold, fade out.
        if renderer.has_overlay("info"):
            alpha = self._info_fade(now)[0]
            current = renderer.overlays["info"].alpha
            if abs(current - alpha) > 0.001:
                renderer.overlay_alpha("info", alpha)
                self._dirty = True

        # Clock: rebuilt only when the displayed string changes.
        if self.config.viewer.show_clock:
            extra = self._clock_extra()
            stamp = time.strftime(self.config.viewer.clock_format) + "\x00" + extra
            if stamp != self._clock_minute:
                self._clock_minute = stamp
                placement = overlay_builders.clock(
                    (self.backend.width, self.backend.height),
                    self._text_style(self.config.viewer.clock_size,
                                     self.config.viewer.clock_position[1],
                                     self.config.viewer.clock_opacity),
                    fmt=self.config.viewer.clock_format,
                    extra=extra,
                    position=self.config.viewer.clock_position,
                    offset_pct=tuple(self.config.viewer.clock_offset_pct),
                )
                if placement is not None:
                    renderer.set_overlay("clock", placement.image, x=placement.x,
                                         y=placement.y, alpha=1.0, z=20)
                    self._dirty = True
        elif renderer.has_overlay("clock"):
            renderer.set_overlay("clock", None)
            self._clock_minute = None
            self._dirty = True

        self._tick_user_overlay()

    def _clock_extra(self) -> str:
        """A second line under the time, taken from a file anything can write."""
        path = self.config.viewer.clock_extra_file
        if not path:
            return ""
        for candidate in (path, "/dev/shm/clock.txt"):   # picframe's old path
            try:
                with open(candidate, encoding="utf-8") as fh:
                    return fh.read(240).strip()
            except OSError:
                continue
        return ""

    def _tick_user_overlay(self) -> None:
        """Watch a PNG on disk so other software can draw on the frame."""
        path = self.config.viewer.overlay_image
        renderer = self.renderer
        if renderer is None or not path:
            return
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            if self._overlay_mtime is not None:
                renderer.set_overlay("user", None)
                self._overlay_mtime = None
                self._dirty = True
            return
        if mtime == self._overlay_mtime:
            return
        self._overlay_mtime = mtime
        try:
            from PIL import Image

            with Image.open(path) as img:
                image = img.convert("RGBA").resize(
                    (self.backend.width, self.backend.height), Image.LANCZOS
                )
            renderer.set_overlay("user", image, x=0, y=0, alpha=1.0, z=5)
            self._dirty = True
        except Exception as exc:
            _log.debug("cannot load overlay image %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    async def _command_loop(self) -> None:
        while not self._stop.is_set():
            command = await self.bus.get(timeout=0.5)
            if command is None:
                continue
            try:
                await self.handle(command)
            except Exception:
                _log.exception("command %s failed", command.action)

    async def handle(self, command: Command) -> None:
        action = command.action
        payload = command.payload
        _log.debug("command %s %s (from %s)", action.value, payload, command.source)

        if action is Action.NEXT:
            await self._advance()
        elif action is Action.PREVIOUS:
            await self._advance(backwards=True)
        elif action in (Action.PAUSE, Action.RESUME, Action.TOGGLE_PAUSE):
            self.paused = (
                True if action is Action.PAUSE
                else False if action is Action.RESUME
                else not self.paused
            )
            if self.video is not None:
                self.video.pause(self.paused)
            if not self.paused:
                self._next_change_at = time.monotonic() + self.config.slideshow.interval
            if self.current:
                self._build_info_overlay(self.current)
            self._dirty = True
        elif action is Action.JUMP:
            await self._jump(payload)
        elif action is Action.DELETE:
            await self._delete_current(source=command.source)
        elif action is Action.RESTORE:
            await self._restore_removed(command.payload.get("stored_as", ""))
        elif action in (Action.DISPLAY_ON, Action.DISPLAY_OFF, Action.DISPLAY_TOGGLE):
            want = (
                True if action is Action.DISPLAY_ON
                else False if action is Action.DISPLAY_OFF
                else not self.display_on
            )
            self.set_display(want)
        elif action is Action.BRIGHTNESS:
            value = float(payload.get("value", 1.0))
            self.set_brightness(value)
        elif action in (Action.INFO_TOGGLE, Action.INFO_SHOW):
            self.show_info = not self.show_info if action is Action.INFO_TOGGLE else True
            if self.current:
                self._build_info_overlay(self.current)
                self._info_until = time.monotonic() + self.config.viewer.text_seconds
                self._slide_started = min(self._slide_started, time.monotonic())
            self._dirty = True
        elif action is Action.CLOCK_TOGGLE:
            self.config.viewer.show_clock = not self.config.viewer.show_clock
            self._clock_minute = None
            self._dirty = True
        elif action is Action.SET_CONFIG:
            self._apply_setting(payload.get("key", ""), payload.get("value"))
        elif action is Action.SET_FILTERS:
            self.set_filters(payload)
            await self._advance()
        elif action is Action.RESCAN:
            asyncio.create_task(self._rescan())
        elif action is Action.RELOAD:
            self._reload_config()
        elif action is Action.RESTART:
            self.request_restart()
        elif action is Action.QUIT:
            self._stop.set()
        self._publish()

    # ------------------------------------------------------------------
    # Which pictures are in the running
    # ------------------------------------------------------------------
    def set_filters(self, patch: dict[str, Any]) -> None:
        """Apply a filter patch from any surface, and keep the rest in step.

        The payload is a patch, not a replacement: Home Assistant sends one
        text box at a time.  ``{"reset": true}`` clears the lot.

        The folder is the one filter that is also a setting
        (``library.subfolder``), because it is the one people want to survive
        a restart.  Writing it back here is what stops the settings page and
        the filter panel from showing two different answers.
        """
        if self.playlist is None:
            return
        new = self.playlist.filters.merged(patch)
        self.playlist.set_filters(new)
        if new.subfolder != self.config.library.subfolder:
            self.config.library.subfolder = new.subfolder
            self._config_dirty = True
        _log.info("filter: %s (%d pictures)", new.describe(), self.playlist.size)

    def folder_choices(self) -> list[str]:
        """The folders a dropdown may offer, relative to the picture roots.

        Relative, because ``/home/pi/Pictures/2024/Italy`` is unreadable in a
        Home Assistant select and matches exactly the same pictures as
        ``2024/Italy`` -- the filter looks for the text anywhere in the path.
        """
        if self.library is None:
            return []
        roots = [os.path.expanduser(p).rstrip("/")
                 for p in self.config.library.picture_folders]
        out: list[str] = []
        for path, _count in self.library.folders():
            relative = path
            for root in roots:
                if path == root:
                    relative = ""
                    break
                if path.startswith(root + "/"):
                    relative = path[len(root) + 1:]
                    break
            if relative and relative not in out:
                out.append(relative)
        return sorted(out)

    def _filters_payload(self) -> dict[str, Any]:
        if self.playlist is None:
            return {}
        payload = self.playlist.filters.as_dict()
        # A Home Assistant select warns on every state it has no option for,
        # so it is told plainly when the folder was set from somewhere else.
        subfolder = payload.get("subfolder") or ""
        payload["folder_choice"] = (
            subfolder if not subfolder or subfolder in self._folder_options
            else ANY_OTHER
        )
        payload["matching"] = self.playlist.size
        return payload

    @property
    def _folder_options(self) -> list[str]:
        """Cached: the state document is rebuilt several times a second, and
        walking the folder table each time to answer one dropdown would be a
        database query per frame."""
        now = time.monotonic()
        if now - getattr(self, "_folder_cache_at", -1e9) > 30.0:
            self._folder_cache_at = now
            self._folder_cache = self.folder_choices()
        return getattr(self, "_folder_cache", [])

    def _apply_setting(self, key: str, value: Any) -> None:
        if not key:
            return
        if key in uischema.SECRETS and value == uischema.REDACTED:
            return          # a masked secret read back and written unchanged
        try:
            applied = self.config.set(key, value)
        except (KeyError, ValueError, TypeError) as exc:
            _log.warning("cannot set %s=%r: %s", key, value, exc)
            return
        _log.info("config %s = %r", key, applied)
        self._config_dirty = True
        section = key.split(".")[0]
        if uischema.needs_restart(key):
            self._restart_needed.add(key)
        if section == "viewer":
            self.loader.options = self._prepare_options()
            self._clock_minute = None
            if self.current:
                self._build_info_overlay(self.current)
        elif section == "slideshow":
            self.renderer.transition_name = self.config.slideshow.transition
            self.renderer.transition_time = self.config.slideshow.transition_time
            self.renderer.transition_pool = transitions.resolve_pool(
                self.config.slideshow.transition_choices)
            self.playlist.portrait_pairs = self.config.slideshow.portrait_pairs
            self.playlist.recent_days = self.config.slideshow.recent_days
            if key.endswith("order"):
                self.playlist.set_order(self.config.slideshow.order)
            self.loader.options = self._prepare_options()
        elif section == "geo":
            self._restyle_locations()
        elif section == "display" and key.endswith("brightness"):
            self.set_brightness(self.config.display.brightness)
        elif section == "display" and key.endswith("background"):
            self.renderer.background = tuple(self.config.display.background)
        elif section == "logging" and key.endswith("level"):
            logging.getLogger().setLevel(
                getattr(logging, str(self.config.logging.level).upper(), logging.INFO))
        elif section == "display" and key.endswith("rotate"):
            self.renderer.rotate = self.config.display.rotate
            self._clock_minute = None
            if self.current:
                self._build_info_overlay(self.current)
        elif section == "health":
            self.health.enabled = self.config.health.enabled
            self.health.interval = max(5.0, float(self.config.health.interval))
            self.health.disk_path = self._health_disk_path()
            if self.health.enabled:
                self.health.snapshot()
        elif section == "network":
            # The loop reads these attributes on every pass, so every one of
            # them -- the watcher itself included -- takes effect at the next
            # check without a restart.
            cfg_net = self.config.network
            self.network.target = cfg_net.target
            self.network.interface = cfg_net.interface
            self.network.interval = max(10.0, float(cfg_net.interval))
            self.network.failures = max(1, int(cfg_net.failures))
            self.network.attempts = max(1, int(cfg_net.attempts))
            self.network.timeout = max(1.0, float(cfg_net.timeout))
            self.network.cooldown = max(0.0, float(cfg_net.cooldown))
            self.network.settle = max(0.0, float(cfg_net.settle))
            self.network.repair = cfg_net.repair
            self.network.enabled = cfg_net.enabled
        elif section == "power":
            self.power = PowerSchedule(self.config.power.schedule,
                                       self.config.power.dim_schedule)
        elif section == "library" and key.endswith("subfolder"):
            self.playlist.filters.subfolder = self.config.library.subfolder
            self.playlist.refresh()
        self._dirty = True

    def _reload_config(self) -> None:
        if not self.config.source_path:
            return
        try:
            fresh = Config.from_file(self.config.source_path)
        except Exception as exc:
            _log.error("cannot reload config: %s", exc)
            return
        self.config = fresh
        self._restart_needed.clear()
        self._config_dirty = False
        self.loader.options = self._prepare_options()
        self.power = PowerSchedule(fresh.power.schedule, fresh.power.dim_schedule)
        self.renderer.transition_name = fresh.slideshow.transition
        self.renderer.transition_time = fresh.slideshow.transition_time
        self.renderer.transition_pool = transitions.resolve_pool(
            fresh.slideshow.transition_choices)
        self._clock_minute = None
        self._dirty = True
        _log.info("configuration reloaded")

    async def _jump(self, payload: dict) -> None:
        target = payload.get("id")
        if target is None and payload.get("path"):
            record = self.library.by_path(str(payload["path"]))
            target = record.id if record else None
        if target is None:
            return
        group = self.playlist.jump_to(int(target))
        if group:
            self.playlist._history.append(list(self.playlist.current_ids))
            self.playlist.current_ids = [r.id for r in group]
            self.loader.cancel_prefetch()
            prepared = await self.loader.load([r.as_meta() for r in group])
            if prepared is not None:
                self.current = group
                texture = Texture.from_image(prepared.image, srgb=True)
                slide = Slide(texture, duration=self.config.slideshow.interval)
                slide.meta = prepared
                self.renderer.show(slide)
                self._slide_started = time.monotonic()
                self._next_change_at = self._slide_started + self.config.slideshow.interval
                self._build_info_overlay(group)
                self._info_until = self._slide_started + self.config.viewer.text_seconds
                self._dirty = True

    async def _delete_current(self, source: str = "") -> None:
        if not self.current:
            return
        record = self.current[0]
        target_dir = os.path.expanduser(self.config.library.deleted_folder)
        os.makedirs(target_dir, exist_ok=True)
        destination = os.path.join(target_dir, os.path.basename(record.path))
        n = 1
        while os.path.exists(destination):
            stem, ext = os.path.splitext(os.path.basename(record.path))
            destination = os.path.join(target_dir, f"{stem}-{n}{ext}")
            n += 1
        try:
            shutil.move(record.path, destination)
        except OSError as exc:
            _log.error("cannot move %s aside: %s", record.path, exc)
            return
        _log.info("moved %s to %s", record.path, destination)
        # Before forget(), never after: the database row is the only place the
        # title, the date taken, the place name and the tags exist, and
        # forget() deletes it.  Journal first and a removal stays explicable
        # even if the picture is never restored.
        self.removals.record(record, os.path.basename(destination), source=source)
        self.library.forget([record.path])
        self.playlist.refresh()
        await self._advance()

    async def _restore_removed(self, stored_as: str) -> dict[str, Any]:
        """Put a removed picture back where it came from.

        The journal keeps the original full path, so this is a real undo and
        not a drop into some generic inbox: the picture reappears in the
        folder it was taken from, and the next scan indexes it there.
        """
        entry = self.removals.find(str(stored_as or ""))
        if entry is None:
            return {"ok": False, "error": f"nothing removed is called {stored_as!r}"}
        folder = os.path.expanduser(self.config.library.deleted_folder)
        source_path = os.path.join(folder, entry["stored_as"])
        if not os.path.exists(source_path):
            return {"ok": False,
                    "error": f"{entry['stored_as']} is no longer in {folder}"}
        destination = entry.get("original_path") or ""
        if not destination:
            return {"ok": False, "error": "the journal has no original path"}
        # The folder may have been tidied away in the meantime, and something
        # else may have taken the name since.  Neither is a reason to refuse;
        # both are a reason to say where the picture actually ended up.
        os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
        n = 1
        while os.path.exists(destination):
            stem, ext = os.path.splitext(entry.get("original_path") or "")
            destination = f"{stem}-restored-{n}{ext}" if n > 1 else f"{stem}-restored{ext}"
            n += 1
        try:
            shutil.move(source_path, destination)
        except OSError as exc:
            _log.error("cannot restore %s: %s", source_path, exc)
            return {"ok": False, "error": str(exc)}
        self.removals.mark_restored(entry["stored_as"], destination)
        _log.info("restored %s to %s", entry["stored_as"], destination)
        if self.scanner is not None:
            await asyncio.get_running_loop().run_in_executor(
                None, self.scanner.rescan_paths, [destination]
            )
        if self.playlist is not None:
            self.playlist.refresh()
        self._dirty = True
        return {"ok": True, "path": destination,
                "moved": destination != entry.get("original_path")}

    # ------------------------------------------------------------------
    # Display power
    # ------------------------------------------------------------------
    def set_display(self, on: bool) -> None:
        if on == self.display_on:
            return
        self.display_on = on
        handled = self.backend.set_power(on) if self.backend else False
        if not handled and self.backlight.available:
            self.backlight.set(self.config.display.brightness if on else 0.0)
            handled = True
        if not handled:
            # Last resort: black out in the shader so at least the picture goes.
            self.renderer.brightness = self.config.display.brightness if on else 0.0
        if on:
            self._next_change_at = time.monotonic() + self.config.slideshow.interval
        self._dirty = True
        _log.info("display %s", "on" if on else "off")

    def set_brightness(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        self.config.display.brightness = value
        if self.renderer is not None:
            self.renderer.brightness = value
        if self.backlight.available:
            self.backlight.set(value)
        self._dirty = True

    # ------------------------------------------------------------------
    # Background work
    # ------------------------------------------------------------------
    async def _maintenance_loop(self) -> None:
        cfg = self.config
        if cfg.library.scan_on_start:
            asyncio.create_task(self._rescan(initial=True))
        if cfg.library.watch and self.scanner is not None:
            loop = asyncio.get_running_loop()

            def on_change(result) -> None:
                loop.call_soon_threadsafe(self._library_changed)

            if not self.scanner.start_watching(on_change):
                _log.info("falling back to periodic rescans every %.0fs",
                          cfg.library.rescan_interval)

        last_scan = time.monotonic()
        last_publish = 0.0
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            now = time.monotonic()

            wanted = self.power.display_should_be_on() and self.config.power.enabled
            if wanted != self.display_on:
                self.set_display(wanted)
            scheduled = self.power.brightness(default=self.config.display.brightness)
            if self.renderer is not None and abs(self.renderer.brightness - scheduled) > 0.01:
                self.renderer.brightness = scheduled
                self._dirty = True

            if cfg.library.rescan_interval > 0 and now - last_scan > cfg.library.rescan_interval:
                last_scan = now
                asyncio.create_task(self._rescan())

            # Fill in place names a few at a time.  Nominatim allows one
            # request a second, so a quiet trickle is the only polite way to
            # geocode a library, and it costs nothing once the cache is warm.
            if (self.geocoder is not None and self.scanner is not None
                    and now - self._location_backfill_at > 30.0):
                self._location_backfill_at = now
                asyncio.create_task(self._backfill_locations())

            if now - self._fps_window >= 5.0:
                self._fps = self._frames / (now - self._fps_window)
                self._frames = 0
                self._fps_window = now
            if now - last_publish >= 5.0:
                last_publish = now
                self._publish()

    async def _backfill_locations(self) -> None:
        try:
            resolved = await self.scanner.backfill_locations_async(limit=10)
        except Exception:  # pragma: no cover - network
            return
        if resolved and self.current:
            fresh = self.library.get(self.current[0].id)
            if fresh is not None and fresh.location != self.current[0].location:
                self.current[0] = fresh
                self._build_info_overlay(self.current)
                self._dirty = True

    def _library_changed(self) -> None:
        if self.playlist is None:
            return
        size_before = self.playlist.size
        self.playlist.refresh()
        _log.info("library updated: %d -> %d pictures", size_before, self.playlist.size)
        if self._placeholder_shown and self.playlist.size:
            self._next_change_at = 0.0
        self._publish()

    async def _rescan(self, *, initial: bool = False) -> None:
        if self.scanner is None or self._scanning:
            return
        self._scanning = True
        self._publish()
        try:
            result = await self.scanner.scan_async()
            if result.added or result.removed or result.updated or initial:
                self._library_changed()
        except Exception:
            _log.exception("library scan failed")
        finally:
            self._scanning = False
            self._publish()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def state(self) -> State:
        record = self.current[0] if self.current else None
        now = time.monotonic()
        return State(
            running=not self._stop.is_set(),
            paused=self.paused,
            display_on=self.display_on,
            brightness=round(self.renderer.brightness if self.renderer else 1.0, 3),
            transition=self.config.slideshow.transition,
            interval=self.config.slideshow.interval,
            order=self.playlist.order if self.playlist else "shuffle",
            show_info=self.show_info,
            show_clock=self.config.viewer.show_clock,
            playlist_size=self.playlist.size if self.playlist else 0,
            playlist_position=self.playlist.position if self.playlist else 0,
            playlist_round=self.playlist.round if self.playlist else 1,
            playlist_remaining=self.playlist.remaining if self.playlist else 0,
            filters=self._filters_payload(),
            scanning=self._scanning,
            restart_required=sorted(self._restart_needed),
            unsaved_changes=self._config_dirty,
            can_restart=True,
            library=self.library.stats() if self.library else {},
            removed=self.removals.summary() if self.removals else {},
            current=self._current_payload(record),
            next_change_in=self._next_change_in(now),
            video=self._video_payload(),
            display={
                "backend": self.backend.info.backend if self.backend else "",
                "name": self.backend.info.name if self.backend else "",
                "width": self.backend.width if self.backend else 0,
                "height": self.backend.height if self.backend else 0,
                "refresh_hz": round(self.backend.info.refresh_hz, 2) if self.backend else 0,
            },
            health=self.health.snapshot(),
            network=self.network.snapshot(),
            version=__version__,
            uptime=round(now - self.started, 1),
            fps=round(self._fps, 2),
        )

    def _next_change_in(self, now: float) -> float:
        """Seconds until the next slide.

        While a video runs the deadline is only ever half a second ahead -- it
        is pushed on every tick -- so reporting it verbatim would show a
        countdown stuck at 0.5.  What is left of the film is the honest answer.
        """
        if self._video_playing():
            left = []
            rest_of_film = self.video.duration - self.video.position
            if rest_of_film > 0 and not self.video.loop:
                left.append(rest_of_film)    # looping starts it over instead
            if self.video.max_seconds > 0:
                left.append(max(0.0, self.video.max_seconds - self.video.elapsed))
            if left:
                return round(min(left), 1)
        return round(max(0.0, self._next_change_at - now), 1)

    def _current_payload(self, record: Record | None) -> dict[str, Any]:
        if record is None:
            return {}
        payload = record.as_dict()
        payload["paired_with"] = [r.path for r in self.current[1:]]
        payload["shown_as"] = self._current_fit
        # Pre-formatted for consumers that cannot template: Home Assistant's
        # timestamp sensors want ISO 8601 with an offset, and its attribute
        # cards want a string rather than a list.
        payload["taken_iso"] = (
            datetime.fromtimestamp(record.taken_at).astimezone().isoformat()
            if record.taken_at else None
        )
        payload["tags_text"] = ", ".join(record.tags or ())
        payload["has_position"] = (record.latitude is not None
                                   and record.longitude is not None)
        return payload

    def _video_payload(self) -> dict[str, Any]:
        if self.video is None or not self.video.playing:
            return {}
        return {
            "playing": True,
            "position": round(self.video.position, 1),
            "duration": round(self.video.duration, 1),
        }

    def _publish(self) -> None:
        self.bus.publish(self.state())

    # ------------------------------------------------------------------
    def save_config(self, path: str | None = None) -> str:
        """Write the running configuration to disk and clear the dirty flag."""
        target = self.config.save(path) if path else self.config.save()
        self._config_dirty = False
        _log.info("configuration written to %s", target or self.config.source_path)
        return target or self.config.source_path or ""

    def request_stop(self) -> None:
        self._stop.set()

    def request_restart(self) -> None:
        """Come back up with a fresh process.

        Under systemd this is simply a clean exit -- ``Restart=always`` brings
        the unit back a few seconds later, in a fresh cgroup, which is more
        thorough than anything the process could do to itself while holding
        DRM master.  Started by hand, the CLI re-executes instead.
        """
        _log.info("restart requested")
        self.restart_requested = True
        self._stop.set()

    @staticmethod
    def under_systemd() -> bool:
        return bool(os.environ.get("INVOCATION_ID") or os.environ.get("JOURNAL_STREAM"))

    def shutdown(self) -> None:
        _log.info("shutting down")
        self._stop_video()
        if self.video is not None:
            self.video.close()
        if self.loader is not None:
            self.loader.close()
        if self.scanner is not None:
            self.scanner.close()
        if self.geocoder is not None:
            self.geocoder.close()
        if self.renderer is not None:
            self.renderer.close()
        if self.backend is not None:
            self.backend.close()
        if self.library is not None:
            self.library.close()
