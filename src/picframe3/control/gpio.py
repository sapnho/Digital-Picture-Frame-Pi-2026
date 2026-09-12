"""Physical buttons wired to the Pi's GPIO header.

Configured as ``input.gpio_buttons: {next: 17, pause: 27, display_toggle: 22}``
using BCM numbering.  Uses gpiozero, which on current Raspberry Pi OS talks to
the kernel's GPIO character device through lgpio -- the sysfs interface and
RPi.GPIO no longer work on a Pi 5.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import InputConfig
from ..events import Action, Command
from ..install import install_hint

_log = logging.getLogger(__name__)


class GpioButtons:
    def __init__(self, app, config: InputConfig):
        self.app = app
        self.config = config
        try:
            from gpiozero import Button  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                f"GPIO buttons need gpiozero ({install_hint('gpio')})"
            ) from exc
        self._buttons: list[Any] = []

    async def run(self) -> None:
        from gpiozero import Button

        loop = asyncio.get_running_loop()
        for action_name, pin in (self.config.gpio_buttons or {}).items():
            try:
                action = Action(str(action_name))
            except ValueError:
                _log.warning("gpio_buttons refers to unknown action %r", action_name)
                continue
            try:
                button = Button(int(pin), pull_up=self.config.gpio_pull_up,
                                bounce_time=0.05, hold_time=1.0)
            except Exception as exc:
                _log.error("cannot claim GPIO %s for %s: %s", pin, action_name, exc)
                continue

            def handler(act: Action = action) -> None:
                self.app.bus.submit_threadsafe(loop, Command(act, source="gpio"))

            button.when_pressed = handler
            self._buttons.append(button)
            _log.info("GPIO %s -> %s", pin, action.value)
        if not self._buttons:
            return
        try:
            await asyncio.Event().wait()          # gpiozero drives its own thread
        finally:
            for button in self._buttons:
                try:
                    button.close()
                except Exception:  # pragma: no cover
                    pass
