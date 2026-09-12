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
    RESTORE = "restore"           # payload: {"stored_as": str} -- undo a removal
    RELEASE = "release"           # payload: {"stored_as": str} -- show it again
    DISPLAY_ON = "display_on"
    DISPLAY_OFF = "display_off"
    DISPLAY_TOGGLE = "display_toggle"
    BRIGHTNESS = "brightness"     # payload: {"value": 0.0-1.0}
    INFO_TOGGLE = "info_toggle"
    INFO_SHOW = "info_show"       # payload: {"seconds": float} -- reveal for a while
    INFO_HIDE = "info_hide"
    CLOCK_TOGGLE = "clock_toggle"
    SET_CONFIG = "set_config"     # payload: {"key": "slideshow.interval", "value": ...}
    SET_FILTERS = "set_filters"
    RESCAN = "rescan"
    RELOAD = "reload"
    RESTART = "restart"           # stop cleanly and come back up
    SHUTDOWN = "shutdown"         # power the Pi off, so the plug can be pulled
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
    #: Which caption elements are written, in order.  Part of the state
    #: document because a control surface has to be able to *show* what it is
    #: offering to change -- a caption box in Home Assistant that does not
    #: know what the caption currently says is worse than no box.
    caption_fields: list[str] = field(default_factory=list)
    playlist_size: int = 0
    playlist_position: int = 0
    #: The shuffle round in progress, and how many pictures in it have not had
    #: their turn yet.  Together they say, at a glance, whether the frame is
    #: working through the library evenly.
    playlist_round: int = 1
    playlist_remaining: int = 0
    #: Which pictures are in the running: folder, tags, place, dates.  Part of
    #: the state document rather than a separate topic because every surface
    #: has to be able to *show* the filter it is offering to change -- a text
    #: box in Home Assistant that forgets what it is filtering on is worse
    #: than no box at all.
    filters: dict[str, Any] = field(default_factory=dict)
    scanning: bool = False
    #: Settings changed since startup that the running frame cannot pick up.
    #: The settings page turns this into "restart to apply", so nobody is left
    #: wondering why a new MQTT broker changed nothing.
    restart_required: list[str] = field(default_factory=list)
    #: Settings changed but not yet written to the config file.  A restart
    #: would lose them, so the page says so before offering the button.
    unsaved_changes: bool = False
    #: Whether the frame can restart itself at all.
    can_restart: bool = True
    #: Whether the frame can power the Pi off.  False where there is no
    #: systemd to ask, so the web interface can leave out a button that
    #: could only ever report that it did nothing.
    can_shutdown: bool = True
    library: dict[str, Any] = field(default_factory=dict)
    #: The removal journal in miniature -- how many pictures have been
    #: taken out of the library and what the last one was.  Home Assistant
    #: and the web UI both read it from here rather than the file.
    removed: dict[str, Any] = field(default_factory=dict)
    current: dict[str, Any] = field(default_factory=dict)
    next_change_in: float = 0.0
    video: dict[str, Any] = field(default_factory=dict)
    display: dict[str, Any] = field(default_factory=dict)
    #: What the Pi itself is doing -- temperature, load, memory, free space and
    #: the power supply.  Empty when the reporting is switched off.
    health: dict[str, Any] = field(default_factory=dict)
    network: dict[str, Any] = field(default_factory=dict)
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
