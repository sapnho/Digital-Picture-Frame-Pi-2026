"""Display backends.

A backend owns the EGL display/context and the presentation path.  The renderer
is written against this interface only, so the same drawing code runs

* on a Raspberry Pi with no desktop at all (``kms``: DRM/KMS + GBM),
* inside a normal desktop session for development (``gbm`` on a render node),
* and completely offscreen in CI or a container (``headless``).
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DisplayInfo:
    width: int
    height: int
    refresh_hz: float
    name: str
    backend: str
    physical_mm: tuple[int, int] = (0, 0)


class Backend(abc.ABC):
    """Presentation surface for the renderer."""

    info: DisplayInfo

    @property
    def width(self) -> int:
        return self.info.width

    @property
    def height(self) -> int:
        return self.info.height

    @abc.abstractmethod
    def make_current(self) -> None:
        """Bind this backend's EGL context to the calling thread."""

    @abc.abstractmethod
    def begin_frame(self) -> None:
        """Bind the default draw target and set the viewport."""

    @abc.abstractmethod
    def end_frame(self) -> None:
        """Present the frame.  Blocks until the next vblank where applicable."""

    @abc.abstractmethod
    def close(self) -> None: ...

    # Optional capabilities -------------------------------------------------
    def set_power(self, on: bool) -> bool:
        """Turn the attached display on or off.  Returns True if handled."""
        return False

    def get_power(self) -> bool | None:
        return None

    def capture(self):
        """Read the frame currently being drawn back as an HxWx4 uint8 array.

        Call it after drawing and *before* presenting: once the buffers are
        swapped the contents of the draw buffer are undefined, so a capture
        taken afterwards is whatever the GPU happens to have left there.

        This is how a frame with no desktop can still be photographed — which
        turns "it looks wrong" into a file you can send someone.
        """
        import ctypes

        import numpy as np

        from . import gl

        w, h = self.width, self.height
        gl.glPixelStorei(gl.PACK_ALIGNMENT, 1)
        buf = (ctypes.c_ubyte * (w * h * 4))()
        gl.glReadPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, ctypes.byref(buf))
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        return arr[::-1].copy()          # GL's origin is the bottom left

    def capture_png(self, path) -> str:
        from PIL import Image

        Image.fromarray(self.capture(), "RGBA").convert("RGB").save(path)
        return str(path)

    @property
    def egl_display(self):
        return getattr(self, "_dpy", None)

    def describe(self) -> str:
        i = self.info
        return f"{i.backend}: {i.name} {i.width}x{i.height}@{i.refresh_hz:.2f}Hz"


def create_backend(
    kind: str = "auto",
    *,
    width: int | None = None,
    height: int | None = None,
    device: str | None = None,
    connector: str | None = None,
    vsync: bool = True,
    mode: str = "",
) -> Backend:
    """Instantiate a backend.

    ``auto`` prefers a real KMS scanout and falls back to offscreen rendering,
    which is what happens in a container or over SSH without ``--device``.
    """
    from . import backend_headless

    order: list[str]
    if kind == "auto":
        order = ["kms", "headless"]
    else:
        order = [kind]

    errors: list[str] = []
    for name in order:
        try:
            if name == "kms":
                from . import backend_kms

                return backend_kms.KmsBackend(
                    device=device, connector=connector, vsync=vsync,
                    width=width, height=height, mode=mode,
                )
            if name == "headless":
                return backend_headless.HeadlessBackend(
                    width=width or 1920, height=height or 1080
                )
            raise ValueError(f"unknown backend {name!r}")
        except Exception as exc:  # pragma: no cover - hardware dependent
            errors.append(f"{name}: {exc}")
            _log.info("backend %s unavailable: %s", name, exc)

    raise RuntimeError("no usable display backend (" + "; ".join(errors) + ")")
