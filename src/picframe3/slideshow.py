"""The slideshow: one place that decides what is on screen, and for how long.

This used to live in :mod:`picframe3.app`, spread across the render loop, the
command handler and a second, shorter copy of itself called ``_jump``.  That
arrangement had three specific faults, and they are the reason this module
exists:

* **Two implementations of "show this picture".**  ``_jump`` was written later
  and never caught up: it did not stop a running video, did not record that the
  picture had been played, did not prefetch the next one and did not cope with
  a file it could not read.  Jumping from the web gallery therefore left a film
  playing under a photograph.  There is now one path, :meth:`_show`, and both
  ``advance`` and ``jump`` end in it.

* **No serialisation.**  Preparing a slide takes a second or two on a Pi and
  the coroutine yields while it happens.  The render loop and the command loop
  are separate tasks, so a key press during that window ran a second advance
  concurrently: the playlist was drawn from twice (a picture silently skipped)
  and whichever load finished last won, leaving the caption describing one
  picture and the screen showing another.  Everything that changes what is on
  screen now goes through :attr:`_lock`.

* **Recursion on unreadable files.**  Skipping a file called ``advance`` again
  from inside itself, one stack frame and one full playlist refresh per file.
  A folder that had lost its permissions -- an interrupted rsync, an unplugged
  stick -- was a ``RecursionError`` and a dead frame.  It is a bounded loop now.

The controller reaches back into the frame for the pieces that can be replaced
while it runs -- the renderer, the video player, the configuration -- and owns
everything else itself.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from .gfx import Slide, Texture
from .library.db import Record
from .media import no_files_screen
from .media.prepare import TooLargeToDecode

_log = logging.getLogger(__name__)

#: How many unreadable files in a row to step over before showing the
#: placeholder instead.  Something is wrong with the library at that point --
#: a share that went away mid-scan, a folder whose permissions changed -- and
#: walking ten thousand files one failure at a time is not a recovery, it is a
#: freeze.  The frame tries again at the next slide.
MAX_SKIPPED = 20

#: How long the frame waits before looking again when there is nothing to show.
EMPTY_RETRY = 10.0


class SlideshowController:
    """What is on screen, what comes next, and when."""

    def __init__(self, frame: Any) -> None:
        self.frame = frame
        #: The records on screen right now: one picture, or two portraits shown
        #: side by side.
        self.current: list[Record] = []
        #: How that picture was laid out (cover/contain/blur/mat), for the API.
        self.current_fit = ""
        self.next_change_at = 0.0
        self.slide_started = 0.0
        self.video_started = False
        #: The message the placeholder is currently showing, so it is not
        #: rebuilt ten times a second while the library is empty.
        self.placeholder_shown: tuple[str, str] | bool = False
        #: Held for the whole of an advance, including the load.  See the
        #: module docstring.
        self._lock = asyncio.Lock()

    # -- the pieces that can change under us -------------------------------
    @property
    def config(self):
        return self.frame.config

    @property
    def renderer(self):
        return self.frame.renderer

    @property
    def playlist(self):
        return self.frame.playlist

    @property
    def loader(self):
        return self.frame.loader

    @property
    def library(self):
        return self.frame.library

    # ------------------------------------------------------------------
    # Moving on
    # ------------------------------------------------------------------
    async def advance(self, *, initial: bool = False, backwards: bool = False) -> None:
        """The next picture (or the previous one), prepared and put on screen."""
        async with self._lock:
            await self._advance_locked(initial=initial, backwards=backwards)

    async def _advance_locked(self, *, initial: bool, backwards: bool) -> None:
        assert self.playlist is not None and self.loader is not None
        self.stop_video()
        skipped: list[int] = []
        try:
            for _ in range(MAX_SKIPPED):
                group = self.playlist.previous() if backwards else self.playlist.next()
                if not group:
                    self._show_placeholder()
                    self.next_change_at = time.monotonic() + EMPTY_RETRY
                    return
                # Is it still there?  Asked here, immediately before the
                # picture goes up, and deliberately not left to the decoder
                # failing: a file that has *gone* is not the same as a file
                # that will not decode, and the two need opposite answers.
                # A missing one is forgotten, so it is out of the playlist for
                # good; an unreadable one is hidden, so it comes back on its
                # own once it is readable again.
                #
                # This also covers the prefetch, which is the narrow window the
                # old frame lost pictures in: the next photograph is decoded up
                # to a whole interval before it is shown, and a picture deleted
                # inside that window would otherwise go on the wall once more
                # from a copy that is already in memory.
                vanished = [r for r in group if not os.path.exists(r.path)]
                if vanished:
                    for record in vanished:
                        _log.info("%s is gone; forgetting it", record.path)
                        self.library.forget([record.path])
                    self.playlist.refresh()
                    continue
                try:
                    prepared = await self._prepare(group, backwards=backwards)
                except TooLargeToDecode as too_big:
                    # Deliberately not hidden and deliberately not counted as a
                    # failure: nothing is wrong with the picture, the frame was
                    # told not to spend that much memory on one.  Raising
                    # viewer.max_decode_megapixels has to be enough to bring it
                    # back, and hiding it would not be.
                    _log.warning("%s (viewer.max_decode_megapixels)", too_big)
                    continue
                if prepared is not None:
                    self._show(group, prepared, initial=initial)
                    return
                # Unreadable: hide it so the frame stops offering it, and try
                # the next one in the same direction the owner asked for.
                _log.warning("skipping unreadable file %s", group[0].path)
                skipped.append(group[0].id)
            _log.error("gave up after %d unreadable files in a row", MAX_SKIPPED)
            self._show_placeholder()
            self.next_change_at = time.monotonic() + EMPTY_RETRY
        finally:
            if skipped:
                # One refresh for the lot.  Refreshing per file meant a full
                # playlist query for every broken picture in the folder.
                for file_id in skipped:
                    self.library.set_hidden(file_id, True)
                self.playlist.refresh()

    async def _prepare(self, group: list[Record], *, backwards: bool):
        """Decode and lay out a group, using the prefetched copy when it fits."""
        metas = [r.as_meta() for r in group]
        prepared = None
        if not backwards and self.loader.prefetched_for(metas):
            prepared = await self.loader.take_prefetched()
        if prepared is None:
            self.loader.cancel_prefetch()
            prepared = await self.loader.load(metas)
        return prepared

    async def jump(self, payload: dict) -> None:
        """Show one particular picture, chosen from the gallery or the API.

        The same path as :meth:`advance` from the load onwards, which is the
        whole point: a jump used to be its own shorter implementation and quietly
        did less.
        """
        target = payload.get("id")
        if target is None and payload.get("path"):
            record = self.library.by_path(str(payload["path"]))
            target = record.id if record else None
        if target is None:
            return
        async with self._lock:
            group = self.playlist.jump_to(int(target))
            if not group:
                return
            # Same question as in _advance_locked, and for the same reason: the
            # gallery may be showing a picture that has since gone, and "gone"
            # is forgotten rather than hidden.
            vanished = [r for r in group if not os.path.exists(r.path)]
            if vanished:
                for record in vanished:
                    _log.info("%s is gone; forgetting it", record.path)
                    self.library.forget([record.path])
                self.playlist.refresh()
                return
            self.stop_video()
            try:
                prepared = await self._prepare(group, backwards=False)
            except TooLargeToDecode as too_big:
                # As in _advance_locked: a setting said no, so the picture
                # stays in the library and comes back when the setting changes.
                _log.warning("%s (viewer.max_decode_megapixels)", too_big)
                return
            if prepared is None:
                _log.warning("cannot show %s", group[0].path)
                self.library.set_hidden(group[0].id, True)
                self.playlist.refresh()
                return
            self._show(group, prepared)

    def _show(self, group: list[Record], prepared, *, initial: bool = False) -> None:
        """Put a prepared group on screen and start its clock.

        Everything that has to be true of a slide happens here and only here:
        the texture, the Ken Burns pan, the transition, the play count, the
        caption, the place name and the prefetch of whatever is next.
        """
        cfg = self.config
        self.placeholder_shown = False
        self.current = group
        self.current_fit = prepared.fit
        kenburns = cfg.slideshow.kenburns and not prepared.is_video
        texture = Texture.from_image(
            prepared.image, srgb=True, mipmap=not prepared.is_video
        )
        duration = self.slide_duration(group)
        if kenburns:
            slide = Slide.with_random_pan(
                texture, duration=duration, zoom=cfg.slideshow.kenburns_zoom
            )
        else:
            slide = Slide(texture, fit="cover", duration=duration)
        slide.meta = prepared
        self.renderer.show(
            slide,
            transition=cfg.slideshow.transition,
            duration=0.0 if initial else cfg.slideshow.transition_time,
        )
        self.slide_started = time.monotonic()
        self.next_change_at = self.slide_started + duration
        self.video_started = False
        self.frame.mark_dirty()

        for record in group:
            self.library.mark_played(record.id, self.playlist.round)
        self.frame.on_slide_shown(group)

        upcoming = self.playlist.peek()
        if upcoming:
            self.loader.prefetch([r.as_meta() for r in upcoming])

    def _show_placeholder(self) -> None:
        message, subtitle = self.frame.describe_empty()
        if self.placeholder_shown == (message, subtitle) or self.renderer is None:
            return
        image = no_files_screen(
            (self.frame.backend.width, self.frame.backend.height),
            self.config.viewer.no_files_img,
            message=message,
            subtitle=subtitle,
        )
        self.renderer.show(Slide(Texture.from_image(image, srgb=True)), transition="fade")
        self.placeholder_shown = (message, subtitle)
        self.current = []
        self.frame.mark_dirty()

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------
    def slide_duration(self, group: list[Record]) -> float:
        """How long this slide is expected to stay up.

        For a photograph this is the interval and that is the whole story.  For
        a video it is only an estimate -- the fade in, then the film -- used for
        the first deadline and for what the API reports.  What actually ends a
        video slide is the player saying it has finished, so a video runs for as
        long as it plays, however long the interval for a still picture is.
        """
        cfg = self.config.slideshow
        base = float(cfg.interval)
        record = group[0]
        if not record.is_video:
            return base
        window = self.video_window()
        if cfg.video_loop:
            length = window
        else:
            length = float(record.duration or 0.0)
            if window > 0:
                length = min(length, window) if length else window
        if length <= 0:                      # duration unknown: the EOS decides
            return base
        return length + cfg.transition_time

    def video_window(self) -> float:
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

    def reschedule(self) -> None:
        """Pull the next change in after the interval was shortened.

        Without this, turning three hours down to ten seconds does nothing for
        up to three hours -- while the settings page says the change took effect
        immediately, which is exactly the shape of "this setting is broken".
        A video is left alone: its own length ends it, not the clock.
        """
        if not self.current or self.current[0].is_video:
            return
        self.next_change_at = min(
            self.next_change_at, self.slide_started + self.slide_duration(self.current)
        )

    def next_change_in(self, now: float) -> float:
        """Seconds until the next slide.

        While a video runs the deadline is only ever half a second ahead -- it
        is pushed on every tick -- so reporting it verbatim would show a
        countdown stuck at 0.5.  What is left of the film is the honest answer.
        """
        video = self.frame.video
        if self.video_playing():
            left = []
            rest_of_film = video.duration - video.position
            if rest_of_film > 0 and not video.loop:
                left.append(rest_of_film)    # looping starts it over instead
            if video.max_seconds > 0:
                left.append(max(0.0, video.max_seconds - video.elapsed))
            if left:
                return round(min(left), 1)
        return round(max(0.0, self.next_change_at - now), 1)

    def hold_slide(self) -> None:
        """Keep the slide up: the video decides when it is over, not the clock."""
        self.next_change_at = time.monotonic() + 0.5

    def end_slide(self) -> None:
        self.next_change_at = min(self.next_change_at, time.monotonic())

    # ------------------------------------------------------------------
    # Video
    # ------------------------------------------------------------------
    def video_playing(self) -> bool:
        video = self.frame.video
        return video is not None and video.playing

    def video_pending(self) -> bool:
        """A video is on screen but has not been set going yet."""
        r = self.renderer
        if r is None or r.current is None or self.video_started:
            return False
        prepared = getattr(r.current, "meta", None)
        return bool(prepared is not None and getattr(prepared, "is_video", False))

    def tick_video(self) -> None:
        renderer = self.renderer
        if renderer is None or renderer.current is None:
            return
        prepared = getattr(renderer.current, "meta", None)
        if prepared is None or not getattr(prepared, "is_video", False):
            return
        if not self.video_started:
            # The poster frame fades in first and the film starts on a fully
            # opaque picture -- a video running underneath a crossfade both
            # looks wrong and throws away its opening second.
            self.hold_slide()
            if renderer.in_transition:
                return
            self.frame._start_video(prepared.video_path)
            return
        video = self.frame.video
        if video is None:                    # poster only: no player available
            return
        frame = video.poll()
        if frame is not None:
            renderer.current.flip_v = True
            renderer.current.texture.update(frame.data, frame.width, frame.height)
            self.frame.mark_dirty()
        if video.playing:
            # However long a still picture is given, a video gets its own
            # length: the deadline is pushed ahead for as long as it runs.
            self.hold_slide()
        else:
            self.end_slide()

    def start_video(self, path: str | None) -> None:
        if not path:
            return
        cfg = self.config.slideshow
        try:
            from .media.video import VideoPlayer, available

            if not available():
                _log.info("GStreamer missing; showing the poster frame only")
                self.video_started = True
                # Nothing will move, so the poster is a still picture and gets
                # a still picture's time.
                self.next_change_at = time.monotonic() + float(cfg.interval)
                return
            if self.frame.video is None:
                self.frame.video = VideoPlayer(
                    (self.frame.backend.width, self.frame.backend.height),
                    mute=cfg.video_mute,
                    loop=cfg.video_loop,
                    max_seconds=self.video_window(),
                )
            else:
                # The player outlives a slide, so re-read the settings: they can
                # change under it from the web UI or MQTT.
                self.frame.video.mute = cfg.video_mute
                self.frame.video.loop = cfg.video_loop
                self.frame.video.max_seconds = self.video_window()
            self.frame.video.play(path)
            self.video_started = True
        except Exception as exc:
            _log.warning("cannot play %s: %s", path, exc)
            self.video_started = True
            self.end_slide()

    def stop_video(self) -> None:
        if self.frame.video is not None:
            self.frame.video.stop()
        self.video_started = False

    def video_payload(self) -> dict[str, Any]:
        video = self.frame.video
        if video is None or not video.playing:
            return {}
        return {
            "playing": True,
            "position": round(video.position, 1),
            "duration": round(video.duration, 1),
        }
