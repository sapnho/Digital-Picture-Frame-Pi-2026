# Configuration reference

*Generated from the code by `tools/gen_config_docs.py` — do not edit by hand.*

The file lives at `~/.config/picframe3/config.yaml`. Every key is optional:
anything you leave out uses the default below, and an unknown key produces a
warning in the log rather than a crash. A fully commented starting point is in
[`config/picframe3.example.yaml`](../config/picframe3.example.yaml).

Every setting here also appears in the **Settings** tab of the web interface —
the page is generated from the same definitions as this file, so neither can
fall behind the code. Settings marked *(advanced)* are behind the “Show
advanced” switch there. Anything can also be set from MQTT, or with
`picframe3 config --set slideshow.interval=90`.

## `display`

Which screen to use and how hard to drive it.

| key | type | default | |
|---|---|---|---|
| `backend` | string | `'auto'` | `auto` uses the screen if there is one, otherwise renders offscreen. *(takes effect on restart)* |
| `device` | str | None | `None` | `/dev/dri/card1`. Empty takes the first card with a connected output. *(takes effect on restart)* *(advanced)* |
| `connector` | str | None | `None` | `HDMI-A-1`, `HDMI-A-2`, `DSI-1`… Empty takes the first connected output. *(takes effect on restart)* *(advanced)* |
| `rotate` | integer | `0` | 0 or 180. For a quarter turn rotate in the kernel (`video=HDMI-A-1:1080x1920M@60,rotate=90`) so the Pi reports a portrait mode. |
| `vsync` | boolean | `True` | Page-flip on the vertical blank. Turning it off tears; it exists for debugging. *(takes effect on restart)* *(advanced)* |
| `fps_limit` | number | `30.0` | Only applies while something is animating; a still picture draws no frames. *(takes effect on restart)* |
| `background` | list of numbers | `[0.0, 0.0, 0.0, 1.0]` | Red, green, blue, alpha, each 0–1. Shown around a picture that does not fill the screen. *(advanced)* |
| `brightness` | number | `1.0` | 0–1, applied in the shader and to the backlight if there is one. |

## `slideshow`

Pacing and sequencing: how long each picture stays, how it changes, what comes next.

| key | type | default | |
|---|---|---|---|
| `interval` | number | `180.0` | Seconds each picture stays on screen. |
| `transition` | string | `'fade'` | Any name from `picframe3 transitions`, or `random`. |
| `transition_time` | number | `2.5` | Seconds the change from one picture to the next takes. |
| `order` | string | `'shuffle'` | `shuffle` gives every picture exactly one turn per round, in a fresh random order each round, and keeps the round in the index — so reboots, rescans and newly copied photographs cannot rob the pictures still waiting. `random` has no memory and may repeat. |
| `recent_days` | integer | `7` | Pictures newer than this are shown first in each shuffle round. |
| `reshuffle_after` | integer | `1` | Complete passes over the library before the order is shuffled again. |
| `portrait_pairs` | boolean | `False` | Two upright photographs side by side on a landscape screen, each with its own caption. |
| `shuffle` | boolean | `True` | Kept only so a migrated picframe configuration still loads. `order` is what decides. *(advanced)* |
| `paused` | boolean | `False` | Whether the frame starts paused. *(advanced)* |
| `video_loop` | boolean | `False` | Repeat a short video until the picture interval is up, instead of moving on when it ends. |
| `video_mute` | boolean | `True` | Videos are silent by default; a frame on a shelf that suddenly talks is startling. |
| `video_max_seconds` | number | `0.0` | Cut every video off after this many seconds. 0 plays each one to the end, however long it is — a video is never cut short by the picture interval. |
| `kenburns` | boolean | `False` | Slow pan and zoom across each picture. |
| `kenburns_zoom` | number | `1.12` | How far in the pan starts, e.g. 1.12 = 12% larger than the screen. |
| `transition_choices` | list of strings | `[]` | Which transitions `transition: random` may draw from. None ticked means the standard pool. |

## `viewer`

How a picture is composed on screen, and what is written over it.

| key | type | default | |
|---|---|---|---|
| `fit` | string | `'auto'` | How a picture is placed on the screen. `auto` decides by shape and is what most frames want. |
| `fit_choices` | list of strings | `['mat']` | What `fit: auto` may do with a picture whose shape does not match the panel. Several means "pick one of these", chosen from the file path so a given photograph always looks the same. A picture that already matches the panel is shown edge to edge regardless, because that crops nothing. |
| `blur_amount` | number | `20.0` | Radius of the blurred backdrop behind a letterboxed picture. |
| `blur_zoom` | number | `1.06` | How far the blurred backdrop is enlarged, so its edges are off screen. *(advanced)* |
| `blur_dim` | number | `0.55` | 0 = black edges, 1 = the blurred copy at full strength. |
| `upscale_limit` | number | `2.5` | Beyond this the frame blur-fills rather than enlarging a small picture. |
| `mat_style` | string | `'single'` | Tick several and each picture gets one of them. `polaroid` deliberately sits the print high with a deep lower margin; if pictures look badly centred, this style is usually why. |
| `mat_tolerance` | number | `0.01` | How different the picture and screen shapes must be before `auto` mats. `-1` always mats. |
| `mat_outer_color` | list[int] | None | `None` | Red, green, blue, 0–255. Empty derives the colour from the photograph. |
| `mat_inner_color` | list[int] | None | `None` | Red, green, blue, 0–255. Empty is a darker shade of the outer mat. |
| `mat_outer_border` | integer | `75` | Width of the board, in pixels at 1080p, scaled to your panel. |
| `mat_inner_border` | integer | `22` | Width of the inner mat, for the double styles. |
| `mat_texture` | boolean | `True` | Paper grain on the outer board — picframe's own board scan. |
| `mat_inner_texture` | boolean | `False` | Usually off: smooth card against the textured board. |
| `mat_auto_inner_color` | boolean | `True` | Off makes the inner mat a darker shade of the outer instead of its own colour from the photograph. |
| `mat_bevel_width` | integer | `5` | The chamfered cut of the opening, in pixels at 1080p, for the `*_bevel` styles. |
| `font` | str | None | `None` | Path to a .ttf. Empty finds DejaVu, Noto or Liberation. |
| `show_text` | list of strings | `['title', 'caption', 'date', 'location']` | What is written over the picture, in the order it is written. |
| `text_size` | integer | `34` | Caption type size, in pixels at 1080p. |
| `text_seconds` | number | `16.0` | How long the caption stays up after each change. |
| `text_justify` | string | `'L'` | Left, centred or right. A pair of portraits always centres each caption under its own picture. |
| `text_opacity` | number | `1.0` | 0–1. *(advanced)* |
| `text_margin_x` | integer | `64` | Gap from the side of the screen to the caption. *(advanced)* |
| `text_margin_y` | integer | `36` | Gap above and below the caption inside its band. *(advanced)* |
| `text_scrim` | number | `0.5` | Darkening behind the caption so it stays legible over a bright picture. 0–1. |
| `text_separator` | string | `'  ·  '` | Written between the caption elements. |
| `date_format` | string | `'%-d %B %Y'` | strftime: `%-d %B %Y` is "7 September 2026", `%d.%m.%Y` is "07.09.2026". |
| `show_clock` | boolean | `False` | A large clock over the picture. |
| `clock_format` | string | `'%H:%M'` | strftime: `%H:%M` or `%-I:%M %p`. |
| `clock_size` | integer | `120` | Type size, in pixels at 1080p. |
| `clock_position` | string | `'TR'` | Top or bottom, left, centre or right. |
| `clock_opacity` | number | `0.9` | 0–1. |
| `clock_offset_pct` | list of numbers | `[3.0, 3.0]` | How far in from the corner, as a percentage of the screen: across, then down. |
| `clock_extra_file` | string | `'/dev/shm/picframe-clock.txt'` | If this file exists its contents are written under the time, much smaller — a weather line, a countdown, anything that writes to it. *(advanced)* |
| `overlay_image` | string | `'/dev/shm/picframe-overlay.png'` | If this PNG exists it is drawn over the picture, under the caption. A hook for anything. *(advanced)* |

## `library`

Where the photographs are and how they are indexed.

| key | type | default | |
|---|---|---|---|
| `picture_folders` | list of strings | `['~/Pictures']` | One per line. `~` is your home directory. *(takes effect on restart)* |
| `database` | string | `'~/.local/share/picframe3/library.db3'` | The index. Moving it starts an empty library. *(takes effect on restart)* *(advanced)* |
| `follow_links` | boolean | `False` | Follow symbolic links while scanning. *(takes effect on restart)* *(advanced)* |
| `include_videos` | boolean | `True` | Index video files as well as photographs. *(takes effect on restart)* |
| `ignore_hidden` | boolean | `True` | Skip dot-files and dot-folders. *(takes effect on restart)* |
| `exclude` | list of strings | `['@eaDir', '.thumbnails', '#recycle']` | Folder names to skip anywhere in the tree. *(takes effect on restart)* |
| `watch` | boolean | `True` | inotify: new photographs appear within seconds. *(takes effect on restart)* |
| `rescan_interval` | number | `3600.0` | Full walk as a backstop behind inotify, in seconds. 0 disables it. *(takes effect on restart)* |
| `scan_on_start` | boolean | `True` | Index at startup. Off is faster to start but new files wait for the watch. *(takes effect on restart)* |
| `deleted_folder` | string | `'~/.local/share/picframe3/deleted'` | Where “Remove” moves a picture. Nothing is ever unlinked. *(takes effect on restart)* *(advanced)* |
| `subfolder` | string | *(empty)* | Show only pictures whose path contains this. Pick one of your folders, or type any part of a path. Empty shows everything. |

## `geo`

Turning GPS coordinates into place names for captions.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `False` | Reverse-geocode GPS coordinates into place names. |
| `contact` | string | *(empty)* | **Required when enabled.** Nominatim's usage policy needs a way to reach you. |
| `language` | string | `'en'` | Two-letter code: the language place names come back in. |
| `cache` | string | `'~/.local/share/picframe3/geocache.db3'` | Nominatim's replies, kept forever. Re-wording place names never costs a request. *(advanced)* |
| `detail` | string | `'full'` | How much of an address a caption shows. Changing it rewrites the names already in the index, from the cache. |
| `suppress` | list of strings | `[]` | Place names never to show — your own country, say. |
| `key_order` | list[list[str]] | `[['tourism', 'attraction', 'amenity', 'isolated_dwelling'], ['neighbourhood', 'suburb', 'village', 'town'], ['city', 'municipality', 'county'], ['state', 'province', 'region'], ['country']]` | Used when **How much of the address** is set to Custom. **One tier per line**, and within a line the keys you would accept for that tier, best first — the first one this particular address actually has is the one written, and the rest of the line is skipped. That is what makes a single setting behave the same in France and in Germany: a French hamlet comes back as `village`, a German one as `isolated_dwelling`, and a line reading `village, isolated_dwelling, town` catches both. |

## `mqtt`

The broker, and the Home Assistant device it announces.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `False` | Announce the frame to Home Assistant and accept commands. *(takes effect on restart)* |
| `host` | string | *(empty)* | Your broker. Home Assistant's built-in Mosquitto is usually the Home Assistant host itself. *(takes effect on restart)* |
| `port` | integer | `1883` | 1883 plain, 8883 with TLS. *(takes effect on restart)* |
| `username` | string | *(empty)* | Leave empty for an open broker. *(takes effect on restart)* |
| `password` | string | *(empty)* | Stored in the config file in plain text, so keep that file to yourself. *(takes effect on restart)* |
| `tls_ca` | string | *(empty)* | Path to a CA certificate; use port 8883 with it. *(takes effect on restart)* *(advanced)* |
| `tls_insecure` | boolean | `False` | Skip certificate verification. For a self-signed broker on your own LAN. *(takes effect on restart)* *(advanced)* |
| `device_id` | string | `'picframe'` | Identifies this frame. Change it if you have two. *(takes effect on restart)* |
| `device_name` | string | `'Picture Frame'` | What Home Assistant calls the device. *(takes effect on restart)* |
| `discovery_prefix` | string | `'homeassistant'` | `homeassistant` unless you changed it there. *(takes effect on restart)* *(advanced)* |
| `topic_prefix` | string | `'picframe'` | The frame publishes under `<prefix>/<device_id>/…`. *(takes effect on restart)* *(advanced)* |
| `publish_interval` | number | `30.0` | Heartbeat, in seconds. State is also published the moment anything changes. *(takes effect on restart)* *(advanced)* |

## `http`

The web interface and REST API.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `True` | This web interface and the REST API. *(takes effect on restart)* |
| `host` | string | `'0.0.0.0'` | `0.0.0.0` listens on every network; `127.0.0.1` only on the frame itself. *(takes effect on restart)* *(advanced)* |
| `port` | integer | `9000` | The port this page is served on. *(takes effect on restart)* |
| `auth_user` | string | *(empty)* | Set this and the password to require a login. *(takes effect on restart)* |
| `auth_password` | string | *(empty)* | Only used when a user name is set. *(takes effect on restart)* |
| `allow_delete` | boolean | `False` | Let the Remove button move pictures out of the library. *(takes effect on restart)* |
| `cors_origins` | list of strings | `[]` | Only needed if another site embeds this API. *(takes effect on restart)* *(advanced)* |

## `input`

Keyboard, touchscreen, mouse and GPIO buttons.

| key | type | default | |
|---|---|---|---|
| `keyboard` | boolean | `True` | A keyboard plugged into the Pi, read straight from evdev. *(takes effect on restart)* |
| `touch` | boolean | `True` | A touchscreen: tap right half for next, left for previous. *(takes effect on restart)* |
| `mouse` | boolean | `False` | Off by default; a stray mouse should not skip pictures. *(takes effect on restart)* |
| `gpio_buttons` | mapping | `{}` | BCM pin numbers, e.g. `{"next": 17, "pause": 27}`. *(takes effect on restart)* |
| `gpio_pull_up` | boolean | `True` | Buttons wired to ground, which is the usual way. *(takes effect on restart)* *(advanced)* |
| `wake_on_input` | boolean | `True` | Any button press turns the screen back on. *(takes effect on restart)* |
| `keymap` | mapping | `{'next': ['KEY_RIGHT', 'KEY_SPACE', 'KEY_DOWN'], 'previous': ['KEY_LEFT', 'KEY_UP'], 'pause': ['KEY_P'], 'display_toggle': ['KEY_O'], 'info_toggle': ['KEY_I'], 'clock_toggle': ['KEY_C'], 'delete': ['KEY_DELETE'], 'quit': ['KEY_ESC', 'KEY_Q']}` | evdev key names per action, e.g. `KEY_RIGHT`. *(takes effect on restart)* *(advanced)* |

## `power`

When the screen is on, off, or dimmed.

| key | type | default | |
|---|---|---|---|
| `schedule` | mapping | `{}` | `{"all": ["22:30-07:00"]}` — ranges may cross midnight, and weekday names work in place of `all`. |
| `dim_schedule` | mapping | `{}` | `{"19:00-22:30": 0.45}` — brightness, not on/off. |
| `enabled` | boolean | `True` | Named `enabled`, not `on`: YAML reads a bare `on:` key as a boolean. |

## `logging`

Where log output goes.

| key | type | default | |
|---|---|---|---|
| `level` | string | `'INFO'` | DEBUG · INFO · WARNING · ERROR |
| `file` | string | *(empty)* | Empty logs to the journal only. *(takes effect on restart)* *(advanced)* |
| `journald` | boolean | `True` | Log through systemd, so `journalctl -u picframe3@pi` works. *(takes effect on restart)* *(advanced)* |

