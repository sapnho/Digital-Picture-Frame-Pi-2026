"""Asynchronous slide preparation with look-ahead.

Decoding a 45 megapixel HEIC, matting it and resampling to 4K takes well over a
second on a Pi 4.  Doing that on the render thread is what makes a frame stutter
at the moment of the transition -- the one moment anyone is looking at it.

Here the next slide is prepared in a worker thread while the current one is
still on screen, so by the time the transition starts the pixels are already
waiting.  Pillow releases the GIL for decode and resample, so threads (not
processes) are enough and cost no extra memory for the image data.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

from PIL import Image

from .metadata import PhotoMeta
from .prepare import PrepareOptions, prepare

_log = logging.getLogger(__name__)


@dataclass
class PreparedSlide:
    image: Image.Image
    metas: list[PhotoMeta]
    elapsed: float
    is_video: bool = False
    video_path: str | None = None

    @property
    def primary(self) -> PhotoMeta:
        return self.metas[0]

    @property
    def fit(self) -> str:
        """How the picture was laid out: cover, contain, blur or mat."""
        return str(self.image.info.get("picframe3_fit", "")) if self.image else ""


class SlideLoader:
    def __init__(
        self,
        screen_size: tuple[int, int],
        options: PrepareOptions,
        *,
        workers: int = 2,
        video_posters: bool = True,
    ):
        self.screen_size = screen_size
        self.options = options
        self.video_posters = video_posters
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="slide-prep"
        )
        self._pending: asyncio.Future | None = None
        self._pending_metas: list[PhotoMeta] | None = None

    # -- synchronous core --------------------------------------------------
    def _prepare_sync(self, metas: Sequence[PhotoMeta]) -> PreparedSlide | None:
        t0 = time.monotonic()
        primary = metas[0]
        if primary.is_video:
            image = None
            if self.video_posters:
                from .video import poster_frame

                image = poster_frame(primary.path, self.screen_size)
            if image is None:
                from .prepare import placeholder

                image = placeholder(self.screen_size, "Video", primary.path.rsplit("/", 1)[-1])
            return PreparedSlide(
                image=image.convert("RGB"),
                metas=list(metas),
                elapsed=time.monotonic() - t0,
                is_video=True,
                video_path=primary.path,
            )
        image = prepare(metas, self.screen_size, self.options)
        if image is None:
            return None
        return PreparedSlide(image=image, metas=list(metas), elapsed=time.monotonic() - t0)

    # -- async API ---------------------------------------------------------
    async def load(self, metas: Sequence[PhotoMeta]) -> PreparedSlide | None:
        """Prepare now, awaiting the worker."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self._prepare_sync, list(metas))

    def prefetch(self, metas: Sequence[PhotoMeta]) -> None:
        """Start preparing a slide in the background; result collected later."""
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
        loop = asyncio.get_running_loop()
        self._pending_metas = list(metas)
        self._pending = loop.run_in_executor(self._pool, self._prepare_sync, list(metas))

    def prefetched_for(self, metas: Sequence[PhotoMeta]) -> bool:
        if self._pending is None or self._pending_metas is None:
            return False
        return [m.path for m in self._pending_metas] == [m.path for m in metas]

    async def take_prefetched(self) -> PreparedSlide | None:
        if self._pending is None:
            return None
        pending, self._pending, self._pending_metas = self._pending, None, None
        try:
            return await pending
        except asyncio.CancelledError:  # pragma: no cover
            return None
        except Exception as exc:
            _log.warning("slide preparation failed: %s", exc)
            return None

    def cancel_prefetch(self) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
        self._pending = None
        self._pending_metas = None

    def resize(self, screen_size: tuple[int, int]) -> None:
        self.screen_size = screen_size
        self.cancel_prefetch()

    def close(self) -> None:
        self.cancel_prefetch()
        self._pool.shutdown(wait=False, cancel_futures=True)
