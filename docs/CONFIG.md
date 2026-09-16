# Configuration reference

*Generated from the code by `tools/gen_config_docs.py` — do not edit by hand.*

The file lives at `~/.config/picframe3/config.yaml`. Every key is optional:
anything you leave out uses the default below, and an unknown key produces a
warning in the log rather than a crash. A fully commented starting point is in
[`config/picframe3.example.yaml`](../config/picframe3.example.yaml).

Every setting here also appears in the **Settings** tab of the web interface —
the page is generated from the same definitions as this file, so neither can
fall behind the code. That tab opens on a handful of cards, one per job
(*how fast pictures change*, *where the photographs come from*), each holding
the few settings that job needs; **All settings** at the foot of it is the
whole list below, grouped the same way. Settings marked *(advanced)* are
behind the “Show advanced” switch there. Anything can also be set from MQTT,
or with `picframe3 config --set slideshow.interval=90`.

## `display`

Which screen to use and how hard to drive it.

| key | type | default | |
|---|---|---|---|
| `backend` | string | `'auto'` | `auto` uses the screen if there is one, otherwise renders offscreen. *(takes effect on restart)* |
| `device` | str | None | `None` | `/dev/dri/card1`. Empty takes the first card with a connected output. *(takes effect on restart)* *(advanced)* |
| `connector` | str | None | `None` | `HDMI-A-1`, `HDMI-A-2`, `DSI-1`… Empty takes the first connected output. *(takes effect on restart)* *(advanced)* |
| `mode` | string | *(empty)* | `3840x2160@30`, `1920x1080`. Empty takes the mode the screen says it prefers, which is almost always right — name one when it is not: a 4K television asks for 2160p60, which a Pi 4 cannot drive without `hdmi_enable_4kp60` in `config.txt`. `picframe3 doctor` lists what this screen offers. *(takes effect on restart)* |
| `rotate` | integer | `0` | 0 or 180. For a quarter turn rotate in the kernel (`video=HDMI-A-1:1080x1920M@60,rotate=90`) so the Pi reports a portrait mode. |
| `vsync` | boolean | `True` | Page-flip on the vertical blank. Turning it off tears; it exists for debugging. *(takes effect on restart)* *(advanced)* |
| `fps_limit` | number | `60.0` | Only applies while something is animating; a still picture draws no frames. *(takes effect on restart)* |
| `background` | list of numbers | `[0.0, 0.0, 0.0, 1.0]` | Red, green, blue, alpha, each 0–1. Shown around a picture that does not fill the screen. *(advanced)* |
| `brightness` | number | `1.0` | 0–1, applied in the shader and to the backlight if there is one. |

## `slideshow`

Pacing and sequencing: how long each picture stays, how it changes, what comes next.

| key | type | default | |
|---|---|---|---|
| `interval` | number | `180.0` | Seconds each picture stays on screen. |
| `transition` | string | `'fade'` | Any name from `picframe3 transitions`, or `random`. |
| `transition_time` | number | `2.5` | Seconds the change from one picture to the next takes. |
| `order` | string | `'shuffle'` | `shuffle` gives every picture exactly one turn per round, in a fresh random order each round, and keeps the round in the index — so reboots, rescans and newly copied photographs cannot rob the pictures still waiting. `random` has no memory and may repeat. An order picked on the running frame is kept through a restart even if it was never saved, until this file says something different. |
| `recent_days` | integer | `7` | Pictures newer than this are shown first in each shuffle round. |
| `reshuffle_after` | integer | `1` | Complete passes over the library before the order is shuffled again. |
| `portrait_pairs` | boolean | `False` | Two upright photographs side by side on a landscape screen, each with its own caption. |
| `shuffle` | boolean | `True` | Kept only so a migrated picframe configuration still loads. It is ignored: `order` alone decides. *(advanced)* |
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
| `max_decode_megapixels` | number | `64.0` | The largest picture the frame will open. A photograph from a camera or a phone is scaled down while it is decoded and never reaches this; a huge PNG or TIFF scan cannot be, and would take the frame down with it, so past this size it is skipped and the log says so. 0 turns the limit off. *(advanced)* |
| `shrink_oversized` | boolean | `True` | What happens to a picture past that size. On, the frame opens it once in a separate process and keeps a screen-sized copy in its cache, so the photograph is shown from then on; the original is never moved, changed or deleted. Off leaves such a picture out of the slideshow. *(advanced)* |
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
| `text_seconds` | number | `16.0` | How long the caption stays up after each change. 0 writes nothing by itself — the caption then appears only when you ask for it. |
| `peek_seconds` | number | `40.0` | How long the caption stays up when you *ask* for it — the Home Assistant button, the `i` key — rather than the seconds it gets by itself. A deliberate look wants longer than a glance. |
| `text_justify` | string | `'L'` | Left, centred or right. A pair of portraits always centres each caption under its own picture. |
| `text_opacity` | number | `1.0` | 0–1. *(advanced)* |
| `text_margin_x` | integer | `64` | Gap from the side of the screen to the caption. *(advanced)* |
| `text_margin_y` | integer | `36` | Gap above and below the caption inside its band. *(advanced)* |
| `text_scrim` | number | `0.5` | Darkening behind the caption so it stays legible over a bright picture. 0–1. |
| `text_separator` | string | `'  ·  '` | Written between the caption elements. |
| `date_format` | string | `'%-d %B %Y'` | strftime: `%-d %B %Y` is "7 September 2026", `%d.%m.%Y` is "07.09.2026". |
| `locale` | string | *(empty)* | Which language month and day names come out in: `de_DE.UTF-8`, `fr_FR.UTF-8`. Empty uses the system's own, which under systemd is usually English whatever the Pi is set to. A language has to be built on the Pi before it works, and only the one chosen here is built: the installer does it, or `picframe3 setup --yes` after choosing a new one here. `doctor` says whether it is. Only the dates change — the system, the installer and this page stay in English. |
| `show_clock` | boolean | `False` | A large clock over the picture. |
| `clock_format` | string | `'%H:%M'` | strftime: `%H:%M` or `%-I:%M %p`. |
| `clock_size` | integer | `120` | Type size, in pixels at 1080p. |
| `clock_position` | string | `'TR'` | Top or bottom, left, centre or right. |
| `clock_opacity` | number | `0.9` | 0–1. |
| `clock_offset_pct` | list of numbers | `[3.0, 3.0]` | How far in from the corner, as a percentage of the screen: across, then down. |
| `clock_extra_file` | string | `'/dev/shm/picframe-clock.txt'` | If this file exists its contents are written under the time, much smaller — a weather line, a countdown, anything that writes to it. *(advanced)* |
| `overlay_image` | string | `'/dev/shm/picframe-overlay.png'` | If this PNG exists it is drawn over the picture, under the caption. A hook for anything. *(advanced)* |
| `no_files_img` | string | *(empty)* | Shown when there is nothing to show. Empty is the picture that ships with the frame; a path is your own; `none` draws a plain screen naming the folders it looked in. |

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
| `deleted_folder` | string | `'~/.local/share/picframe3/deleted'` | Where “Remove” moves a picture. Every removal is written to removals.jsonl in this folder — when it went, where it came from, and what it was. A removed picture also stays out of the slideshow if the file turns up again, under any name, from a sync or a backup; the Removed tab says when that has happened and is where you let it back in. That tab reads the journal, can put a picture back, and can empty the trash: that deletes the files and keeps the lines about them. *(takes effect on restart)* *(advanced)* |
| `subfolder` | string | *(empty)* | Show only pictures whose path contains this. Pick one of your folders, or type any part of a path. Empty shows everything. |
| `prune_max_fraction` | number | `0.2` | How much of the index one scan may drop, as a fraction. A picture folder on a stick or a share is briefly absent now and then, and every file under it then looks deleted — this is what stops that from throwing away the play history and the place names. 0 removes the safeguard. *(takes effect on restart)* *(advanced)* |

## `sync`

Syncthing — photographs that arrive by themselves from your phone, your Mac or a NAS, without anybody copying anything.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `False` | Run Syncthing on this frame. Switching it on installs it if it is missing and starts it with the Pi; switching it off stops it and leaves the folder, the pairings and the photographs exactly where they are. |
| `folder_path` | string | *(empty)* | The folder Syncthing keeps in step. Empty means your first picture folder, which is almost always the right answer. |
| `folder_label` | string | `'Picture Frame'` | What this folder is called on your phone and your Mac. *(advanced)* |
| `folder_id` | string | `'picframe3-pictures'` | The id the two sides agree on. Changing it afterwards means pairing the folder again, so leave it alone unless you have a reason. *(advanced)* |
| `folder_type` | string | `'sendreceive'` | **Send & receive** is two-way: photographs arrive, and what the frame does to them travels back — including a removal. **Receive only** takes photographs and never sends a change of its own, so Remove on the frame stays on the frame. **Send only** is the frame handing pictures out and taking none. |
| `versioning_days` | integer | `30` | Days Syncthing keeps its own copy of anything deleted or overwritten in that folder — the safety net under a two-way folder. 0 switches the trash can off. |
| `gui_lan` | boolean | `True` | Syncthing listens on the frame itself out of the box, which on a Pi with no browser means nobody can open its page. On makes it reachable from your own network, the way the frame’s own page is — and, like it, with no password in front of it. |
| `gui_port` | integer | `8384` | The port Syncthing’s own page is served on. 8384 unless something else on the frame already wants it. *(advanced)* |

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
| `publish_image` | boolean | `True` | Send the picture itself to Home Assistant, so a dashboard can show what is on the frame. The photograph, not the screen — it is there even while the display is off. |
| `image_width` | integer | `1280` | Longest edge of that picture, in pixels. 1280 looks right on a dashboard and on a phone; larger costs more on every change. *(advanced)* |
| `image_quality` | integer | `82` | JPEG quality for it, 1–100. *(advanced)* |

## `http`

The web interface and REST API.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `True` | This web interface and the REST API. *(takes effect on restart)* |
| `host` | string | `'0.0.0.0'` | `0.0.0.0` listens on every network; `127.0.0.1` only on the frame itself. *(takes effect on restart)* *(advanced)* |
| `port` | integer | `9000` | The port this page is served on. *(takes effect on restart)* |
| `auth_user` | string | *(empty)* | Set this and the password to require a login. *(takes effect on restart)* |
| `auth_password` | string | *(empty)* | Only used when a user name is set. *(takes effect on restart)* |
| `allow_delete` | boolean | `True` | Let the Remove button — and Home Assistant's *Remove current picture* — move pictures out of the library. Off refuses both, and a picture can then only be removed at the frame itself, with a button or a key. Nothing is ever unlinked either way: “remove” means moved to the deleted folder, and the Removed tab can put it back. *(takes effect on restart)* |
| `cors_origins` | list of strings | `[]` | Named sites that may call this API from a browser, e.g. `https://ha.example.com`. Anything listed here can do everything this page can do — skip pictures, change settings, remove photographs — on behalf of anyone who visits it while on your network. Leave it empty unless you are embedding the API somewhere. `*` is refused: it would grant that to every site on the web. *(takes effect on restart)* *(advanced)* |
| `allowed_hosts` | list of strings | `[]` | Extra names this frame answers to. It already answers to `localhost`, to its own hostname and to any address on your own network; add a name here only if you reach it through a reverse proxy or an unusual local domain. A request arriving under some other name is refused, which is what stops a web page from using your browser as a way in. *(takes effect on restart)* *(advanced)* |

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

## `health`

What the frame reports about the Pi it runs on — temperature, load, memory, free space and the power supply.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `True` | Measure the Pi’s temperature, load, memory and free space, and report them to Home Assistant and this page. |
| `interval` | number | `30.0` | Seconds between readings. Taken on a background thread, so it never interrupts a transition. *(advanced)* |
| `disk_path` | string | *(empty)* | Which disk the free-space reading is about. Empty means your first picture folder — the one that actually fills up. *(advanced)* |

## `network`

Watching the frame’s own link to the house, and mending it when it breaks.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `True` | Check every so often that the frame can still reach the house, and say so in Home Assistant. |
| `target` | string | *(empty)* | Empty means your router, found automatically. Never put an address on the internet here — the frame would mend a link that is not broken. |
| `interface` | string | *(empty)* | Empty means whichever interface the frame actually uses. *(advanced)* |
| `interval` | number | `60.0` | Seconds between checks. A minute is plenty — the frame is looking for an outage, not measuring latency. |
| `attempts` | integer | `3` | Ping runs per check. One lost packet is not an outage; needing all of them to fail is what makes a failed check mean something. *(advanced)* |
| `timeout` | number | `3.0` | How long to wait for a reply before that run counts as lost. *(advanced)* |
| `failures` | integer | `3` | Three checks a minute apart is several minutes of real silence, not one lost packet. |
| `repair` | boolean | `True` | Off watches and reports but never touches the connection. |
| `cooldown` | number | `1800.0` | The safety catch: however bad it looks, never mend more than once in this window. *(advanced)* |
| `settle` | number | `45.0` | After mending, wait this long before checking again — a link that has just come back needs a moment to finish coming back. *(advanced)* |

## `logging`

Where log output goes.

| key | type | default | |
|---|---|---|---|
| `level` | string | `'INFO'` | DEBUG · INFO · WARNING · ERROR |
| `file` | string | *(empty)* | Empty logs to the journal only. *(takes effect on restart)* *(advanced)* |
| `journald` | boolean | `True` | Log through systemd, so `journalctl -u picframe3@pi` works. *(takes effect on restart)* *(advanced)* |

