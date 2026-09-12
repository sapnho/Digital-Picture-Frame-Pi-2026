"""One description of every setting, for the settings page and the docs.

The web UI used to carry a hand-written list of the twenty settings someone had
got round to exposing.  That is the wrong shape for an appliance: the settings
that are missing are exactly the ones nobody thought about, and they drift out
of step with the code the moment a field is renamed.

So the page is generated from the dataclasses instead.  Everything in
``Config`` appears, with a sensible control chosen from its type, and this
module carries only what the type cannot say: what to call it, what it means,
which values are legal, whether it takes effect immediately, and whether it is
a secret.  ``tools/gen_config_docs.py`` reads the same two tables, so the
reference and the settings page can never disagree.
"""

from __future__ import annotations

from dataclasses import MISSING, fields, is_dataclass
from typing import Any, get_args, get_origin

# --------------------------------------------------------------------------
# What each section is for
# --------------------------------------------------------------------------

SECTION_PROSE = {
    "display": "Which screen to use and how hard to drive it.",
    "slideshow": "Pacing and sequencing: how long each picture stays, how it "
                 "changes, what comes next.",
    "viewer": "How a picture is composed on screen, and what is written over it.",
    "library": "Where the photographs are and how they are indexed.",
    "geo": "Turning GPS coordinates into place names for captions.",
    "mqtt": "The broker, and the Home Assistant device it announces.",
    "http": "The web interface and REST API.",
    "input": "Keyboard, touchscreen, mouse and GPIO buttons.",
    "power": "When the screen is on, off, or dimmed.",
    "health": "What the frame reports about the Pi it runs on \u2014 temperature, load, memory, free space and the power supply.",
    "network": "Watching the frame\u2019s own link to the house, and mending it when it breaks.",
    "logging": "Where log output goes.",
}

SECTION_LABELS = {
    "display": "Screen",
    "slideshow": "Slideshow",
    "viewer": "Picture and captions",
    "library": "Library",
    "geo": "Place names",
    "mqtt": "MQTT and Home Assistant",
    "http": "Web interface",
    "input": "Buttons, keyboard and touch",
    "power": "Screen on and off",
    "health": "Health and temperature",
    "network": "Network watchdog",
    "logging": "Logging",
}

# --------------------------------------------------------------------------
# What the type cannot say
# --------------------------------------------------------------------------

NOTES = {
    "display.backend": "`auto` uses the screen if there is one, otherwise renders offscreen.",
    "display.rotate": "0 or 180. For a quarter turn rotate in the kernel (`video=HDMI-A-1:1080x1920M@60,rotate=90`) so the Pi reports a portrait mode.",
    "display.device": "`/dev/dri/card1`. Empty takes the first card with a connected output.",
    "display.connector": "`HDMI-A-1`, `HDMI-A-2`, `DSI-1`… Empty takes the first connected output.",
    "display.vsync": "Page-flip on the vertical blank. Turning it off tears; it exists for debugging.",
    "display.fps_limit": "Only applies while something is animating; a still picture draws no frames.",
    "display.background": "Red, green, blue, alpha, each 0–1. Shown around a picture that does not fill the screen.",
    "display.brightness": "0–1, applied in the shader and to the backlight if there is one.",
    "slideshow.interval": "Seconds each picture stays on screen.",
    "slideshow.transition": "Any name from `picframe3 transitions`, or `random`.",
    "slideshow.transition_time": "Seconds the change from one picture to the next takes.",
    "slideshow.transition_choices": "Which transitions `transition: random` may draw from. None ticked means the standard pool.",
    "slideshow.order": "`shuffle` gives every picture exactly one turn per round, in a fresh random order each round, and keeps the round in the index — so reboots, rescans and newly copied photographs cannot rob the pictures still waiting. `random` has no memory and may repeat.",
    "slideshow.recent_days": "Pictures newer than this are shown first in each shuffle round.",
    "slideshow.reshuffle_after": "Complete passes over the library before the order is shuffled again.",
    "slideshow.portrait_pairs": "Two upright photographs side by side on a landscape screen, each with its own caption.",
    "slideshow.shuffle": "Kept only so a migrated picframe configuration still loads. `order` is what decides.",
    "slideshow.paused": "Whether the frame starts paused.",
    "slideshow.video_loop": "Repeat a short video until the picture interval is up, instead of moving on when it ends.",
    "slideshow.video_mute": "Videos are silent by default; a frame on a shelf that suddenly talks is startling.",
    "slideshow.video_max_seconds": "Cut every video off after this many seconds. 0 plays each one to the end, however long it is \u2014 a video is never cut short by the picture interval.",
    "slideshow.kenburns": "Slow pan and zoom across each picture.",
    "slideshow.kenburns_zoom": "How far in the pan starts, e.g. 1.12 = 12% larger than the screen.",
    "viewer.fit": "How a picture is placed on the screen. `auto` decides by shape and is what most frames want.",
    "viewer.fit_choices": "What `fit: auto` may do with a picture whose shape does not match the panel. Several means \"pick one of these\", chosen from the file path so a given photograph always looks the same. A picture that already matches the panel is shown edge to edge regardless, because that crops nothing.",
    "viewer.blur_amount": "Radius of the blurred backdrop behind a letterboxed picture.",
    "viewer.blur_zoom": "How far the blurred backdrop is enlarged, so its edges are off screen.",
    "viewer.blur_dim": "0 = black edges, 1 = the blurred copy at full strength.",
    "viewer.upscale_limit": "Beyond this the frame blur-fills rather than enlarging a small picture.",
    "viewer.mat_style": "Tick several and each picture gets one of them. `polaroid` deliberately sits the print high with a deep lower margin; if pictures look badly centred, this style is usually why.",
    "viewer.mat_tolerance": "How different the picture and screen shapes must be before `auto` mats. `-1` always mats.",
    "viewer.mat_outer_color": "Red, green, blue, 0–255. Empty derives the colour from the photograph.",
    "viewer.mat_inner_color": "Red, green, blue, 0–255. Empty is a darker shade of the outer mat.",
    "viewer.mat_outer_border": "Width of the board, in pixels at 1080p, scaled to your panel.",
    "viewer.mat_inner_border": "Width of the inner mat, for the double styles.",
    "viewer.mat_texture": "Paper grain on the outer board — picframe's own board scan.",
    "viewer.mat_inner_texture": "Usually off: smooth card against the textured board.",
    "viewer.mat_auto_inner_color": "Off makes the inner mat a darker shade of the outer instead of its own colour from the photograph.",
    "viewer.mat_bevel_width": "The chamfered cut of the opening, in pixels at 1080p, for the `*_bevel` styles.",
    "viewer.font": "Path to a .ttf. Empty finds DejaVu, Noto or Liberation.",
    "viewer.show_text": "What is written over the picture, in the order it is written.",
    "viewer.text_separator": "Written between the caption elements.",
    "viewer.text_size": "Caption type size, in pixels at 1080p.",
    "viewer.text_seconds": "How long the caption stays up after each change.",
    "viewer.text_justify": "Left, centred or right. A pair of portraits always centres each caption under its own picture.",
    "viewer.text_opacity": "0–1.",
    "viewer.text_margin_x": "Gap from the side of the screen to the caption.",
    "viewer.text_margin_y": "Gap above and below the caption inside its band.",
    "viewer.text_scrim": "Darkening behind the caption so it stays legible over a bright picture. 0–1.",
    "viewer.date_format": "strftime: `%-d %B %Y` is \"7 September 2026\", `%d.%m.%Y` is \"07.09.2026\".",
    "viewer.show_clock": "A large clock over the picture.",
    "viewer.clock_format": "strftime: `%H:%M` or `%-I:%M %p`.",
    "viewer.clock_size": "Type size, in pixels at 1080p.",
    "viewer.clock_position": "Top or bottom, left, centre or right.",
    "viewer.clock_opacity": "0–1.",
    "viewer.clock_offset_pct": "How far in from the corner, as a percentage of the screen: across, then down.",
    "viewer.clock_extra_file": "If this file exists its contents are written under the time, much smaller — a weather line, a countdown, anything that writes to it.",
    "viewer.overlay_image": "If this PNG exists it is drawn over the picture, under the caption. A hook for anything.",
    "viewer.no_files_img": "Shown when there is nothing to show. Empty is the picture that ships with the frame; a path is your own; `none` draws a plain screen naming the folders it looked in.",
    "library.picture_folders": "One per line. `~` is your home directory.",
    "library.database": "The index. Moving it starts an empty library.",
    "library.follow_links": "Follow symbolic links while scanning.",
    "library.include_videos": "Index video files as well as photographs.",
    "library.ignore_hidden": "Skip dot-files and dot-folders.",
    "library.exclude": "Folder names to skip anywhere in the tree.",
    "library.watch": "inotify: new photographs appear within seconds.",
    "library.rescan_interval": "Full walk as a backstop behind inotify, in seconds. 0 disables it.",
    "library.scan_on_start": "Index at startup. Off is faster to start but new files wait for the watch.",
    "library.deleted_folder": "Where “Remove” moves a picture. Nothing is ever unlinked, and every removal is written to removals.jsonl in this folder — when it went, where it came from, and what it was. The Removed tab reads that file and can put a picture back.",
    "library.subfolder": "Show only pictures whose path contains this. Pick one of your folders, or type any part of a path. Empty shows everything.",
    "geo.enabled": "Reverse-geocode GPS coordinates into place names.",
    "geo.contact": "**Required when enabled.** Nominatim's usage policy needs a way to reach you.",
    "geo.language": "Two-letter code: the language place names come back in.",
    "geo.cache": "Nominatim's replies, kept forever. Re-wording place names never costs a request.",
    "geo.detail": "How much of an address a caption shows. Changing it rewrites the names already in the index, from the cache.",
    "geo.suppress": "Place names never to show — your own country, say.",
    "geo.key_order": "Used when **How much of the address** is set to Custom. **One tier per line**, and within a line the keys you would accept for that tier, best first — the first one this particular address actually has is the one written, and the rest of the line is skipped. That is what makes a single setting behave the same in France and in Germany: a French hamlet comes back as `village`, a German one as `isolated_dwelling`, and a line reading `village, isolated_dwelling, town` catches both.",
    "mqtt.enabled": "Announce the frame to Home Assistant and accept commands.",
    "mqtt.host": "Your broker. Home Assistant's built-in Mosquitto is usually the Home Assistant host itself.",
    "mqtt.port": "1883 plain, 8883 with TLS.",
    "mqtt.username": "Leave empty for an open broker.",
    "mqtt.password": "Stored in the config file in plain text, so keep that file to yourself.",
    "mqtt.tls_ca": "Path to a CA certificate; use port 8883 with it.",
    "mqtt.tls_insecure": "Skip certificate verification. For a self-signed broker on your own LAN.",
    "mqtt.device_id": "Identifies this frame. Change it if you have two.",
    "mqtt.device_name": "What Home Assistant calls the device.",
    "mqtt.discovery_prefix": "`homeassistant` unless you changed it there.",
    "mqtt.topic_prefix": "The frame publishes under `<prefix>/<device_id>/…`.",
    "mqtt.publish_interval": "Heartbeat, in seconds. State is also published the moment anything changes.",
    "mqtt.publish_image": "Send the picture itself to Home Assistant, so a dashboard can show what is on the frame. The photograph, not the screen \u2014 it is there even while the display is off.",
    "mqtt.image_width": "Longest edge of that picture, in pixels. 1280 looks right on a dashboard and on a phone; larger costs more on every change.",
    "mqtt.image_quality": "JPEG quality for it, 1\u2013100.",
    "http.enabled": "This web interface and the REST API.",
    "http.host": "`0.0.0.0` listens on every network; `127.0.0.1` only on the frame itself.",
    "http.port": "The port this page is served on.",
    "http.auth_user": "Set this and the password to require a login.",
    "http.auth_password": "Only used when a user name is set.",
    "http.allow_delete": "Let the Remove button move pictures out of the library.",
    "http.cors_origins": "Only needed if another site embeds this API.",
    "input.keyboard": "A keyboard plugged into the Pi, read straight from evdev.",
    "input.touch": "A touchscreen: tap right half for next, left for previous.",
    "input.mouse": "Off by default; a stray mouse should not skip pictures.",
    "input.gpio_buttons": "BCM pin numbers, e.g. `{\"next\": 17, \"pause\": 27}`.",
    "input.gpio_pull_up": "Buttons wired to ground, which is the usual way.",
    "input.wake_on_input": "Any button press turns the screen back on.",
    "input.keymap": "evdev key names per action, e.g. `KEY_RIGHT`.",
    "power.enabled": "Named `enabled`, not `on`: YAML reads a bare `on:` key as a boolean.",
    "power.schedule": "`{\"all\": [\"22:30-07:00\"]}` — ranges may cross midnight, and weekday names work in place of `all`.",
    "power.dim_schedule": "`{\"19:00-22:30\": 0.45}` — brightness, not on/off.",
    "network.enabled": "Check every so often that the frame can still reach the house, and say so in Home Assistant.",
    "network.target": "Empty means your router, found automatically. Never put an address on the internet here — the frame would mend a link that is not broken.",
    "network.interface": "Empty means whichever interface the frame actually uses.",
    "network.failures": "Three checks a minute apart is several minutes of real silence, not one lost packet.",
    "network.repair": "Off watches and reports but never touches the connection.",
    "network.interval": "Seconds between checks. A minute is plenty \u2014 the frame is looking for an outage, not measuring latency.",
    "network.attempts": "Ping runs per check. One lost packet is not an outage; needing all of them to fail is what makes a failed check mean something.",
    "network.timeout": "How long to wait for a reply before that run counts as lost.",
    "network.settle": "After mending, wait this long before checking again \u2014 a link that has just come back needs a moment to finish coming back.",
    "network.cooldown": "The safety catch: however bad it looks, never mend more than once in this window.",
    "health.enabled": "Measure the Pi’s temperature, load, memory and free space, and report them to Home Assistant and this page.",
    "health.interval": "Seconds between readings. Taken on a background thread, so it never interrupts a transition.",
    "health.disk_path": "Which disk the free-space reading is about. Empty means your first picture folder — the one that actually fills up.",
    "logging.level": "DEBUG · INFO · WARNING · ERROR",
    "logging.file": "Empty logs to the journal only.",
    "logging.journald": "Log through systemd, so `journalctl -u picframe3@pi` works.",
}

#: Labels where turning the field name into words is not good enough.
LABELS = {
    "fps_limit": "Frame rate limit",
    "kenburns": "Ken Burns pan and zoom",
    "kenburns_zoom": "Ken Burns starting zoom",
    "video_max_seconds": "Stop a video after (s)",
    "transition_time": "Transition length (s)",
    "interval": "Seconds per picture",
    "recent_days": "Treat photographs as new for (days)",
    "reshuffle_after": "Reshuffle after (rounds)",
    "text_seconds": "Caption stays up for (s)",
    "transition_choices": "Transitions “random” may use",
    "fit_choices": "What “auto” may do with a mismatched picture",
    "mat_style": "Mat styles",
    "show_text": "Caption elements",
    "text_separator": "Between caption elements",
    "clock_offset_pct": "Clock offset from the corner (%)",
    "clock_extra_file": "Second clock line, read from",
    "key_order": "Custom address tiers",
    "tls_ca": "TLS certificate authority",
    "tls_insecure": "Skip TLS verification",
    "mqtt.device_id": "Device ID",
    "http.auth_user": "User name",
    "http.auth_password": "Password",
    "gpio_pull_up": "GPIO pull-up",
    "gpio_buttons": "GPIO buttons",
    "journald": "Log to the journal",
    "subfolder": "Show only this subfolder",
    "detail": "How much of the address",
    "suppress": "Never show these names",
    "dim_schedule": "Dimming schedule",
    "network.enabled": "Watch the connection",
    "network.target": "Ping this address",
    "network.interface": "Interface to reconnect",
    "network.interval": "Seconds between checks",
    "network.attempts": "Ping runs per check",
    "network.timeout": "Seconds to wait for a reply",
    "network.failures": "Failed checks before repairing",
    "network.repair": "Repair the connection",
    "network.cooldown": "Seconds between repairs",
    "network.settle": "Seconds to settle after a repair",
    "health.interval": "Seconds between readings",
    "health.disk_path": "Free space reported for",
    "health.enabled": "Report the Pi\u2019s health",
    "mqtt.publish_image": "Send the current picture",
    "mqtt.image_width": "Picture width sent (px)",
    "mqtt.image_quality": "Picture quality sent",
}

#: Free-text fields whose value is a secret and must never be echoed back.
#: The web interface is usually open on a home network, so ``GET /api/config``
#: returning the MQTT password in clear would be a real leak.
SECRETS = {"mqtt.password", "http.auth_password"}

#: What a secret reads as over the API.  Writing it back changes nothing, so a
#: read-modify-write of the whole configuration cannot blank a password.
REDACTED = "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"


def redact(data: dict) -> dict:
    """A copy of ``config.as_dict()`` with the secrets masked."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in data.items()}
    for dotted in SECRETS:
        section, _, name = dotted.partition(".")
        values = out.get(section)
        if isinstance(values, dict) and name in values:
            values[name] = REDACTED if values[name] else ""
    return out

#: Settings the running frame picks up at once.  Everything else is written to
#: the config file and takes effect when the frame restarts, and the page says
#: so rather than leaving the owner to wonder why nothing happened.
LIVE_SECTIONS = {"viewer", "slideshow", "geo", "power", "health", "network"}
LIVE_KEYS = {
    "display.brightness", "display.rotate", "display.background",
    "library.subfolder", "logging.level",
    # Read when the picture is published rather than when the bridge starts,
    # so changing any of them takes effect on the next picture.
    "mqtt.publish_image", "mqtt.image_width", "mqtt.image_quality",
}

def needs_restart(key: str) -> bool:
    """True when only a fresh process will pick this setting up.

    One answer, used by the running frame to decide what to put in
    ``restart_required``, by the settings page to badge the control, and by the
    reference to mark the row -- so the three can never disagree about which
    settings a restart is for.
    """
    section = key.split(".", 1)[0]
    return not (section in LIVE_SECTIONS or key in LIVE_KEYS)


#: Explicit control choices, where the type alone cannot say.
CHOICES: dict[str, list[str]] = {
    "display.backend": ["auto", "kms", "headless"],
    "viewer.fit": ["auto", "cover", "contain", "blur", "mat"],
    "viewer.text_justify": ["L", "C", "R"],
    "viewer.clock_position": ["TL", "TC", "TR", "BL", "BC", "BR"],
    "logging.level": ["DEBUG", "INFO", "WARNING", "ERROR"],
}

#: Controls that need a list of options built at runtime.
DYNAMIC: dict[str, str] = {
    "slideshow.transition": "transition",
    "slideshow.transition_choices": "transitions",
    "slideshow.order": "order",
    "viewer.fit_choices": "fits",
    "viewer.mat_style": "mat-styles",
    "viewer.show_text": "caption-fields",
    "viewer.text_separator": "separator",
    "geo.detail": "geo-detail",
    "geo.key_order": "address-keys",
    "library.subfolder": "folders",
    "display.rotate": "rotate",
}

#: Fields nobody should meet before the ones that matter.
ADVANCED = {
    "display.device", "display.connector", "display.vsync", "display.background",
    "slideshow.shuffle", "slideshow.paused",
    "viewer.text_margin_x", "viewer.text_margin_y", "viewer.text_opacity",
    "viewer.overlay_image", "viewer.clock_extra_file", "viewer.blur_zoom",
    "library.database", "library.deleted_folder", "library.follow_links",
    "geo.cache",
    "mqtt.discovery_prefix", "mqtt.topic_prefix", "mqtt.tls_ca",
    "mqtt.tls_insecure", "mqtt.publish_interval",
    "mqtt.image_width", "mqtt.image_quality",
    "http.host", "http.cors_origins",
    "input.gpio_pull_up", "input.keymap",
    "logging.file", "logging.journald",
    "health.interval", "health.disk_path",
    "network.interface", "network.attempts", "network.timeout",
    "network.settle", "network.cooldown",
}


# --------------------------------------------------------------------------
# Building the schema
# --------------------------------------------------------------------------

def _label(section: str, name: str) -> str:
    for key in (f"{section}.{name}", name):
        if key in LABELS:
            return LABELS[key]
    words = name.replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def _type_of(annotation: Any) -> tuple[str, bool]:
    """(kind, nullable) from a resolved type hint."""
    nullable = False
    args = get_args(annotation)
    if get_origin(annotation) is not None and type(None) in args:
        nullable = True
        rest = [a for a in args if a is not type(None)]
        if len(rest) == 1:
            annotation = rest[0]
            args = get_args(annotation)

    origin = get_origin(annotation)
    if origin is dict or annotation is dict:
        return "json", nullable
    if origin in (list, tuple) or annotation is list:
        inner = args[0] if args else str
        if get_origin(inner) in (list, tuple):
            return "json", nullable          # list of lists: geo.key_order
        if inner in (int, float):
            return "numbers", nullable
        return "csv", nullable
    if annotation is bool:
        return "bool", nullable
    if annotation is int:
        return "int", nullable
    if annotation is float:
        return "number", nullable
    return "text", nullable


def _options() -> dict[str, list[dict[str, str]]]:
    """The option lists the dynamic controls need, built once."""
    from .gfx import transitions
    from .gfx.overlays import CAPTION_FIELDS
    from .library.playlist import ORDER_MODES
    from .media.geocode import DETAIL_LABELS, NOMINATIM_KEYS
    from .media.mat import STYLE_LABELS, STYLES
    from .media.prepare import AUTO_FITS

    def pairs(items):
        return [{"name": n, "label": label} for n, label in items]

    order_labels = {
        "shuffle": "Shuffle — every picture once per round",
        "random": "Random — no memory, may repeat",
        "date_desc": "Newest first",
        "date_asc": "Oldest first",
        "name": "By file name",
        "folder": "By folder",
        "recent": "Most recently taken first",
        "least_played": "Least shown first",
    }
    fit_labels = {
        "mat": "In a mat (passepartout)",
        "blur": "Whole, on a blurred copy of itself",
        "contain": "Whole, on the background colour",
        "cover": "Cropped to fill the screen",
    }
    return {
        "transition": pairs([("random", "random"), *((n, n) for n in transitions.names())]),
        "transitions": pairs((n, n) for n in transitions.names()),
        "order": pairs((n, order_labels.get(n, n)) for n in ORDER_MODES),
        "fits": pairs((n, fit_labels[n]) for n in AUTO_FITS),
        "mat-styles": pairs((n, STYLE_LABELS.get(n, n)) for n in STYLES),
        "caption-fields": pairs(CAPTION_FIELDS),
        "geo-detail": pairs(DETAIL_LABELS.items()),
        "separator": pairs([("  ·  ", "Dot  ·"), (" – ", "Dash  –"),
                            (", ", "Comma  ,"), ("\n", "One per line")]),
        "rotate": pairs([("0", "Upright"), ("180", "Upside down")]),
        # Every address key Nominatim is known to return, grouped the way the
        # tiers editor offers them.
        "address-keys": [{"name": key, "label": f"{key} — {group.lower()}"}
                         for group, keys in NOMINATIM_KEYS for key in keys],
        "folders": [],
    }


def schema(config, extra_options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Every setting, with enough about each to draw a control for it."""
    import typing

    sections = []
    for section in fields(type(config)):
        obj = getattr(config, section.name)
        if not is_dataclass(obj):
            continue
        hints = typing.get_type_hints(type(obj))
        entries = []
        for f in fields(obj):
            dotted = f"{section.name}.{f.name}"
            kind, nullable = _type_of(hints.get(f.name, f.type))
            if dotted in CHOICES:
                kind = "select"
            widget = DYNAMIC.get(dotted)
            if widget in ("transition", "separator", "geo-detail", "rotate"):
                kind = "select"
            elif widget in ("transitions", "fits", "caption-fields"):
                kind = "pick"
            elif widget == "mat-styles":
                kind = "pick-string"
            elif widget == "order":
                kind = "select"
            elif widget == "address-keys":
                kind = "tiers"
            elif widget == "folders":
                kind = "datalist"
            if dotted in SECRETS:
                kind = "secret"

            default = f.default
            if default is MISSING and f.default_factory is not MISSING:  # type: ignore[misc]
                default = f.default_factory()                            # type: ignore[misc]
            live = not needs_restart(dotted)
            entries.append({
                "key": dotted,
                "name": f.name,
                "label": _label(section.name, f.name),
                "kind": kind,
                "nullable": nullable,
                "value": None if dotted in SECRETS else getattr(obj, f.name),
                "is_set": bool(getattr(obj, f.name)) if dotted in SECRETS else None,
                "default": None if dotted in SECRETS else default,
                "note": NOTES.get(dotted, ""),
                "choices": CHOICES.get(dotted),
                "options": DYNAMIC.get(dotted),
                "ordered": dotted == "viewer.show_text",
                "live": live,
                "advanced": dotted in ADVANCED,
            })
        sections.append({
            "name": section.name,
            "label": SECTION_LABELS.get(section.name, section.name.title()),
            "prose": SECTION_PROSE.get(section.name, ""),
            "fields": entries,
        })
    options = _options()
    options.update(extra_options or {})
    return {"sections": sections, "options": options}
