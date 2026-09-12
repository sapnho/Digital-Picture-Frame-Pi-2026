"""Keyboard, touchscreen and mouse input, read straight from evdev.

With no X server and no Wayland compositor there is no toolkit to deliver
events, so the frame reads ``/dev/input/event*`` itself.  That turns out to be
an advantage: a touchscreen works identically whether or not a desktop is
installed, and hot-plugging a keyboard is noticed without restarting.

Requires membership of the ``input`` group (the installer adds it).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from ..config import InputConfig
from ..events import Action, Command
from ..install import install_hint

_log = logging.getLogger(__name__)

#: A horizontal movement of at least this fraction of the screen is a swipe.
SWIPE_FRACTION = 0.12
SWIPE_MAX_SECONDS = 0.8
TAP_MAX_SECONDS = 0.4
TAP_MAX_MOVE = 0.04


@dataclass
class Gesture:
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    started: float = 0.0
    active: bool = False


class InputWatcher:
    def __init__(self, app, config: InputConfig):
        self.app = app
        self.config = config
        try:
            import evdev  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                f"input handling needs python-evdev ({install_hint('input')})"
            ) from exc
        self._key_actions = self._build_keymap()
        self._devices: dict[str, Any] = {}

    def _build_keymap(self) -> dict[str, Action]:
        mapping: dict[str, Action] = {}
        for action_name, keys in (self.config.keymap or {}).items():
            try:
                action = Action(action_name)
            except ValueError:
                _log.warning("keymap refers to unknown action %r", action_name)
                continue
            for key in keys if isinstance(keys, (list, tuple)) else [keys]:
                mapping[str(key).upper()] = action
        return mapping

    # ------------------------------------------------------------------
    def _classify(self, device) -> str | None:
        from evdev import ecodes

        caps = device.capabilities()
        keys = set(caps.get(ecodes.EV_KEY, []))
        abs_axes = set(caps.get(ecodes.EV_ABS, []))
        if isinstance(next(iter(abs_axes), None), tuple):
            abs_axes = {a[0] for a in caps.get(ecodes.EV_ABS, [])}
        if ecodes.ABS_MT_POSITION_X in abs_axes or ecodes.BTN_TOUCH in keys:
            return "touch"
        if ecodes.KEY_A in keys and ecodes.KEY_Z in keys:
            return "keyboard"
        if ecodes.EV_REL in caps and ecodes.BTN_LEFT in keys:
            return "mouse"
        return None

    def _wanted(self, kind: str) -> bool:
        return {
            "keyboard": self.config.keyboard,
            "touch": self.config.touch,
            "mouse": self.config.mouse,
        }.get(kind, False)

    def _discover(self) -> list:
        import evdev

        found = []
        for path in evdev.list_devices():
            try:
                device = evdev.InputDevice(path)
            except OSError:
                continue
            kind = self._classify(device)
            if kind and self._wanted(kind):
                found.append((kind, device))
                _log.info("listening to %s (%s) as %s", device.path, device.name, kind)
            else:
                device.close()
        if not found:
            _log.info("no matching input devices found "
                      "(is this user in the 'input' group?)")
        return found

    # ------------------------------------------------------------------
    async def run(self) -> None:
        while True:
            devices = self._discover()
            if not devices:
                await asyncio.sleep(30)      # a keyboard may be plugged in later
                continue
            tasks = [asyncio.create_task(self._pump(kind, dev)) for kind, dev in devices]
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                for _, dev in devices:
                    dev.close()
                raise
            except Exception as exc:
                _log.info("input device disappeared (%s); rediscovering", exc)
            for task in tasks:
                task.cancel()
            for _, dev in devices:
                try:
                    dev.close()
                except Exception:
                    pass
            await asyncio.sleep(2)

    async def _pump(self, kind: str, device) -> None:
        from evdev import ecodes

        gesture = Gesture()
        ranges = self._abs_ranges(device)

        async for event in device.async_read_loop():
            if event.type == ecodes.EV_KEY:
                await self._on_key(event, kind, gesture)
            elif event.type == ecodes.EV_ABS and kind == "touch":
                self._on_abs(event, gesture, ranges)
            elif event.type == ecodes.EV_REL and kind == "mouse":
                self._wake()

    @staticmethod
    def _abs_ranges(device) -> dict[int, tuple[int, int]]:
        from evdev import ecodes

        out: dict[int, tuple[int, int]] = {}
        try:
            for code, info in device.capabilities().get(ecodes.EV_ABS, []):
                out[code] = (info.min, max(info.max, info.min + 1))
        except (TypeError, ValueError):
            pass
        return out

    def _normalise(self, code: int, value: int, ranges: dict) -> float:
        low, high = ranges.get(code, (0, 4096))
        span = max(1, high - low)
        return min(1.0, max(0.0, (value - low) / span))

    def _on_abs(self, event, gesture: Gesture, ranges: dict) -> None:
        from evdev import ecodes

        if event.code in (ecodes.ABS_X, ecodes.ABS_MT_POSITION_X):
            gesture.x1 = self._normalise(event.code, event.value, ranges)
        elif event.code in (ecodes.ABS_Y, ecodes.ABS_MT_POSITION_Y):
            gesture.y1 = self._normalise(event.code, event.value, ranges)
        elif event.code == ecodes.ABS_MT_TRACKING_ID:
            if event.value == -1:
                self._finish_gesture(gesture)
            else:
                gesture.active = True
                gesture.started = time.monotonic()
                gesture.x0, gesture.y0 = gesture.x1, gesture.y1

    async def _on_key(self, event, kind: str, gesture: Gesture) -> None:
        from evdev import ecodes

        if event.value != 1:                      # key/button down only
            return
        if event.code in (ecodes.BTN_TOUCH, ecodes.BTN_LEFT):
            if kind == "touch":
                gesture.active = True
                gesture.started = time.monotonic()
                gesture.x0, gesture.y0 = gesture.x1, gesture.y1
                return
            self._submit(Action.NEXT)
            return
        if event.code == ecodes.BTN_RIGHT:
            self._submit(Action.PREVIOUS)
            return
        name = ecodes.KEY.get(event.code)
        if isinstance(name, (list, tuple)):
            name = name[0]
        if not name:
            return
        action = self._key_actions.get(str(name).upper())
        if action is None:
            return
        if self._wake():
            return
        self.app.bus.submit(Command(action, source="input"))

    def _finish_gesture(self, gesture: Gesture) -> None:
        if not gesture.active:
            return
        gesture.active = False
        dt = time.monotonic() - gesture.started
        dx = gesture.x1 - gesture.x0
        dy = gesture.y1 - gesture.y0
        if self._wake():
            return
        if dt < SWIPE_MAX_SECONDS and abs(dx) > SWIPE_FRACTION and abs(dx) > abs(dy):
            self._submit(Action.NEXT if dx < 0 else Action.PREVIOUS)
        elif dt < SWIPE_MAX_SECONDS and abs(dy) > SWIPE_FRACTION and abs(dy) > abs(dx):
            self._submit(Action.DISPLAY_OFF if dy > 0 else Action.INFO_SHOW)
        elif dt < TAP_MAX_SECONDS and abs(dx) < TAP_MAX_MOVE and abs(dy) < TAP_MAX_MOVE:
            self._submit(Action.INFO_SHOW if gesture.y1 > 0.25 else Action.TOGGLE_PAUSE)

    def _wake(self) -> bool:
        """Any input while the screen is off just turns it back on."""
        if self.config.wake_on_input and not self.app.display_on:
            self._submit(Action.DISPLAY_ON)
            return True
        return False

    def _submit(self, action: Action) -> None:
        self.app.bus.submit(Command(action, source="input"))


from typing import Any  # noqa: E402  (kept last to avoid a circular hint above)
