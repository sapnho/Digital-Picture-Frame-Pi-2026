# Moving from picframe

## The short version

```bash
picframe3 migrate ~/picframe_data/config/configuration.yaml
picframe3 scan
picframe3 doctor
systemctl --user disable --now picframe        # stop the old one
sudo systemctl enable  --now picframe3@$USER
```

`--user`, because picframe is installed as a **user** unit
(`~/.config/systemd/user/picframe.service`). Run with `sudo` it fails with
"Unit picframe.service does not exist" — which is easy to read as "it was
already gone", and it was not: the old frame returns at the next login and the
two then fight over DRM master.

`migrate` prints exactly what it carried over and what it dropped, and why.
Your pictures are never touched — picframe3 builds its own index in a new
database file and leaves `pictureframe.db3` alone, so you can go back.

## What carries over

Picture folders, interval, fade time, shuffle and reshuffle settings, portrait
pairing, recent-days weighting, the caption fields and their size, justification
and opacity, the date format, mat colours and borders, blur settings, Ken
Burns, the clock, the deleted-pictures folder, geocoding, your empty-library
picture (`no_files_img` → `viewer.no_files_img`), and the MQTT and HTTP
settings.

## What does not, and why

| picframe setting | why it is gone |
|---|---|
| `use_sdl2`, `use_glx` | there is no SDL and no X server; the frame talks to DRM/KMS |
| `display_power`, `display_hdmi` | display power is DRM DPMS on every Pi — one path, no choice to make. If autodetection picks the wrong output, set `display.connector: HDMI-A-2` |
| `display_x/y/w/h` | the frame always uses the output's native mode |
| `shader`, `blend_type` | transitions are built in: `picframe3 transitions` lists fifteen. `blend`→`fade`, `burn`→`burn`, `bump`→`bump` |
| `fps` | the loop is adaptive — full rate while something moves, **no frames at all** while a still picture is up |
| `mat_resource_folder` | mats are generated, including the paper grain. Nothing to install |
| `image_attr` | all metadata is published; there is no allow-list to maintain |
| `sort_cols` | `slideshow.order`: `shuffle`, `random`, `date_desc`, `date_asc`, `name`, `folder`, `recent`, `least_played` |
| `update_interval` | inotify notices new files in seconds; `library.rescan_interval` is only a backstop |
| `peripherals.buttons` | keys are mapped in `input.keymap` using evdev names (`KEY_RIGHT`, `KEY_SPACE`); GPIO pins in `input.gpio_buttons` |
| `http.path` | the web UI ships inside the package |

## Things that behave differently

**Matting.** `mat_images: 0.01` becomes `viewer.fit: auto` with
`viewer.mat_tolerance: 0.01` — same meaning. `mat_type` becomes
`viewer.mat_style`, and every picframe style name still works:

| picframe | picframe3 |
|---|---|
| `float` | `float` (no shadow) / `float_shadow` |
| `float_polaroid` | `polaroid` |
| `float_color_wrap` | `float_wrap` |
| `single_bevel` | `single_bevel` |
| `double_bevel` | `double_bevel` |
| `double_flat` | `double` |

`""` (any) still works, `random` picks per picture, and a space-separated list
(`float polaroid double_flat`) still means "choose from these".

The mats *look* different in two deliberate ways: the board colour comes from a
k-means over the photograph rather than a mean, and the bevels, shadows,
hairline and paper grain are drawn procedurally instead of stretched from
nine-patch PNGs, so they stay correct at any panel size — and there is no
`mat_resource_folder` to install.

**Place names arrive as you watch.** picframe looked a location up the first
time a photo with coordinates was displayed. picframe3 does the same, and adds
a background trickle that fills the rest of the library in at the one request
per second OpenStreetMap asks for. Turning `geo.enabled` on after the library
is already indexed therefore works — no re-index needed. `picframe3 scan`
resolves everything outstanding in one go, and `--regeocode` starts over.

**Fades look lighter in the middle.** They are blended in linear light now.
The old midpoint was too dark; this is the fix, not a regression.

**Portrait pairs are matched on height.** picframe scaled both to the narrower
width, which cropped the taller of the two. Nothing is cropped now; the gap
absorbs the difference.

**The clock and captions are real text.** Pillow with libraqm shapes Arabic,
Hebrew and Indic scripts correctly; pi3d's `FixedString` could not.

**Videos crossfade.** The video is the slide texture, so it fades in and out
like a photograph and the captions stay on top. There is no separate VLC window.

## If you added a Wi-Fi watchdog of your own

Many picframe installations grew a little `check_wifi.py` under systemd or cron,
because a frame that silently falls off the network is the one fault the frame
itself never reports. picframe3 does that job now — `network:` in the config —
so **turn the old one off**:

```bash
sudo systemctl disable --now check_wifi.service    # or remove the cron line
```

Leaving both running is worse than either alone: two watchdogs restart the
network independently, each one making the other's next check fail.

It is worth reading the old script before deleting it, because most of them
share two flaws that make the cure worse than the disease. They ping an address
on the internet (8.8.8.8, usually), so the router's nightly reconnect looks
identical to broken Wi-Fi; and they act on a single failed ping with no cooldown,
so the restart they trigger makes the next check fail too, and the frame spends
the night restarting its network every few minutes until someone pulls the plug.
picframe3 pings the gateway, needs three failed checks in a row, and will not
touch the connection twice within half an hour.

The one thing it needs that a root cron job did not: permission. The frame runs
as an ordinary user with `NoNewPrivileges=yes`, so `picframe3 setup` installs a
polkit rule (`/etc/polkit-1/rules.d/50-picframe3-network.rules`) granting that
user exactly two things — reconnecting a network device, and restarting
`NetworkManager.service`. Without the rule the frame still reports outages; it
just cannot mend them.

## Home Assistant

The MQTT entities are new (`picframe3_<device_id>_*`), so Home Assistant will
create a new device alongside the old one. Delete the old device once you are
happy. Automations referring to the old entity ids need updating; the entities
are listed in [API.md](API.md).

If you want the frames to coexist during the changeover, give picframe3 a
different `mqtt.device_id`.

## Running both at once

You cannot: only one process can be DRM master. Stop picframe before starting
picframe3.
