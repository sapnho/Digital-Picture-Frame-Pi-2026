"""Convert a picframe (pi3d) configuration into a picframe3 one.

Only the settings that still mean something are carried over; the rest are
reported so nothing disappears silently.  Options that existed purely to work
around the old display stack (``use_sdl2``, ``use_glx``, ``display_power``,
``display_hdmi``, the shader path) have no equivalent because picframe3 talks
to DRM/KMS directly and chooses the right path itself.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .config import Config

_log = logging.getLogger(__name__)

#: old dotted key -> new dotted key
DIRECT: dict[str, str] = {
    "model.pic_dir": "library.picture_folders",
    "model.follow_links": "library.follow_links",
    "model.subdirectory": "library.subfolder",
    "model.recent_n": "slideshow.recent_days",
    "model.reshuffle_num": "slideshow.reshuffle_after",
    "model.time_delay": "slideshow.interval",
    "model.fade_time": "slideshow.transition_time",
    "model.shuffle": "slideshow.shuffle",
    "model.portrait_pairs": "slideshow.portrait_pairs",
    "model.deleted_pictures": "library.deleted_folder",
    "model.load_geoloc": "geo.enabled",
    "model.geo_key": "geo.contact",
    "model.log_level": "logging.level",
    "model.log_file": "logging.file",
    "model.no_files_img": "viewer.no_files_img",

    "viewer.blur_amount": "viewer.blur_amount",
    "viewer.blur_zoom": "viewer.blur_zoom",
    "viewer.edge_alpha": "viewer.blur_dim",
    "viewer.font_file": "viewer.font",
    "viewer.show_text_tm": "viewer.text_seconds",
    "viewer.show_text_sz": "viewer.text_size",
    "viewer.text_justify": "viewer.text_justify",
    "viewer.text_opacity": "viewer.text_opacity",
    "viewer.text_x_margin": "viewer.text_margin_x",
    "viewer.text_y_margin": "viewer.text_margin_y",
    "viewer.text_bkg_hgt": "viewer.text_scrim",
    "viewer.show_text_fm": "viewer.date_format",
    "viewer.kenburns": "slideshow.kenburns",
    "viewer.background": "display.background",
    "viewer.mat_type": "viewer.mat_style",
    "viewer.outer_mat_color": "viewer.mat_outer_color",
    "viewer.inner_mat_color": "viewer.mat_inner_color",
    "viewer.outer_mat_border": "viewer.mat_outer_border",
    "viewer.inner_mat_border": "viewer.mat_inner_border",
    "viewer.outer_mat_use_texture": "viewer.mat_texture",
    "viewer.inner_mat_use_texture": "viewer.mat_inner_texture",
    "viewer.show_clock": "viewer.show_clock",
    "viewer.clock_format": "viewer.clock_format",
    "viewer.clock_text_sz": "viewer.clock_size",
    "viewer.clock_opacity": "viewer.clock_opacity",
    "viewer.geo_suppress_list": "geo.suppress",

    "mqtt.use_mqtt": "mqtt.enabled",
    "mqtt.server": "mqtt.host",
    "mqtt.port": "mqtt.port",
    "mqtt.login": "mqtt.username",
    "mqtt.password": "mqtt.password",
    "mqtt.tls": "mqtt.tls_ca",
    "mqtt.device_id": "mqtt.device_id",

    "http.use_http": "http.enabled",
    "http.port": "http.port",
    "http.username": "http.auth_user",
    "http.password": "http.auth_password",
}

#: Settings that no longer exist, and why.
RETIRED: dict[str, str] = {
    "viewer.use_sdl2": "picframe3 renders on DRM/KMS directly; no SDL involved",
    "viewer.use_glx": "no X server is used",
    "viewer.display_power": "display power goes through DRM DPMS on every Pi",
    "viewer.display_hdmi": "use display.connector (e.g. HDMI-A-1) if autodetect is wrong",
    "viewer.display_x": "the frame always uses the output's native mode",
    "viewer.display_y": "the frame always uses the output's native mode",
    "viewer.display_w": "the frame always uses the output's native mode",
    "viewer.display_h": "the frame always uses the output's native mode",
    "viewer.shader": "transitions are built in; pick one with slideshow.transition",
    "viewer.fps": "the loop is adaptive; see display.fps_limit",
    "viewer.blend_type": "use slideshow.transition (blend -> fade, burn, bump)",
    "viewer.menu_text_sz": "the on-screen menu was replaced by the web UI",
    "viewer.menu_autohide_tm": "the on-screen menu was replaced by the web UI",
    "model.image_attr": "all metadata is published; no allow-list needed",
    "model.db_file": "use library.database",
    "model.locale": "the system locale is used",
    "model.sort_cols": "use slideshow.order (shuffle, date_desc, name, folder, …)",
    "model.update_interval": "inotify replaces polling; see library.rescan_interval",
    "http.path": "the web UI ships inside the package",
}

BLEND_TO_TRANSITION = {"blend": "fade", "burn": "burn", "bump": "bump"}


def _get(data: dict, dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def migrate(old_path: str, *, new_path: str | None = None) -> tuple[Config, list[str]]:
    import yaml

    with open(os.path.expanduser(old_path), encoding="utf-8") as fh:
        old = yaml.safe_load(fh) or {}

    config = Config()
    notes: list[str] = []

    for old_key, new_key in DIRECT.items():
        value = _get(old, old_key)
        if value is None or value == "":
            continue
        try:
            if new_key == "library.picture_folders":
                value = [value] if isinstance(value, str) else list(value)
            elif new_key == "viewer.text_scrim":
                value = min(1.0, float(value) * 2)      # old value was a height fraction
            elif new_key == "viewer.mat_style" and not value:
                continue
            elif new_key == "geo.contact" and str(value).startswith("this_needs"):
                notes.append("geo.contact was still the placeholder; set your email address")
                continue
            config.set(new_key, value)
        except (KeyError, ValueError, TypeError) as exc:
            notes.append(f"could not carry over {old_key} ({exc})")

    blend = _get(old, "viewer.blend_type")
    if blend:
        config.set("slideshow.transition", BLEND_TO_TRANSITION.get(blend, "fade"))

    fit = _get(old, "viewer.fit")
    blur_edges = _get(old, "viewer.blur_edges")
    mat_images = _get(old, "viewer.mat_images")
    if mat_images not in (None, False, "false", "no", "off"):
        config.set("viewer.fit", "auto")
        try:
            tol = float(mat_images)
            config.set("viewer.mat_tolerance", tol)
        except (TypeError, ValueError):
            config.set("viewer.mat_tolerance", -1.0 if mat_images in (True, "true") else 0.01)
    elif blur_edges:
        config.set("viewer.fit", "blur")
    elif fit:
        config.set("viewer.fit", "contain")
    else:
        config.set("viewer.fit", "cover")

    show_text = _get(old, "viewer.show_text")
    if isinstance(show_text, str):
        wanted = [w for w in ("title", "caption", "name", "date", "location", "folder")
                  if w in show_text.lower()]
        if wanted:
            config.set("viewer.show_text", wanted)

    keys = _get(old, "model.key_list")
    if keys:
        config.set("geo.key_order", keys)

    for retired, reason in RETIRED.items():
        if _get(old, retired) is not None:
            notes.append(f"{retired}: dropped — {reason}")

    shortcuts = _get(old, "peripherals.buttons") or {}
    if shortcuts:
        notes.append("peripherals.buttons: rewrite as input.keymap "
                     "(actions map to evdev key names such as KEY_RIGHT)")

    if new_path:
        config.save(new_path)
    return config, notes
