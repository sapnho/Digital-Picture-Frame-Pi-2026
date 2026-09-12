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
| `info_toggle`, `info_show`, `info_hide` | `info_show` takes `{"seconds": 90}` | the caption overlay. `info_show` reveals it for `viewer.peek_seconds`; a frame with `text_seconds: 0` writes nothing until asked |
| `clock_toggle` | — | the clock overlay |
| `set_config` | `{"key": "slideshow.interval", "value": 120}` | any setting, live |
| `set_filters` | see below | narrow the playlist |
| `rescan` | — | walk the picture folders now |
| `reload` | — | re-read the config file |
| `restart` | — | stop and come back up with a fresh process |
| `shutdown` | — | stop, then power the Pi off |
| `quit` | — | stop the process, and leave it stopped |

Filter payload: `subfolder`, `tags_any`, `tags_all`, `tags_none`, `date_window`,
`date_from`, `date_to`, `min_rating`, `location_contains`, `search`,
`include_videos`, `include_images`.

## HTTP

Default `http://<frame>:9000`. There is **no** interactive documentation:
Swagger UI fetches its assets from a CDN and the frame is often on a network
with no route to one, so `/api/docs` is a plain-text notice saying so. The
machine-readable schema is served at `/api/openapi.json`, behind the same
authentication as everything else — point any OpenAPI client at it.

| method | path | |
|---|---|---|
| GET | `/api/state` | the full state snapshot |
| GET | `/api/events` | server-sent events; one message per state change |
| POST | `/api/command` | `{"action": "...", ...}` |
| GET | `/api/config` | the whole configuration, passwords masked |
| GET | `/api/config/schema` | every setting with type, label, explanation and default |
| PATCH | `/api/config?persist=true` | `{"slideshow.interval": 90}` |
| POST | `/api/geo/preview` | what a `detail` / `key_order` would write under the current picture |
| POST | `/api/restart` | stop cleanly and come back up; `?save=false` to skip saving first |
| POST | `/api/shutdown` | stop cleanly, then power the Pi off; `?save=false` to skip saving first |
| GET | `/api/filters` | the filter in force, plus the folders, tags and places to choose from |
| POST | `/api/filters` | change one or more filters; absent keys are left alone |
| POST | `/api/filters/preview` | how many pictures a filter *would* select, applying nothing |
| GET | `/api/library` | counts, date range, size on disk |
| GET | `/api/library/folders`, `/api/library/tags`, `/api/library/locations` | with counts |
| GET | `/api/library/photos?q=&limit=&offset=&selected=1` | list, full-text search, or exactly what the slideshow is drawing from |
| GET | `/api/library/photo/{id}` | one record |
| GET | `/api/library/photo/{id}/thumb` | JPEG thumbnail, cacheable |
| GET | `/api/library/photo/{id}/file` | the original file |
| GET | `/api/removed?include_restored=&include_purged=&limit=` | the removal journal, newest first |
| GET | `/api/removed/summary` | how many are in the deleted folder and what they weigh |
| GET | `/api/removed/{stored_as}/thumb` | JPEG thumbnail of a removed picture |
| GET | `/api/removed/{stored_as}/file` | the removed file itself |
| POST | `/api/removed/{stored_as}/restore` | put it back where it came from, and say where that was |
| POST | `/api/removed/{stored_as}/allow` | show that picture again: the one thing that clears a hold |
| POST | `/api/removed/{stored_as}/purge` | delete that file for good; its journal line stays |
| POST | `/api/removed/empty` | empty the trash: delete every file in it for good |
| GET | `/api/removed/journal` | the raw journal as `removals.jsonl` (NDJSON) |
| GET | `/api/current?size=` | **a JPEG of the photograph on the frame right now** |
| GET | `/api/screenshot` | **a PNG of what is on the frame's screen right now** |
| GET | `/api/transitions` | the available transition names |
| GET | `/api/caption-fields` | what can be written over a picture, with labels |
| GET | `/api/mat-styles` | the mat styles, with the wording the settings page uses |
| GET | `/api/fits` | what `fit: auto` may do with a picture that needs help |
| GET | `/api/geo-detail` | how much of an address a caption may show |
| GET | `/api/date-windows` | the rolling date filters, and which one is in force |
| GET | `/api/openapi.json` | the OpenAPI schema |
| GET | `/api/docs` | a plain-text notice; see above |
| GET | `/api/health` | liveness, no auth |
| POST | `/api/{action}` | shorthand, e.g. `POST /api/next` — **registered last**, see below |

Set `http.auth_user` and `http.auth_password` for HTTP basic auth on everything
except `/api/health`.

### Route order, and why it matters

`POST /api/{action}` matches any single-segment POST under `/api/`, and
Starlette matches routes in the order they were registered. It is therefore
declared **after** every other route, and nothing but the static files may be
registered below it. Placed higher, it swallows its specific neighbours: that
is exactly how `POST /api/restart?save=true` once arrived at the shorthand
handler instead of the restart handler, where the query parameter was never
read and **Save & restart** silently discarded the changes it had been asked
to apply.

### Removed pictures

"Remove" never unlinks anything: the picture is moved to
`library.deleted_folder` and an entry is appended to a journal beside it. The
journal is what these endpoints read, not the folder listing — the folder only
knows there is a file called `IMG_4312.jpg`, while the journal knows it was
taken in Lisbon in 2019, removed on Tuesday from the web interface, and which
folder it belongs back in. `{stored_as}` is the name the file was given in the
deleted folder, as returned by `GET /api/removed`.

A removal is also remembered by the picture's **content**, not only by its
path. Deleting a row is undone by any copy of the file landing back in the
folder — which is what a two-way Syncthing folder, a restored backup or the
same photograph arriving again from a phone actually does — so the frame keeps
the SHA-256 of every removed picture in a small `holds` table. A file whose
size matches one of them (the size is free, it comes with the scan's own
`stat`) is hashed, and if it is the same picture it is indexed but marked
`held`: out of the playlist, out of the browsing grid, listed on the Removed
tab as *turned up again — still held out*, and counted by the `came_back`
sensor in Home Assistant.

Nothing clears a hold but somebody saying so: `POST
/api/removed/{stored_as}/allow`, the *Show this again* button, or the `release`
action over MQTT. Putting a picture back from the trash releases it too —
restoring a picture *is* saying it may be shown again, and without that the
file would go back to its folder and the next scan would quietly hold it out.

Removing over the network is off unless `http.allow_delete` is set: without it
`delete` is refused with 403 and a picture can only be removed at the frame
itself, with a button or a key. `POST /api/removed/{stored_as}/restore` puts a
picture back whatever that setting says.

Emptying the trash is the one thing here that does delete. `POST
/api/removed/{stored_as}/purge` unlinks one file and `POST /api/removed/empty`
unlinks all of them; both then **mark** the journal line `purged_at` rather
than removing it, so the record of what the picture was, when it went and
where it came from outlives the JPEG — and the Removed tab keeps the row,
says the file is gone and stops offering to put it back. Both obey
`http.allow_delete`: deleting for good cannot be easier than removing. Only
names the journal accounts for are ever unlinked, so a file somebody else put
in the deleted folder is counted (`left_alone` in the reply) and left where it
is. `/api/removed/empty` is registered **before** the `{stored_as}` routes;
declared after them, `empty` would be read as a file name.

### What answers, and from where

Three checks sit in front of every request, in one piece of ASGI middleware:

- **A body limit** of 1 MiB. Starlette buffers a request body in memory before
  any handler sees it, so without a limit an unauthenticated POST of a
  gigabyte is enough to have the frame killed by the OOM killer — and with
  `Restart=always` that is a restart loop, not a single crash.
- **The `Host` header** must be the frame's own name or address (its hostname,
  `<hostname>.local`, `localhost`, whatever `http.host` names, or an address it
  actually has). This is what stops a page on the internet from reading
  `/api/config` through the owner's own browser.
- **`Origin` / `Referer` on anything that changes state.** A POST with no body
  and no content type is a CORS "simple request" — no preflight, so CORS never
  gets a say and any site the owner visits could quietly fire `/api/delete`. A
  browser always labels such a request with its `Origin`, and one that does not
  match this host is refused. A request carrying neither header is left alone:
  that is curl, Home Assistant or a shell script, none of which a website can
  forge.

`http.cors_origins` names the sites that may call this API from a browser.
`*` is refused out loud and logged rather than honoured: on an interface with
no password it would grant every page on the web the run of the frame.

`/api/health` is a liveness probe and nothing more -- it answers `{"ok": true}`
to a monitoring system without a password. What the Pi is actually doing --
temperature, load, memory, free space, the power supply -- rides in the
`health` block of the state document, behind whatever authentication the rest
of the API has.

The frame has no desktop and therefore no screenshot tool, so it reads its own
framebuffer back: `/api/screenshot` draws the next frame, captures it before it
is presented, and returns it. That is how a frame on a wall becomes something
you can attach to a message.

### Filtering

Which pictures are in the running is a **patch**, not a whole filter set:
every control surface has one text box, one dropdown, one switch, and a box
that wiped the date range whenever somebody typed a place name into it would
be unusable. Keys not mentioned are left as they are; `{"reset": true}` clears
the lot.

| key | |
|---|---|
| `folder` | any part of a path; `""` for all of them |
| `tags` | `"holiday, france"` or `["holiday", "france"]` |
| `tags_match_all` | `false` (any of them, the default) or `true` (all of them) |
| `location` | any part of a place name — `France` catches every town in it |
| `date_window` | a rolling window: `today`, `7d`, `30d`, `90d`, `1y`, `3y`, `on_this_day`, or `all` |
| `date_from`, `date_to` | `2024-07-14`, `14.07.2024`, or a Unix timestamp; `""` for no limit |
| `min_rating` | 1–5 |
| `search` | free text over titles, captions, tags and places |
| `include_videos`, `include_images` | |
| `reset` | `true` clears everything |

An upper date limit means the whole of that day, not midnight at the start of
it. The folder is also a setting (`library.subfolder`), so it survives a
restart and the settings page always agrees with the filter panel.

`date_window` is a *rule*, not a pair of dates, and that distinction is the
point of it. Setting `7d` stores "the last seven days"; the frame works out
what that means every time it asks the library, and re-resolves the selection
when the day turns. Storing the two dates instead — which is what a client
computing them itself would do — freezes the filter on the day it was set, and
a month later the frame is still showing that same week with nothing to say so.
`on_this_day` is the same calendar day in every year. A window and an explicit
`date_from`/`date_to` are two answers to one question, so setting either clears
the other.

```bash
# only the holiday pictures, from this summer
curl -XPOST http://frame.local:9000/api/filters \
     -H 'content-type: application/json' \
     -d '{"tags":"holiday","date_from":"2026-06-01"}'

# how many would that be?  (changes nothing)
curl -XPOST http://frame.local:9000/api/filters/preview \
     -H 'content-type: application/json' -d '{"tags":"holiday"}'

curl -XPOST http://frame.local:9000/api/filters -d '{"reset":true}' \
     -H 'content-type: application/json'
```

`/api/current` is the other half of that pair, and the cheaper one: the
photograph itself rather than the screen, so it is there while the display is
off and it costs the render loop nothing. `size` is the longest edge, 64 to
3840.

```bash
curl -o picture.jpg 'http://frame.local:9000/api/current?size=1280'
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
| `picframe/picframe/image` | out, retained | the current photograph as a JPEG |
| `picframe/picframe/cmd` | in | an action name, or a full command as JSON |
| `picframe/picframe/display/set` | in | `on` / `off` |
| `picframe/picframe/brightness/set` | in | `0`–`255` |
| `picframe/picframe/pause/set` | in | `on` / `off` |
| `picframe/picframe/captions/set` | in | `on` / `off` — write a caption at all |
| `picframe/picframe/caption_fields/set` | in | `date, location` — which elements |
| `picframe/picframe/info_show/set` | in | any payload reveals the caption for `viewer.peek_seconds` |
| `picframe/picframe/next/set` … | in | any payload triggers it |
| `picframe/picframe/interval/set` | in | seconds |
| `picframe/picframe/transition/set` | in | a transition name |
| `picframe/picframe/order/set` | in | a playlist order |
| `picframe/picframe/subfolder/set` | in | restrict to a subfolder |
| `picframe/picframe/folder_filter/set` | in | a folder from the list, or `(all)` |
| `picframe/picframe/tags_filter/set` | in | `holiday, france` |
| `picframe/picframe/tags_match_all/set` | in | `on` / `off` |
| `picframe/picframe/location_filter/set` | in | any part of a place name |
| `picframe/picframe/date_from/set`, `…/date_to/set` | in | `2024-07-14`, or empty for no limit |
| `picframe/picframe/dates_7d/set`, `…/dates_today/set`, `…/dates_on_this_day/set`, … | in | one button per rolling window; any payload presses it |
| `picframe/picframe/clear_filters/set` | in | any payload shows everything again |

State is published as **one** JSON document that every Home Assistant entity
reads with a template, so a frame with a dozen entities still produces one
message per update.

### Home Assistant entities

Discovered automatically under one device:

- `light.<name>_display` — on/off and brightness
- `switch.<name>_pause`
- `button.<name>_next_picture`, `_previous_picture`, `_rescan_library`,
  `_restart_the_frame`, `_shut_the_frame_down`, `_remove_current_picture`
- `number.<name>_seconds_per_picture`
- `select.<name>_transition`, `select.<name>_order`
- `image.<name>_picture` — **the photograph on the frame**, sent as a JPEG on
  its own topic whenever the picture changes and retained, so a dashboard shows
  it straight after a Home Assistant restart. The photograph, not the screen:
  no mat, no caption, and there even while the display is off. Off with
  `mqtt.publish_image: false`; `mqtt.image_width` and `mqtt.image_quality` say
  how large it goes over the wire
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
**Which pictures are in the running** — the four boxes picframe's own card
had, and the reason to put a frame in Home Assistant at all ("only the holiday
pictures", "only this Christmas", said from the sofa). Each entity sends only
itself and the frame merges it into the filter already in force:

- `select.<name>_folder` — the folders in the library, plus `(all)`. Shows
  `(other)` when the folder was set to a free-text fragment from the settings
  page, rather than becoming a state the select has no option for
- `text.<name>_tags_filter` — `holiday, france`
- `switch.<name>_match_all_tags` — off means any of them, on means all of them
- `text.<name>_place_filter` — any part of a place name
- `text.<name>_pictures_from`, `text.<name>_pictures_until` — `2024-07-14`,
  empty for no limit
- `sensor.<name>_selected_pictures` — how many are in the running, with the
  whole filter as attributes
- `binary_sensor.<name>_filter_active`
- `button.<name>_show_everything_again`

**The caption**, for a frame that keeps it off and asks for it when somebody
wants to know where a photograph was taken — which is what picframe's owners
built out of its text switches:

- `switch.<name>_captions` — whether anything is written over the picture
- `text.<name>_caption_elements` — `date, location` (config)
- `button.<name>_show_the_caption` — reveals it for `viewer.peek_seconds`

- `binary_sensor.<name>_scanning`
- `binary_sensor.<name>_restart_needed` — on when a setting has been changed
  that only a fresh process will pick up, with the list as an attribute

And, while `health.enabled` is on, the Pi itself — all diagnostic:

- `sensor.<name>_cpu_temperature` — °C, a real `temperature` entity
- `sensor.<name>_cpu_load` — the one-minute load as a percentage of the cores
  there are, with all three averages as attributes
- `sensor.<name>_memory_used` — percent, with the byte figures as attributes
- `sensor.<name>_free_space` — GiB free on the disk the photographs are on,
  which is not necessarily the root filesystem
- `binary_sensor.<name>_power_supply` — a `problem` entity, on for an
  undervoltage happening now **and** for one the firmware recorded earlier in
  this boot. A frame that browns out at 3am looks perfect by breakfast; the
  flag is the only trace, and undervoltage is what corrupts SD cards

Turning `health.enabled` off withdraws those five from Home Assistant rather
than leaving them behind, permanently unavailable.

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
| `POST /api/shutdown` | stop cleanly, then ask systemd to power the Pi off; saves the configuration first unless `?save=false`. Answers `503` where there is no systemd to ask, which is also what `state.can_shutdown` says |
| `POST /api/quit` | stop, and stay stopped: the process exits 143, and `RestartPreventExitStatus=143` in the unit keeps systemd from starting it again. `sudo systemctl start picframe3@<user>` brings it back |

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

### Shutting the frame down

**Shut the frame down**, in the *Rarely needed* strip at the foot of the
Settings tab and as a button in Home Assistant, powers the **Pi** off — so the
frame can be switched off from the sofa and the plug pulled without corrupting
the card. It is not a restart with nothing after it: only the power switch
brings the frame back.

The frame stops the slideshow the way it always does and then asks systemd for
the power-off, once the event loop has ended and the screen has been handed
back. Doing that as an ordinary user needs one narrow polkit rule —
`/etc/polkit-1/rules.d/55-picframe3-power.rules`, this user and logind's
`power-off` action, nothing else — which `picframe3 setup` installs and
`picframe3 doctor` checks. Without it the Pi stays on, the frame comes back
rather than leaving a dark screen, and the journal says why.

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
