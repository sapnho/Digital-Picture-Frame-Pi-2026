"""Commands in, state out.

Every way of controlling the frame -- a key press, an MQTT message, an HTTP
request, a GPIO button -- produces the same :class:`Command`, and every
observer sees the same :class:`State`.  ``picframe`` implemented each control
surface against the viewer's internals, which is why adding one meant touching
the renderer.  Here the renderer knows nothing about MQTT.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

_log = logging.getLogger(__name__)


class Action(StrEnum):
    NEXT = "next"
    PREVIOUS = "previous"
    PAUSE = "pause"
    RESUME = "resume"
    TOGGLE_PAUSE = "toggle_pause"
    JUMP = "jump"                 # payload: {"id": int} or {"path": str}
    DELETE = "delete"             # move the current picture out of the library
    DISPLAY_ON = "display_on"
    DISPLAY_OFF = "display_off"
    DISPLAY_TOGGLE = "display_toggle"
    BRIGHTNESS = "brightness"     # payload: {"value": 0.0-1.0}
    INFO_TOGGLE = "info_toggle"
    INFO_SHOW = "info_show"
    CLOCK_TOGGLE = "clock_toggle"
    SET_CONFIG = "set_config"     # payload: {"key": "slideshow.interval", "value": ...}
    SET_FILTERS = "set_filters"
    RESCAN = "rescan"
    RELOAD = "reload"
    QUIT = "quit"


@dataclass
class Command:
    action: Action
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "internal"
    received: float = field(default_factory=time.time)

    @classmethod
    def parse(cls, raw: Any, source: str = "external") -> Command | None:
        """Accept a bare action name, or a mapping with action+payload."""
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("{"):
                import json

                try:
                    raw = json.loads(text)
                except ValueError:
                    return None
            else:
                try:
                    return cls(Action(text.lower()), source=source)
                except ValueError:
                    return None
        if isinstance(raw, dict):
            name = str(raw.get("action", "")).lower()
            try:
                action = Action(name)
            except ValueError:
                return None
            payload = {k: v for k, v in raw.items() if k != "action"}
            return cls(action, payload, source=source)
        return None


@dataclass
class State:
    """A snapshot the control surfaces publish.  JSON-serialisable throughout."""

    running: bool = False
    paused: bool = False
    display_on: bool = True
    brightness: float = 1.0
    transition: str = "fade"
    interval: float = 180.0
    order: str = "shuffle"
    show_info: bool = True
    show_clock: bool = False
    playlist_size: int = 0
    playlist_position: int = 0
    #: The shuffle round in progress, and how many pictures in it have not had
    #: their turn yet.  Together they say, at a glance, whether the frame is
    #: working through the library evenly.
    playlist_round: int = 1
    playlist_remaining: int = 0
    scanning: bool = False
    library: dict[str, Any] = field(default_factory=dict)
    current: dict[str, Any] = field(default_factory=dict)
    next_change_in: float = 0.0
    video: dict[str, Any] = field(default_factory=dict)
    display: dict[str, Any] = field(default_factory=dict)
    version: str = ""
    uptime: float = 0.0
    fps: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


Listener = Callable[[State], Any]


class Bus:
    """Async command queue plus a state fan-out."""

    def __init__(self, maxsize: int = 128):
        self._queue: asyncio.Queue[Command] = asyncio.Queue(maxsize=maxsize)
        self._listeners: list[Listener] = []
        self.state = State()

    # -- commands ----------------------------------------------------------
    def submit(self, command: Command | None) -> bool:
        """Thread-safe enough for callers on the loop; drops when saturated."""
        if command is None:
            return False
        try:
            self._queue.put_nowait(command)
            return True
        except asyncio.QueueFull:
            _log.warning("command queue full; dropping %s", command.action)
            return False

    def submit_threadsafe(self, loop: asyncio.AbstractEventLoop,
                          command: Command | None) -> None:
        """For input threads that are not running on the event loop."""
        if command is None:
            return
        loop.call_soon_threadsafe(self.submit, command)

    async def get(self, timeout: float | None = None) -> Command | None:
        if timeout is None:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None

    def drain(self) -> list[Command]:
        out: list[Command] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return out

    # -- state -------------------------------------------------------------
    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def publish(self, state: State | None = None) -> None:
        if state is not None:
            self.state = state
        for listener in list(self._listeners):
            try:
                result = listener(self.state)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:  # pragma: no cover - listener bugs
                _log.exception("state listener failed")
