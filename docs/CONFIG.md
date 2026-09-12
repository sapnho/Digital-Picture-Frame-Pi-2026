# Configuration reference

*Generated from the code by `tools/gen_config_docs.py` — do not edit by hand.*

The file lives at `~/.config/picframe3/config.yaml`. Every key is optional:
anything you leave out uses the default below, and an unknown key produces a
warning in the log rather than a crash. A fully commented starting point is in
[`config/picframe3.example.yaml`](../config/picframe3.example.yaml).

Any of these can also be changed while the frame is running, from the web UI,
from MQTT, or with `picframe3 config --set slideshow.interval=90`.

## `display`

Which screen to use and how hard to drive it.

| key | type | default | |
|---|---|---|---|
| `backend` | string | `'auto'` | `auto` uses the screen if there is one, otherwise renders offscreen. |
| `device` | str | None | `None` |  |
| `connector` | str | None | `None` | `HDMI-A-1`, `HDMI-A-2`, `DSI-1`… `null` takes the first connected output. |
| `rotate` | integer | `0` | 0 or 180. For a quarter turn rotate in the kernel (`video=HDMI-A-1:1080x1920M@60,rotate=90`) so the Pi reports a portrait mode. |
| `vsync` | boolean | `True` |  |
| `fps_limit` | number | `30.0` | Only applies while something is animating; a still picture draws no frames. |
| `background` | list of numbers | `[0.0, 0.0, 0.0, 1.0]` |  |
| `brightness` | number | `1.0` |  |

## `slideshow`

Pacing and sequencing: how long each picture stays, how it changes, what comes next.

| key | type | default | |
|---|---|---|---|
| `interval` | number | `180.0` |  |
| `transition` | string | `'fade'` | Any name from `picframe3 transitions`, or `random`. |
| `transition_time` | number | `2.5` |  |
| `order` | string | `'shuffle'` | shuffle · random · date_desc · date_asc · name · folder · recent · least_played. `shuffle` gives every picture exactly one turn per round, in a fresh random order each round, and keeps the round in the index so reboots, rescans and newly copied photographs do not rob the pictures still waiting. `random` has no memory and can repeat. |
| `recent_days` | integer | `7` | Pictures newer than this are shown first in each shuffle round. |
| `reshuffle_after` | integer | `1` |  |
| `portrait_pairs` | boolean | `False` |  |
| `shuffle` | boolean | `True` |  |
| `paused` | boolean | `False` |  |
| `video_loop` | boolean | `False` |  |
| `video_mute` | boolean | `True` |  |
| `video_max_seconds` | number | `0.0` | 0 plays each video to the end. |
| `kenburns` | boolean | `False` |  |
| `kenburns_zoom` | number | `1.12` |  |

## `viewer`

How a picture is composed on screen, and what is written over it.

| key | type | default | |
|---|---|---|---|
| `fit` | string | `'auto'` | auto · cover · contain · blur · mat |
| `blur_amount` | number | `20.0` |  |
| `blur_zoom` | number | `1.06` |  |
| `blur_dim` | number | `0.55` |  |
| `upscale_limit` | number | `2.5` | Beyond this the frame blur-fills rather than enlarging a small picture. |
| `mat_style` | string | `'single'` | single · double · bevel · float · float_shadow · polaroid · random · `""` for any |
| `mat_tolerance` | number | `0.01` | How different the picture and screen shapes must be before `auto` mats. `-1` always mats. |
| `mat_outer_color` | list[int] | None | `None` | `null` derives the colour from the photograph. |
| `mat_inner_color` | list[int] | None | `None` |  |
| `mat_outer_border` | integer | `75` |  |
| `mat_inner_border` | integer | `22` |  |
| `mat_texture` | boolean | `True` |  |
| `mat_inner_texture` | boolean | `False` |  |
| `mat_auto_inner_color` | boolean | `True` |  |
| `mat_bevel_width` | integer | `5` |  |
| `font` | str | None | `None` |  |
| `show_text` | list of strings | `['title', 'caption', 'date', 'location']` | Any of: title, caption, name, date, location, folder, camera, exposure. The order is the order they are written; the Settings tab edits this list. |
| `text_separator` | string | `'  ·  '` | Written between the elements above. `"\n"` puts each on its own line. |
| `text_size` | integer | `34` |  |
| `text_seconds` | number | `16.0` |  |
| `text_justify` | string | `'L'` |  |
| `text_opacity` | number | `1.0` |  |
| `text_margin_x` | integer | `64` |  |
| `text_margin_y` | integer | `36` |  |
| `text_scrim` | number | `0.5` |  |
| `date_format` | string | `'%-d %B %Y'` |  |
| `show_clock` | boolean | `False` |  |
| `clock_format` | string | `'%H:%M'` |  |
| `clock_size` | integer | `120` |  |
| `clock_position` | string | `'TR'` | TL · TC · TR · BL · BC · BR |
| `clock_opacity` | number | `0.9` |  |
| `clock_offset_pct` | list of numbers | `[3.0, 3.0]` |  |
| `clock_extra_file` | string | `'/dev/shm/picframe-clock.txt'` |  |
| `overlay_image` | string | `'/dev/shm/picframe-overlay.png'` | If this PNG exists it is drawn over the picture. A hook for anything. |

## `library`

Where the photographs are and how they are indexed.

| key | type | default | |
|---|---|---|---|
| `picture_folders` | list of strings | `['~/Pictures']` |  |
| `database` | string | `'~/.local/share/picframe3/library.db3'` |  |
| `follow_links` | boolean | `False` |  |
| `include_videos` | boolean | `True` |  |
| `ignore_hidden` | boolean | `True` |  |
| `exclude` | list of strings | `['@eaDir', '.thumbnails', '#recycle']` |  |
| `watch` | boolean | `True` | inotify: new photographs appear within seconds. |
| `rescan_interval` | number | `3600.0` | Full walk as a backstop, in seconds. 0 disables it. |
| `scan_on_start` | boolean | `True` |  |
| `deleted_folder` | string | `'~/.local/share/picframe3/deleted'` |  |
| `subfolder` | string | *(empty)* | Live filter; also settable from MQTT and the web UI. |

## `geo`

Turning GPS coordinates into place names for captions.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `False` |  |
| `contact` | string | *(empty)* | **Required when `enabled`.** Nominatim's usage policy needs a contact address. |
| `language` | string | `'en'` |  |
| `cache` | string | `'~/.local/share/picframe3/geocache.db3'` |  |
| `suppress` | list of strings | `[]` |  |
| `key_order` | list[list[str]] | `[['tourism', 'attraction', 'amenity', 'isolated_dwelling'], ['neighbourhood', 'suburb', 'village', 'town'], ['city', 'municipality', 'county'], ['state', 'province', 'region'], ['country']]` |  |

## `mqtt`

The broker, and the Home Assistant device it announces.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `False` |  |
| `host` | string | *(empty)* |  |
| `port` | integer | `1883` |  |
| `username` | string | *(empty)* |  |
| `password` | string | *(empty)* |  |
| `tls_ca` | string | *(empty)* | Path to a CA certificate; use port 8883 with it. |
| `tls_insecure` | boolean | `False` |  |
| `device_id` | string | `'picframe'` |  |
| `device_name` | string | `'Picture Frame'` |  |
| `discovery_prefix` | string | `'homeassistant'` |  |
| `topic_prefix` | string | `'picframe'` |  |
| `publish_interval` | number | `30.0` |  |

## `http`

The web interface and REST API.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `True` |  |
| `host` | string | `'0.0.0.0'` |  |
| `port` | integer | `9000` |  |
| `auth_user` | string | *(empty)* | Set this and `auth_password` to require HTTP basic auth. |
| `auth_password` | string | *(empty)* |  |
| `allow_delete` | boolean | `False` |  |
| `cors_origins` | list of strings | `[]` |  |

## `input`

Keyboard, touchscreen, mouse and GPIO buttons.

| key | type | default | |
|---|---|---|---|
| `keyboard` | boolean | `True` |  |
| `touch` | boolean | `True` |  |
| `mouse` | boolean | `False` |  |
| `gpio_buttons` | mapping | `{}` | BCM pin numbers, e.g. `{next: 17, pause: 27}`. |
| `gpio_pull_up` | boolean | `True` |  |
| `wake_on_input` | boolean | `True` |  |
| `keymap` | mapping | `{'next': ['KEY_RIGHT', 'KEY_SPACE', 'KEY_DOWN'], 'previous': ['KEY_LEFT', 'KEY_UP'], 'pause': ['KEY_P'], 'display_toggle': ['KEY_O'], 'info_toggle': ['KEY_I'], 'clock_toggle': ['KEY_C'], 'delete': ['KEY_DELETE'], 'quit': ['KEY_ESC', 'KEY_Q']}` | evdev key names, e.g. `KEY_RIGHT`. |

## `power`

When the screen is on, off, or dimmed.

| key | type | default | |
|---|---|---|---|
| `schedule` | mapping | `{}` | `{all: ["22:30-07:00"]}` — ranges may cross midnight. |
| `dim_schedule` | mapping | `{}` | `{"19:00-22:30": 0.45}` |
| `enabled` | boolean | `True` | Named `enabled`, not `on`: YAML reads a bare `on:` key as a boolean. |

## `logging`

Where log output goes.

| key | type | default | |
|---|---|---|---|
| `level` | string | `'INFO'` |  |
| `file` | string | *(empty)* |  |
| `journald` | boolean | `True` |  |

