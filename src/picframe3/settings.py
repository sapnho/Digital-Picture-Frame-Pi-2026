"""Applying a changed setting to the running frame.

There used to be three implementations of this and they disagreed.  ``setup()``
built the objects from the configuration; ``_apply_setting`` knew how to change
some of them afterwards; ``_reload_config`` -- the SIGHUP path, and the one
taken after the config file is edited by hand -- knew how to change a third,
much smaller set, and then cleared the "unsaved" and "needs a restart" flags as
though it had applied everything.  After a reload the frame reported that it
was fully up to date while running the old brightness, the old rotation, the
old log level, the old playlist order and the old filters.

So there is one table now, keyed by setting, and everything that applies
settings goes through it: one key at a time from MQTT, the web UI and the CLI,
or a whole file's worth after a reload.  A setting that this table does not
mention is a setting only a fresh process picks up, which is exactly what
:func:`picframe3.uischema.needs_restart` tells the settings page -- so the page
and the frame cannot drift apart either.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from . import uischema
from .config import set_time_locale
from .control.power import PowerSchedule
from .gfx import transitions
from .media import geocode as geocode_module
from .media.geocode import Geocoder

_log = logging.getLogger(__name__)

#: Geo settings that change how an address is *worded*, and therefore mean
#: every stored place name has to be rewritten.  The others -- the contact
#: address Nominatim asks for, the cache location -- change nothing on screen,
#: and rewriting the library for them was several seconds of frozen picture for
#: no visible difference.
GEO_WORDING = {"geo.detail", "geo.key_order", "geo.suppress", "geo.language"}


class ConfigApplier:
    """Make the running frame match ``frame.config``."""

    def __init__(self, frame: Any) -> None:
        self.frame = frame

    # ------------------------------------------------------------------
    def apply(self, key: str) -> None:
        """Apply one setting that has already been written to the config."""
        frame = self.frame
        section = key.split(".", 1)[0]

        handler: Callable[[], None] | None = getattr(
            self, f"_on_{section}", None)
        if handler is not None:
            handler(key)
        frame.mark_dirty()

    def apply_all(self, changed: list[str]) -> None:
        """Apply a set of settings, each through the same path as a single one."""
        for key in changed:
            try:
                self.apply(key)
            except Exception:                       # pragma: no cover - defensive
                _log.exception("could not apply %s", key)

    # ------------------------------------------------------------------
    # One method per section.  Each takes the dotted key, because a few
    # settings need more than their section to decide what to do.
    # ------------------------------------------------------------------
    def _on_viewer(self, key: str) -> None:
        frame = self.frame
        if key.endswith(".locale"):
            # Process-wide, so it has to be set rather than passed: strftime
            # reads LC_TIME from the C library, not from an argument.
            set_time_locale(frame.config.viewer.locale)
        frame.loader.options = frame._prepare_options()
        # The next picture may already be prepared with the old mat, fit or
        # headroom.  Without throwing that away, every viewer change appeared
        # one slide late -- which, to somebody trying three mat styles in a
        # row, looks like the setting not working.
        frame.loader.cancel_prefetch()
        frame._clock_minute = None
        if frame.current:
            frame._build_info_overlay(frame.current)

    def _on_slideshow(self, key: str) -> None:
        frame = self.frame
        cfg = frame.config.slideshow
        frame.renderer.transition_name = cfg.transition
        frame.renderer.transition_time = cfg.transition_time
        frame.renderer.transition_pool = transitions.resolve_pool(cfg.transition_choices)
        frame.playlist.portrait_pairs = cfg.portrait_pairs
        frame.playlist.recent_days = cfg.recent_days
        if key.endswith("order"):
            frame.playlist.set_order(cfg.order)
        if key.endswith("paused"):
            # The settings page has a Paused switch; without this it moved and
            # nothing happened, then moved back on its own at the next refresh.
            frame.paused = bool(cfg.paused)
        frame.loader.options = frame._prepare_options()
        frame.loader.cancel_prefetch()
        frame.slideshow.reschedule()

    def _on_display(self, key: str) -> None:
        frame = self.frame
        cfg = frame.config.display
        if key.endswith("brightness"):
            frame.set_brightness(cfg.brightness)
        elif key.endswith("background"):
            frame.renderer.background = tuple(cfg.background)
        elif key.endswith("rotate"):
            frame.renderer.rotate = cfg.rotate
            frame._clock_minute = None
            if frame.current:
                frame._build_info_overlay(frame.current)

    def _on_geo(self, key: str) -> None:
        frame = self.frame
        cfg = frame.config.geo
        if key.endswith("enabled"):
            # Switching geocoding on used to do nothing at all until a restart,
            # while the settings page said it took effect immediately: the
            # geocoder was only ever built in setup().
            if cfg.enabled and frame.geocoder is None:
                frame.geocoder = Geocoder(
                    os.path.expanduser(cfg.cache),
                    contact=cfg.contact,
                    enabled=True,
                    key_order=geocode_module.key_order_for(cfg.detail, cfg.key_order),
                    suppress=cfg.suppress,
                    language=cfg.language,
                )
                if frame.scanner is not None:
                    frame.scanner.geocoder = frame.geocoder
                _log.info("geocoding switched on")
            elif not cfg.enabled and frame.geocoder is not None:
                frame.geocoder.enabled = False
                _log.info("geocoding switched off")
            return
        if key in GEO_WORDING and frame.geocoder is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:            # applied outside the frame's loop
                return
            loop.create_task(frame._restyle_locations())

    def _on_logging(self, key: str) -> None:
        if key.endswith("level"):
            level = str(self.frame.config.logging.level).upper()
            logging.getLogger().setLevel(getattr(logging, level, logging.INFO))

    def _on_health(self, key: str) -> None:
        frame = self.frame
        cfg = frame.config.health
        frame.health.enabled = cfg.enabled
        frame.health.interval = max(5.0, float(cfg.interval))
        frame.health.disk_path = frame._health_disk_path()
        if frame.health.enabled:
            frame.health.snapshot()

    def _on_network(self, key: str) -> None:
        # The loop reads these attributes on every pass, so every one of them --
        # the watcher itself included -- takes effect at the next check without
        # a restart.
        frame = self.frame
        cfg = frame.config.network
        watch = frame.network
        watch.target = cfg.target
        watch.interface = cfg.interface
        watch.interval = max(10.0, float(cfg.interval))
        watch.failures = max(1, int(cfg.failures))
        watch.attempts = max(1, int(cfg.attempts))
        watch.timeout = max(1.0, float(cfg.timeout))
        watch.cooldown = max(0.0, float(cfg.cooldown))
        watch.settle = max(0.0, float(cfg.settle))
        watch.repair = cfg.repair
        watch.enabled = cfg.enabled

    def _on_power(self, key: str) -> None:
        frame = self.frame
        frame.power = PowerSchedule(frame.config.power.schedule,
                                    frame.config.power.dim_schedule)
        # A schedule that has just been given an "off" window should take
        # effect now, but one that has just been emptied must not leave the
        # screen wherever the old schedule had put it.
        frame.apply_power_schedule(force=True)

    def _on_sync(self, key: str) -> None:
        """Make Syncthing match the settings -- on a thread, always.

        Everything behind this key talks to another program: systemd, a
        package manager, Syncthing's own API.  The slowest of them is an apt
        install, which is minutes, and none of it may happen on the loop that
        is drawing the cross-fade.  So the work is handed to a thread and the
        outcome is read back from ``/api/sync`` by whoever asked -- the
        settings page polls it -- rather than being reported here.
        """
        from . import sync as sync_module

        config = self.frame.config
        switch = bool(config.sync.enabled) if key.endswith("enabled") else None
        sync_module.apply_async(config, switch=switch)

    def _on_library(self, key: str) -> None:
        frame = self.frame
        cfg = frame.config.library
        if key.endswith("subfolder"):
            frame.playlist.filters.subfolder = cfg.subfolder
            frame.playlist.refresh()
        elif key.endswith("prune_max_fraction") and frame.scanner is not None:
            frame.scanner.prune_max_fraction = cfg.prune_max_fraction


def changed_keys(before: dict, after: dict) -> list[str]:
    """Every dotted setting whose value differs between two configurations."""
    out: list[str] = []
    for section, values in after.items():
        old = before.get(section) or {}
        if not isinstance(values, dict):
            continue
        for name, value in values.items():
            if old.get(name) != value:
                out.append(f"{section}.{name}")
    return out


def restart_required(changed: list[str]) -> set[str]:
    """Of those, the ones a running process cannot pick up."""
    return {key for key in changed if uischema.needs_restart(key)}
