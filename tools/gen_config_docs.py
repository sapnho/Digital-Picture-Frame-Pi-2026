#!/usr/bin/env python3
"""Generate docs/CONFIG.md from the config dataclasses.

Keeping the reference generated means it cannot drift from the code.
"""
import pathlib
import sys
from dataclasses import fields, is_dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from picframe3.config import Config  # noqa: E402

PROSE = {
    "display": "Which screen to use and how hard to drive it.",
    "slideshow": "Pacing and sequencing: how long each picture stays, how it changes, what comes next.",
    "viewer": "How a picture is composed on screen, and what is written over it.",
    "library": "Where the photographs are and how they are indexed.",
    "geo": "Turning GPS coordinates into place names for captions.",
    "mqtt": "The broker, and the Home Assistant device it announces.",
    "http": "The web interface and REST API.",
    "input": "Keyboard, touchscreen, mouse and GPIO buttons.",
    "power": "When the screen is on, off, or dimmed.",
    "logging": "Where log output goes.",
}

NOTES = {
    "display.backend": "`auto` uses the screen if there is one, otherwise renders offscreen.",
    "display.rotate": "0 or 180. For a quarter turn rotate in the kernel (`video=HDMI-A-1:1080x1920M@60,rotate=90`) so the Pi reports a portrait mode.",
    "display.connector": "`HDMI-A-1`, `HDMI-A-2`, `DSI-1`… `null` takes the first connected output.",
    "display.fps_limit": "Only applies while something is animating; a still picture draws no frames.",
    "slideshow.transition": "Any name from `picframe3 transitions`, or `random`.",
    "slideshow.order": "shuffle · random · date_desc · date_asc · name · folder · recent · least_played. `shuffle` gives every picture exactly one turn per round in a fresh random order, and keeps the round in the index, so reboots, rescans and newly copied photographs cannot rob the pictures still waiting. `random` has no memory and may repeat.",
    "slideshow.recent_days": "Pictures newer than this are shown first in each shuffle round.",
    "slideshow.video_max_seconds": "0 plays each video to the end.",
    "slideshow.transition_choices": "Which transitions `transition: random` may draw from. Empty means the standard pool. Ticked in the Settings tab.",
    "viewer.fit": "auto · cover · contain · blur · mat",
    "viewer.fit_choices": "What `fit: auto` may do with a picture whose shape does not match the panel: any of mat, blur, contain, cover. Several means \"pick one of these\", chosen from the file path so a given photograph always looks the same. A picture that already matches the panel is shown edge to edge regardless, because that crops nothing.",
    "viewer.mat_style": "single · single_bevel · double · double_bevel · float · float_shadow · float_wrap · polaroid · random · `\"\"` for any. Several names separated by spaces means \"choose from these\". Note that `polaroid` deliberately sits the print high with a deep lower margin; if pictures look badly centred, this style is usually why.",
    "viewer.mat_tolerance": "How different the picture and screen shapes must be before `auto` mats. `-1` always mats.",
    "viewer.mat_outer_color": "`null` derives the colour from the photograph.",
    "viewer.show_text": "Any of: title, caption, name, date, location, folder, camera, exposure. The order is the order they are written, and the Settings tab edits this list.",
    "viewer.text_separator": "Written between the elements above. `\"\\n\"` puts each on its own line.",
    "viewer.clock_position": "TL · TC · TR · BL · BC · BR",
    "viewer.overlay_image": "If this PNG exists it is drawn over the picture. A hook for anything.",
    "viewer.upscale_limit": "Beyond this the frame blur-fills rather than enlarging a small picture.",
    "library.watch": "inotify: new photographs appear within seconds.",
    "library.rescan_interval": "Full walk as a backstop, in seconds. 0 disables it.",
    "library.subfolder": "Live filter; also settable from MQTT and the web UI.",
    "geo.detail": "How much of an address a caption shows: full · town_region_country · town_country · town · region_country · country · custom (uses `key_order` below).",
    "geo.suppress": "Place names never to show — your own country, say.",
    "geo.key_order": "Only used when `detail` is `custom`. One tier per line; within a tier the first key Nominatim returned wins, which is what makes one setting behave the same in France and in Germany.",
    "geo.contact": "**Required when `enabled`.** Nominatim's usage policy needs a contact address.",
    "mqtt.tls_ca": "Path to a CA certificate; use port 8883 with it.",
    "http.auth_user": "Set this and `auth_password` to require HTTP basic auth.",
    "input.gpio_buttons": "BCM pin numbers, e.g. `{next: 17, pause: 27}`.",
    "input.keymap": "evdev key names, e.g. `KEY_RIGHT`.",
    "power.schedule": "`{all: [\"22:30-07:00\"]}` — ranges may cross midnight.",
    "power.dim_schedule": "`{\"19:00-22:30\": 0.45}`",
    "power.enabled": "Named `enabled`, not `on`: YAML reads a bare `on:` key as a boolean.",
}


def type_name(annotation) -> str:
    text = str(annotation).replace("typing.", "")
    text = text.replace("<class '", "").replace("'>", "")
    optional = False
    while text.startswith("Optional[") and text.endswith("]"):
        text, optional = text[len("Optional["):-1], True
    pretty = {"str": "string", "bool": "boolean", "int": "integer",
              "float": "number", "list[str]": "list of strings",
              "list[int]": "list of integers", "list[float]": "list of numbers",
              "dict": "mapping"}.get(text, text)
    return pretty + (" or null" if optional else "")


def main() -> None:
    out = [
        "# Configuration reference",
        "",
        "*Generated from the code by `tools/gen_config_docs.py` — do not edit by hand.*",
        "",
        "The file lives at `~/.config/picframe3/config.yaml`. Every key is optional:",
        "anything you leave out uses the default below, and an unknown key produces a",
        "warning in the log rather than a crash. A fully commented starting point is in",
        "[`config/picframe3.example.yaml`](../config/picframe3.example.yaml).",
        "",
        "Any of these can also be changed while the frame is running, from the web UI,",
        "from MQTT, or with `picframe3 config --set slideshow.interval=90`.",
        "",
    ]
    cfg = Config()
    import typing

    for section in fields(Config):
        if section.name == "source_path":
            continue
        obj = getattr(cfg, section.name)
        if not is_dataclass(obj):
            continue
        out.append(f"## `{section.name}`")
        out.append("")
        out.append(PROSE.get(section.name, ""))
        out.append("")
        out.append("| key | type | default | |")
        out.append("|---|---|---|---|")
        hints = typing.get_type_hints(type(obj))
        for f in fields(obj):
            dotted = f"{section.name}.{f.name}"
            value = getattr(obj, f.name)
            default = "`" + repr(value).replace("|", "\\|") + "`" if value != "" else "*(empty)*"
            out.append(
                f"| `{f.name}` | {type_name(hints.get(f.name, f.type))} | {default} | "
                f"{NOTES.get(dotted, '')} |"
            )
        out.append("")

    target = pathlib.Path(__file__).resolve().parents[1] / "docs" / "CONFIG.md"
    target.write_text("\n".join(out) + "\n")
    print(f"wrote {target} ({len(out)} lines)")


if __name__ == "__main__":
    main()
