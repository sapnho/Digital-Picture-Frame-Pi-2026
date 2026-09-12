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
  `_remove_current_picture`
- `number.<name>_seconds_per_picture`
- `select.<name>_transition`, `select.<name>_order`
- `sensor.<name>_current_picture` — filename, with all metadata as attributes
- `sensor.<name>_pictures_indexed`
- `binary_sensor.<name>_scanning`

## The overlay hook

If the PNG at `viewer.overlay_image` (default `/dev/shm/picframe-overlay.png`)
exists, it is scaled to the panel and drawn over the picture, beneath the
caption and clock. Write a transparent PNG there from anything — a weather
script, a calendar, a doorbell camera — and it appears within a second. Delete
the file and it disappears.
