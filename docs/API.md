# Interfaces

## Commands

Every control surface produces the same command. The actions are:

| action | payload | meaning |
|---|---|---|
| `next`, `previous` | — | move through the playlist |
| `pause`, `resume`, `toggle_pause` | — | hold the current picture |
| `jump` | `{"id": 42}` or `{"path": "/…/a.jpg"}` | show a specific picture |
| `delete` | — | move the current picture to `library.deleted_folder` |
| `display_on`, `display_off`, `display_toggle` | — | panel power (DRM DPMS) |
| `brightness` | `{"value": 0.0–1.0}` | dim without turning off |
| `info_toggle`, `info_show` | — | the caption overlay |
| `clock_toggle` | — | the clock overlay |
| `set_config` | `{"key": "slideshow.interval", "value": 120}` | any setting, live |
| `set_filters` | see below | narrow the playlist |
| `rescan` | — | walk the picture folders now |
| `reload` | — | re-read the config file |
| `quit` | — | stop the process |

Filter payload: `subfolder`, `tags_any`, `tags_all`, `tags_none`, `date_from`,
`date_to`, `min_rating`, `location_contains`, `search`, `include_videos`,
`include_images`.

## HTTP

Default `http://<frame>:9000`. Interactive documentation at `/api/docs`.

| method | path | |
|---|---|---|
| GET | `/api/state` | the full state snapshot |
| GET | `/api/events` | server-sent events; one message per state change |
| POST | `/api/command` | `{"action": "...", ...}` |
| POST | `/api/{action}` | shorthand, e.g. `POST /api/next` |
| GET | `/api/config` | the whole configuration |
| PATCH | `/api/config?persist=true` | `{"slideshow.interval": 90}` |
| GET | `/api/library` | counts, date range, size on disk |
| GET | `/api/library/folders`, `/api/library/tags` | with counts |
| GET | `/api/library/photos?q=&limit=&offset=` | list or full-text search |
| GET | `/api/library/photo/{id}` | one record |
| GET | `/api/library/photo/{id}/thumb` | JPEG thumbnail, cacheable |
| GET | `/api/library/photo/{id}/file` | the original file |
| GET | `/api/screenshot` | **a PNG of what is on the frame's screen right now** |
| GET | `/api/transitions` | the available transition names |
| GET | `/api/health` | liveness, no auth |

Set `http.auth_user` and `http.auth_password` for HTTP basic auth on everything
except `/api/health`.

The frame has no desktop and therefore no screenshot tool, so it reads its own
framebuffer back: `/api/screenshot` draws the next frame, captures it before it
is presented, and returns it. That is how a frame on a wall becomes something
you can attach to a message.

```bash
curl -o frame.png http://frame.local:9000/api/screenshot
curl -XPOST http://frame.local:9000/api/next
curl -XPOST http://frame.local:9000/api/command \
     -H 'content-type: application/json' \
     -d '{"action":"set_config","key":"slideshow.transition","value":"zoom"}'
curl -N http://frame.local:9000/api/events
```

## MQTT

Topics, with `mqtt.topic_prefix` defaulting to `picframe` and `device_id` to
`picframe`:

| topic | direction | |
|---|---|---|
| `picframe/picframe/state` | out, retained | the whole state as JSON |
| `picframe/picframe/availability` | out, retained | `online` / `offline` (Last Will) |
| `picframe/picframe/cmd` | in | an action name, or a full command as JSON |
| `picframe/picframe/display/set` | in | `on` / `off` |
| `picframe/picframe/brightness/set` | in | `0`–`255` |
| `picframe/picframe/pause/set` | in | `on` / `off` |
| `picframe/picframe/next/set` … | in | any payload triggers it |
| `picframe/picframe/interval/set` | in | seconds |
| `picframe/picframe/transition/set` | in | a transition name |
| `picframe/picframe/order/set` | in | a playlist order |
| `picframe/picframe/subfolder/set` | in | restrict to a subfolder |

State is published as **one** JSON document that every Home Assistant entity
reads with a template, so a frame with a dozen entities still produces one
message per update.

### Home Assistant entities

Discovered automatically under one device:

- `light.<name>_display` — on/off and brightness
- `switch.<name>_pause`
- `button.<name>_next_picture`, `_previous_picture`, `_rescan_library`,
  `_restart_the_frame`, `_remove_current_picture`
- `number.<name>_seconds_per_picture`
- `select.<name>_transition`, `select.<name>_order`
- `sensor.<name>_current_picture` — filename, with all metadata as attributes
- `sensor.<name>_title` — title, else caption, else filename
- `sensor.<name>_taken` — a real `timestamp` entity, so Home Assistant can
  format it and automations can compare it
- `sensor.<name>_place` — the place name, with `latitude` / `longitude` /
  `source_type: gps` as attributes, which a map card reads directly
- `sensor.<name>_tags` — comma-separated, with the list itself as an attribute
- `sensor.<name>_camera` — model, with make, lens, aperture, shutter, ISO and
  focal length as attributes
- `sensor.<name>_folder`
- `sensor.<name>_laid_out_as` — mat · cover · contain · blur (diagnostic)
- `sensor.<name>_pictures_indexed`
- `sensor.<name>_shuffle_round`, `sensor.<name>_left_in_this_round` — how far
  through the current pass over the library the frame is (diagnostic)
- `binary_sensor.<name>_scanning`
- `binary_sensor.<name>_restart_needed` — on when a setting has been changed
  that only a fresh process will pick up, with the list as an attribute

All of them read one retained JSON document on `…/state`, so adding entities
costs no extra traffic.

## Configuration

| endpoint | |
|---|---|
| `GET /api/config` | the whole configuration as JSON, with passwords masked as `••••••••` |
| `GET /api/config/schema` | every setting with its type, label, explanation, legal values, default, and whether it takes effect without a restart — generated from the dataclasses, and what the Settings tab draws itself from |
| `PATCH /api/config` | `{"slideshow.interval": 90}`; add `?persist=true` to write the config file |
| `POST /api/geo/preview` | `{"detail": "custom", "key_order": [["village","town"],["country"]]}` → what that would write under the picture currently on screen, plus the address keys that picture's own reply carries. Reads the geocache only; never makes a network request |
| `POST /api/restart` | stop cleanly and come back up; saves the configuration first unless `?save=false` |

Writing the mask `••••••••` back to a password changes nothing, so reading the
configuration, editing one key and sending it all back cannot blank a secret.

Most settings apply the moment they are set. The ones that cannot — the MQTT
broker, the HTTP port, which folders are indexed — are listed in
`state.restart_required`, badged **restart** on the settings page, and marked
*(takes effect on restart)* in the [configuration reference](CONFIG.md). The
Settings tab shows a notice naming them with a **Save & restart** button, and
there is a **Restart the frame** button alongside Save and Reload.

Under systemd the restart is a clean exit: `Restart=always` starts a fresh unit
a few seconds later, in a new cgroup, with the DRM device released by the
kernel rather than by code unwinding while it still owns the screen. Started by
hand, the process re-executes itself once the event loop has finished.

## Light and dark

The web interface follows the device by default and can be pinned either way
from the picker in the header: **Auto · Light · Dark**. The choice is kept in
that browser's local storage rather than in the frame's configuration — the
phone in a dark bedroom and the laptop at the desk are looking at the same
frame and want different answers. A tiny inline script stamps `data-theme` on
`<html>` before the stylesheet paints, so a pinned page never flashes the
other theme on its way to the right one; with scripting off, a
`prefers-color-scheme` media query still delivers a whole palette.

Photographs are shown against a neutral dark ground in both themes, the way
they would be in a frame.

## The overlay hook

If the PNG at `viewer.overlay_image` (default `/dev/shm/picframe-overlay.png`)
exists, it is scaled to the panel and drawn over the picture, beneath the
caption and clock. Write a transparent PNG there from anything — a weather
script, a calendar, a doorbell camera — and it appears within a second. Delete
the file and it disappears.
