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

import os
from collections.abc import Sequence
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
    "sync": "Syncthing \u2014 photographs that arrive by themselves from your phone, "
            "your Mac or a NAS, without anybody copying anything.",
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
    "sync": "Syncthing",
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
    "display.mode": "`3840x2160@30`, `1920x1080`. Empty takes the mode the screen says it prefers, which is almost always right — name one when it is not: a 4K television asks for 2160p60, which a Pi 4 cannot drive without `hdmi_enable_4kp60` in `config.txt`. `picframe3 doctor` lists what this screen offers.",
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
    "viewer.text_seconds": "How long the caption stays up after each change. 0 writes nothing by itself — the caption then appears only when you ask for it.",
    "viewer.peek_seconds": "How long the caption stays up when you *ask* for it — the Home Assistant button, the `i` key — rather than the seconds it gets by itself. A deliberate look wants longer than a glance.",
    "viewer.text_justify": "Left, centred or right. A pair of portraits always centres each caption under its own picture.",
    "viewer.text_opacity": "0–1.",
    "viewer.text_margin_x": "Gap from the side of the screen to the caption.",
    "viewer.text_margin_y": "Gap above and below the caption inside its band.",
    "viewer.text_scrim": "Darkening behind the caption so it stays legible over a bright picture. 0–1.",
    "viewer.date_format": "strftime: `%-d %B %Y` is \"7 September 2026\", `%d.%m.%Y` is \"07.09.2026\".",
    "viewer.locale": "Which language month and day names come out in: `de_DE.UTF-8`, `fr_FR.UTF-8`. Empty uses the system's own, which under systemd is usually English whatever the Pi is set to. The locale has to be generated on the Pi — `doctor` says whether it is.",
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
    "library.prune_max_fraction": "How much of the index one scan may drop, as a fraction. A picture folder on a stick or a share is briefly absent now and then, and every file under it then looks deleted — this is what stops that from throwing away the play history and the place names. 0 removes the safeguard.",
    "sync.enabled": "Run Syncthing on this frame. Switching it on installs it if it is missing and starts it with the Pi; switching it off stops it and leaves the folder, the pairings and the photographs exactly where they are.",
    "sync.folder_path": "The folder Syncthing keeps in step. Empty means your first picture folder, which is almost always the right answer.",
    "sync.folder_label": "What this folder is called on your phone and your Mac.",
    "sync.folder_id": "The id the two sides agree on. Changing it afterwards means pairing the folder again, so leave it alone unless you have a reason.",
    "sync.folder_type": "**Send & receive** is two-way: photographs arrive, and what the frame does to them travels back \u2014 including a removal. **Receive only** takes photographs and never sends a change of its own, so Remove on the frame stays on the frame. **Send only** is the frame handing pictures out and taking none.",
    "sync.versioning_days": "Days Syncthing keeps its own copy of anything deleted or overwritten in that folder \u2014 the safety net under a two-way folder. 0 switches the trash can off.",
    "sync.gui_lan": "Syncthing listens on the frame itself out of the box, which on a Pi with no browser means nobody can open its page. On makes it reachable from your own network, the way the frame\u2019s own page is \u2014 and, like it, with no password in front of it.",
    "sync.gui_port": "The port Syncthing\u2019s own page is served on. 8384 unless something else on the frame already wants it.",
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
    "http.allow_delete": "Let the Remove button — and Home Assistant's *Remove current picture* — move pictures out of the library. Off refuses both, and a picture can then only be removed at the frame itself, with a button or a key. Nothing is ever unlinked either way: “remove” means moved to the deleted folder, and the Removed tab can put it back.",
    "http.allowed_hosts": "Extra names this frame answers to. It already answers to `localhost`, to its own hostname and to any address on your own network; add a name here only if you reach it through a reverse proxy or an unusual local domain. A request arriving under some other name is refused, which is what stops a web page from using your browser as a way in.",
    "http.cors_origins": "Named sites that may call this API from a browser, e.g. `https://ha.example.com`. Anything listed here can do everything this page can do — skip pictures, change settings, remove photographs — on behalf of anyone who visits it while on your network. Leave it empty unless you are embedding the API somewhere. `*` is refused: it would grant that to every site on the web.",
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
    "peek_seconds": "When asked for, stays up for (s)",
    "viewer.locale": "Language for dates",
    "display.mode": "Screen mode",
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
    "http.allow_delete": "Remove pictures from the network",
    "http.cors_origins": "Sites allowed to call this API",
    "http.allowed_hosts": "Extra names the frame answers to",
    "library.prune_max_fraction": "Most of the index one scan may drop",
    "gpio_pull_up": "GPIO pull-up",
    "gpio_buttons": "GPIO buttons",
    "journald": "Log to the journal",
    "subfolder": "Show only this subfolder",
    "sync.enabled": "Run Syncthing on the frame",
    "sync.folder_path": "Folder kept in step",
    "sync.folder_label": "Name on your other machines",
    "sync.folder_id": "Folder id",
    "sync.folder_type": "Which way photographs travel",
    "sync.versioning_days": "Keep deleted files for (days)",
    "sync.gui_lan": "Syncthing\u2019s own page on the network",
    "sync.gui_port": "Syncthing\u2019s port",
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

# --------------------------------------------------------------------------
# Settings that name a file, and where a file is allowed to be
# --------------------------------------------------------------------------
# The web interface is deliberately open on the LAN, and several settings take
# a free path.  Without a check, anyone who can reach the frame can point
# ``logging.file`` at ``~/.bashrc`` or ``~/.config/systemd/user/*.service`` and
# have the frame itself write to it -- turning "can change a setting" into "can
# run code at the next login".  So the paths are confined to the places a
# picture frame legitimately keeps things.

#: Settings whose value is a filesystem path (or a list of them).
PATH_KEYS = {
    "logging.file",
    "library.database",
    "library.deleted_folder",
    "library.picture_folders",
    "sync.folder_path",
    "viewer.overlay_image",
    "viewer.font",
    "viewer.clock_extra_file",
    "viewer.no_files_img",
    "geo.cache",
    "mqtt.tls_ca",
    "health.disk_path",
}

#: Where those paths may point.  The home directory covers the photographs, the
#: index and the logs as the frame ships them; the mount points cover a USB
#: stick or a NAS; ``/var/lib/picframe3`` is the system-wide install's own
#: state; ``/tmp`` is scratch.  ``/dev/shm`` is here because two settings (the
#: overlay image and the second clock line) default into it -- it is a tmpfs
#: that evaporates on reboot, so nothing durable can be planted there.  The two
#: font directories are read-only to the frame's own user and are where a
#: system font actually lives, which ``viewer.font`` has always been able to
#: name.
ALLOWED_PATH_ROOTS = ("/media", "/mnt", "/srv", "/var/lib/picframe3",
                      "/tmp", "/dev/shm",
                      "/usr/share/fonts", "/usr/local/share/fonts")

#: The frame's own dot-directories under the home directory.  Hidden paths are
#: otherwise refused (see below) and these three are where the index, the
#: geocache and the config actually live, so they are named rather than
#: excepted by a rule.
STATE_DIRS = (".local/share/picframe3", ".config/picframe3", ".cache/picframe3")


def allowed_path_roots() -> tuple[str, ...]:
    """The roots, with the current user's home resolved in."""
    home = os.path.realpath(os.path.expanduser("~"))
    return (home, *(os.path.join(home, d) for d in STATE_DIRS), *ALLOWED_PATH_ROOTS)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + os.sep)


def path_is_allowed(value: str, extra_roots: Sequence[str] = ()) -> bool:
    """Whether a single path may be written into a setting.

    ``realpath`` first, so neither ``..`` nor a symlink the caller planted
    earlier can walk out of an allowed root.  An empty value is allowed: that
    is how these settings are switched off.

    Being inside the home directory is not on its own enough, because the
    interesting targets are all *in* it: ``~/.bashrc``, ``~/.profile``,
    ``~/.config/systemd/user/anything.service``.  Every one of them is hidden,
    and nothing a picture frame legitimately reads or writes is -- except its
    own three state directories, which are listed above.  So a hidden
    component anywhere below a general root is refused.
    """
    text = str(value or "").strip()
    if not text:
        return True
    resolved = os.path.realpath(os.path.expanduser(text))
    home = os.path.realpath(os.path.expanduser("~"))
    for state in STATE_DIRS:
        if _within(resolved, os.path.join(home, state)):
            return True
    # Wherever the frame's own pictures already live counts as one of its
    # places.  A library at /photos or /data/bilder loads from the config file
    # perfectly well, and without this the owner could not re-save that same
    # value from the settings page: the page would refuse a path the frame is
    # already using, which reads as the page being broken.
    for root in extra_roots:
        if root and _within(resolved, os.path.realpath(os.path.expanduser(root))):
            return True
    for root in (home, *ALLOWED_PATH_ROOTS):
        if not _within(resolved, root):
            continue
        below = os.path.relpath(resolved, root)
        return not any(part.startswith(".") and part not in (".", "..")
                       for part in below.split(os.sep))
    return False


def config_roots(config: Any) -> tuple[str, ...]:
    """The places a particular configuration already points at.

    Passed to :func:`check_path_setting` so that a frame whose pictures live
    somewhere unusual can still have its own settings written back.
    """
    if config is None:
        return ()
    out: list[str] = []
    try:
        out.extend(config.library.picture_folders)
        out.append(os.path.dirname(os.path.expanduser(config.library.database)))
        out.append(config.library.deleted_folder)
    except AttributeError:                     # pragma: no cover - defensive
        return ()
    return tuple(p for p in out if p)


def check_path_setting(key: str, value: Any, config: Any = None) -> str | None:
    """``None`` when the setting may be written, else why it may not.

    One helper for every surface -- the HTTP API, and MQTT through the app --
    so a path the web interface refuses cannot be smuggled in over the broker.
    """
    if key not in PATH_KEYS:
        return None
    extra = config_roots(config)
    values = value if isinstance(value, (list, tuple)) else [value]
    for item in values:
        if isinstance(item, (dict, list, tuple)):
            return f"{key} takes a path, not {type(item).__name__}"
        if not path_is_allowed(item, extra):
            roots = ", ".join((*allowed_path_roots(), *extra))
            return (f"{key} may not point at {item!r}: the frame only reads "
                    f"and writes visible files under {roots}")
    return None


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
LIVE_SECTIONS = {"viewer", "slideshow", "geo", "power", "health", "network",
                 "sync"}
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


#: What a number may be, where the type alone cannot say: (minimum, maximum),
#: either end None for "no bound".  These are not taste, they are the range in
#: which the frame still works -- an interval of zero is a busy loop, a Ken
#: Burns zoom below 1 is a division by zero, a negative transition time runs
#: the fade backwards.  ``Config.set`` clamps to them, so a value out of range
#: arrives as a log line rather than as a crash in the render loop.
LIMITS: dict[str, tuple[float | None, float | None]] = {
    "slideshow.interval": (1.0, 86400.0),
    "slideshow.transition_time": (0.0, 30.0),
    "slideshow.kenburns_zoom": (1.0, 3.0),
    "slideshow.recent_days": (1, None),
    "slideshow.reshuffle_after": (1, None),
    "slideshow.video_max_seconds": (0.0, None),
    "display.fps_limit": (1.0, 240.0),
    "display.brightness": (0.0, 1.0),
    "display.rotate": (0, 359),
    "viewer.text_size": (6, 512),
    "viewer.text_opacity": (0.0, 1.0),
    "viewer.text_scrim": (0.0, 1.0),
    "viewer.text_seconds": (0.0, None),
    "viewer.peek_seconds": (0.0, 3600.0),
    "viewer.clock_size": (6, 512),
    "viewer.clock_opacity": (0.0, 1.0),
    "viewer.blur_amount": (0.0, None),
    "viewer.blur_zoom": (1.0, 3.0),
    "viewer.blur_dim": (0.0, 1.0),
    "viewer.upscale_limit": (1.0, 10.0),
    "library.rescan_interval": (0.0, None),
    "library.prune_max_fraction": (0.0, 1.0),
    "http.port": (1, 65535),
    "sync.gui_port": (1, 65535),
    "sync.versioning_days": (0, 3650),
    "mqtt.port": (1, 65535),
    "mqtt.publish_interval": (1.0, None),
    "mqtt.image_width": (64, 4096),
    "mqtt.image_quality": (1, 100),
    "health.interval": (5.0, None),
    "network.interval": (10.0, None),
    "network.failures": (1, None),
    "network.attempts": (1, None),
    "network.timeout": (1.0, None),
    "network.cooldown": (0.0, None),
    "network.settle": (0.0, None),
}

#: Explicit control choices, where the type alone cannot say.
CHOICES: dict[str, list[str]] = {
    "display.backend": ["auto", "kms", "headless"],
    "viewer.fit": ["auto", "cover", "contain", "blur", "mat"],
    "viewer.text_justify": ["L", "C", "R"],
    "viewer.clock_position": ["TL", "TC", "TR", "BL", "BC", "BR"],
    "logging.level": ["DEBUG", "INFO", "WARNING", "ERROR"],
    "sync.folder_type": ["sendreceive", "receiveonly", "sendonly"],
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
    "sync.folder_type": "sync-directions",
}

#: Fields nobody should meet before the ones that matter.
ADVANCED = {
    "display.device", "display.connector", "display.vsync", "display.background",
    "slideshow.shuffle", "slideshow.paused",
    "viewer.text_margin_x", "viewer.text_margin_y", "viewer.text_opacity",
    "viewer.overlay_image", "viewer.clock_extra_file", "viewer.blur_zoom",
    "library.database", "library.deleted_folder", "library.follow_links",
    "sync.folder_id", "sync.folder_label", "sync.gui_port",
    "library.prune_max_fraction",
    "geo.cache",
    "mqtt.discovery_prefix", "mqtt.topic_prefix", "mqtt.tls_ca",
    "mqtt.tls_insecure", "mqtt.publish_interval",
    "mqtt.image_width", "mqtt.image_quality",
    "http.host", "http.cors_origins", "http.allowed_hosts",
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
        "sync-directions": pairs([
            ("sendreceive", "Send & receive \u2014 two-way"),
            ("receiveonly", "Receive only \u2014 the frame never sends a change"),
            ("sendonly", "Send only \u2014 the frame hands pictures out"),
        ]),
        # Every address key Nominatim is known to return, grouped the way the
        # tiers editor offers them.
        "address-keys": [{"name": key, "label": f"{key} — {group.lower()}"}
                         for group, keys in NOMINATIM_KEYS for key in keys],
        "folders": [],
    }


# --------------------------------------------------------------------------
# The front door: jobs, not fields
# --------------------------------------------------------------------------
# Seventy-odd settings in twelve sections is the right thing to *have* and the
# wrong thing to be *shown*.  Nobody arrives at a picture frame wanting to edit
# `viewer.mat_outer_border`; they arrive wanting a wider board.  So the page
# opens on a dozen jobs, each holding the three to five settings that job
# actually needs, with a sentence saying what the frame is doing now.
#
# This is a hand-written list, and hand-written lists go stale — which is why
# nothing is hidden behind it.  Every setting stays reachable through the full
# list, the tests below hold every key here against the real dataclasses, and
# a section no job claims turns into a link of its own automatically.


def _n(value, digits: int = 1) -> str:
    """A number as a person writes it: 35, 2.5, 10.75 — never 35.0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"{number:.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def _ratio(value) -> str:
    """0.85, 1.0 — a fraction always reads as one, never as a bare integer."""
    try:
        text = f"{float(value):.2f}".rstrip("0")
    except (TypeError, ValueError):
        return str(value)
    return text + "0" if text.endswith(".") else text


def _cap(text: str) -> str:
    """First letter up, the rest left alone — `str.capitalize` eats GPIO."""
    return text[:1].upper() + text[1:]


def _list(values, limit: int = 3) -> str:
    values = [str(v) for v in (values or []) if str(v).strip()]
    if not values:
        return ""
    if len(values) <= limit:
        return ", ".join(values)
    return f"{', '.join(values[:limit])} and {len(values) - limit} more"


ORDER_SHORT = {
    "shuffle": "shuffle, every picture once",
    "random": "random, may repeat",
    "date_desc": "newest first",
    "date_asc": "oldest first",
    "name": "by file name",
    "folder": "by folder",
    "recent": "most recently taken first",
    "least_played": "least shown first",
}

FIT_SHORT = {
    "auto": "Automatic",
    "mat": "Always in a mat",
    "blur": "Always on a blurred copy",
    "contain": "Always whole, on the background",
    "cover": "Always cropped to fill",
}

JUSTIFY_SHORT = {"L": "left", "C": "centred", "R": "right"}


def _state_pacing(c) -> str:
    s = c.slideshow
    bits = [f"{_n(s.interval)} seconds each",
            f"{s.transition} over {_n(s.transition_time)} s",
            ORDER_SHORT.get(s.order, s.order)]
    return ("paused · " if s.paused else "") + " · ".join(bits)


def _state_framing(c) -> str:
    v = c.viewer
    bits = [FIT_SHORT.get(v.fit, v.fit)]
    if v.fit == "auto":
        does = [{"mat": "mats", "blur": "blurs behind", "contain": "letterboxes",
                 "cover": "crops"}.get(f, f) for f in (v.fit_choices or ["mat"])]
        bits.append(f"{' or '.join(does)} a mismatched shape")
    if v.fit in ("auto", "mat"):
        board = f"{v.mat_outer_border} px board"
        if v.mat_texture:
            board += " with paper grain"
        bits.append(board)
    return " · ".join(bits)


def _state_captions(c) -> str:
    v = c.viewer
    written = [CAPTION_SHORT.get(name, name) for name in (v.show_text or [])]
    if not written:
        return "Nothing written over the picture"
    # `text_seconds: 0` is a frame that writes nothing until it is asked to,
    # which is a different frame from one whose caption lasts 16 seconds --
    # and the card has to say which of the two this is.
    when = (f"up for {_n(v.text_seconds)} s" if v.text_seconds > 0
            else "only when asked for")
    return (f"{_list(written, 4)} — {JUSTIFY_SHORT.get(v.text_justify, v.text_justify)}, "
            f"{v.text_size} px, {when}")


def _state_library(c) -> str:
    lib = c.library
    bits = [_list(lib.picture_folders) or "no folder set"]
    if lib.subfolder:
        bits.append(f"only {lib.subfolder}")
    bits.append("videos included" if lib.include_videos else "photographs only")
    bits.append("watching for new files" if lib.watch
                else f"rescan every {_n(lib.rescan_interval / 60)} min")
    return " · ".join(bits)


#: How a folder direction reads in a sentence.
SYNC_DIRECTION_SHORT = {
    "sendreceive": "two-way",
    "receiveonly": "incoming only",
    "sendonly": "outgoing only",
}


def _state_sync(c) -> str:
    """What Syncthing is set to do — from the configuration alone.

    Deliberately says nothing about whether Syncthing is installed, running or
    paired: this sentence is built every time the settings page is drawn, and
    asking another program over the network on every draw is how a settings
    page becomes slow. The card's own panel asks, once it is open.
    """
    sync = c.sync
    if not sync.enabled:
        return "Off \u2014 photographs arrive some other way"
    import os as _os

    folders = c.library.picture_folders or []
    where = sync.folder_path or (folders[0] if folders else "~/Pictures")
    bits = [_os.path.basename(str(where).rstrip("/")) or str(where),
            SYNC_DIRECTION_SHORT.get(sync.folder_type, sync.folder_type)]
    bits.append(f"deleted files kept {sync.versioning_days} days"
                if sync.versioning_days else "no trash can")
    return "On \u00b7 " + " \u00b7 ".join(bits)


def _state_video(c) -> str:
    s = c.slideshow
    if not c.library.include_videos:
        return "No videos in the library"
    bits = ["plays to the end" if not s.video_max_seconds
            else f"cut off after {_n(s.video_max_seconds)} s"]
    bits.append("repeats until the interval is up" if s.video_loop else "never repeats")
    bits.append("silent" if s.video_mute else "with sound")
    return _cap(" · ".join(bits))


def _state_sleep(c) -> str:
    p = c.power
    if not p.enabled or (not p.schedule and not p.dim_schedule):
        return "On all day — no schedule set, no dimming"
    off = sum(len(v if isinstance(v, list) else [v]) for v in (p.schedule or {}).values())
    bits = []
    bits.append(f"{off} off period{'' if off == 1 else 's'}" if off else "never turns off")
    bits.append(f"{len(p.dim_schedule)} dimming step"
                f"{'' if len(p.dim_schedule) == 1 else 's'}"
                if p.dim_schedule else "no dimming")
    return _cap(" · ".join(bits))


def _state_screen(c) -> str:
    d = c.display
    turn = {0: "Upright", 180: "Upside down"}.get(int(d.rotate or 0), f"{d.rotate}°")
    backend = {"auto": "automatic backend", "kms": "kms backend",
               "headless": "no screen — headless"}.get(d.backend, d.backend)
    mode = d.mode.strip() if d.mode else ""
    return " · ".join(filter(None, [
        mode or None, turn, f"brightness {_ratio(d.brightness)}", backend,
    ]))


def _state_places(c) -> str:
    g = c.geo
    if not g.enabled:
        return "Off — captions name no places"
    from .media.geocode import DETAIL_LABELS
    detail = DETAIL_LABELS.get(g.detail, g.detail)
    detail = detail.split("—")[-1].strip().lower()
    language = LANGUAGE_NAMES.get((g.language or "").lower(), g.language)
    bits = ["On", language, detail]
    if g.suppress:
        bits.append(f"{_list(g.suppress, 2)} hidden")
    if not g.contact:
        bits.append("no contact address — lookups will fail")
    return " · ".join(bits)


def _state_ha(c) -> str:
    m = c.mqtt
    if not m.enabled:
        return "Off — the frame announces nothing"
    where = f"{m.host or 'no broker set'}:{m.port}"
    return f"Announced as “{m.device_name}” · {where}"


def _state_buttons(c) -> str:
    i = c.input
    pins = len(i.gpio_buttons or {})
    on = [name for name, live in (("touchscreen", i.touch), ("keyboard", i.keyboard),
                                  ("mouse", i.mouse),
                                  (f"{pins} GPIO button{'' if pins == 1 else 's'}",
                                   bool(pins))) if live]
    tail = ("any press wakes the screen" if i.wake_on_input
            else "a press does not wake the screen")
    if not on:
        return f"Nothing connected · {tail}"
    return _cap(f"{_list(on, 4)} · {tail}")


#: The caption elements, said in as few words as a summary line can spare.
CAPTION_SHORT = {
    "title": "Title", "caption": "Caption", "name": "File name",
    "date": "Date taken", "location": "Place", "folder": "Folder",
    "camera": "Camera", "exposure": "Exposure",
}

#: Enough of the languages Nominatim answers in to keep a summary readable.
LANGUAGE_NAMES = {
    "en": "English", "de": "German", "fr": "French", "it": "Italian",
    "es": "Spanish", "pt": "Portuguese", "nl": "Dutch", "pl": "Polish",
    "sv": "Swedish", "da": "Danish", "nb": "Norwegian", "fi": "Finnish",
    "cs": "Czech", "tr": "Turkish", "ru": "Russian", "ja": "Japanese",
    "zh": "Chinese",
}


#: One entry per card on the settings page, in the order they are shown.
#: ``section`` is only where the "everything else in this area" link goes.
JOBS = [
    {"name": "pacing", "kicker": "Pacing", "title": "How fast pictures change",
     "section": "slideshow", "state": _state_pacing,
     "fields": ["slideshow.interval", "slideshow.transition",
                "slideshow.transition_time", "slideshow.order"]},
    {"name": "framing", "kicker": "Framing", "title": "How a picture sits on screen",
     "section": "viewer", "state": _state_framing,
     "fields": ["viewer.fit", "viewer.fit_choices", "viewer.mat_style",
                "viewer.mat_outer_border", "viewer.mat_outer_color",
                "viewer.mat_texture"]},
    {"name": "captions", "kicker": "Captions", "title": "What is written over the picture",
     "section": "viewer", "state": _state_captions,
     "fields": ["viewer.show_text", "viewer.text_size", "viewer.text_seconds",
                "viewer.peek_seconds", "viewer.text_justify",
                "viewer.date_format", "viewer.locale"]},
    {"name": "library", "kicker": "Library", "title": "Where the photographs come from",
     "section": "library", "state": _state_library,
     "fields": ["library.picture_folders", "library.subfolder",
                "library.include_videos", "library.watch"]},
    {"name": "sync", "kicker": "Syncthing",
     "title": "Getting photographs onto the frame",
     "section": "sync", "state": _state_sync,
     # Deliberately no sync.enabled here: the card carries a panel that
     # switches Syncthing on and off with the state in front of you, and a
     # bare checkbox beside it would be a second, quieter way to do the same
     # thing. It is still in the full list, like every other setting.
     "fields": ["sync.folder_path", "sync.folder_type",
                "sync.versioning_days", "sync.gui_lan"]},
    {"name": "video", "kicker": "Video", "title": "How a video clip plays",
     "section": "slideshow", "state": _state_video,
     "fields": ["slideshow.video_max_seconds", "slideshow.video_loop",
                "slideshow.video_mute"]},
    {"name": "sleep", "kicker": "Sleep", "title": "When the screen turns off",
     "section": "power", "state": _state_sleep,
     "fields": ["power.enabled", "power.schedule", "power.dim_schedule"]},
    {"name": "screen", "kicker": "Screen", "title": "The panel itself",
     "section": "display", "state": _state_screen,
     "fields": ["display.mode", "display.backend", "display.rotate",
                "display.brightness"]},
    {"name": "places", "kicker": "Places", "title": "Naming where a photo was taken",
     "section": "geo", "state": _state_places,
     "fields": ["geo.enabled", "geo.contact", "geo.language", "geo.detail",
                "geo.suppress"]},
    {"name": "ha", "kicker": "Home Assistant", "title": "Control from elsewhere",
     "section": "mqtt", "state": _state_ha,
     "fields": ["mqtt.enabled", "mqtt.host", "mqtt.username", "mqtt.password",
                "mqtt.device_name"]},
    {"name": "buttons", "kicker": "Buttons", "title": "Touch, keys and GPIO",
     "section": "input", "state": _state_buttons,
     "fields": ["input.touch", "input.keyboard", "input.mouse",
                "input.gpio_buttons", "input.wake_on_input"]},
]


def jobs(config) -> list[dict[str, Any]]:
    """The cards on the front of the settings page, each with its own summary.

    The summary is built here rather than in the browser so that one sentence
    about, say, what ``fit: auto`` currently does cannot say one thing on the
    page and another in the docs.
    """
    out = []
    for job in JOBS:
        try:
            state = job["state"](config)
        except Exception:                      # a summary must never break the page
            state = ""
        out.append({"name": job["name"], "kicker": job["kicker"],
                    "title": job["title"], "section": job["section"],
                    "fields": list(job["fields"]), "state": state})
    return out



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
            if widget in ("transition", "separator", "geo-detail", "rotate",
                          "sync-directions"):
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
                # A setting can have both: CHOICES is what the frame will
                # *accept* (so a value typed into the config file or sent over
                # MQTT is checked), a dynamic option list is how it should be
                # *offered*. The browser draws the option list when there is
                # one, or it would show the bare internal name.
                "choices": None if widget else CHOICES.get(dotted),
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
    return {"sections": sections, "options": options, "jobs": jobs(config)}
