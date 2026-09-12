# picframe3

A digital picture frame for **current Raspberry Pi OS**, rebuilt from the ground up.

It renders with **OpenGL ES 3 on EGL, directly onto DRM/KMS** — no X server, no
Wayland compositor, no desktop session. Video goes through **GStreamer**, the
photo index is **SQLite with FTS5**, control is **asyncio** with a web UI, MQTT
and Home Assistant discovery, and the whole thing installs as a **systemd
service** on Raspberry Pi OS Lite.

It is a reimplementation of the ideas in
[helgeerbe/picframe](https://github.com/helgeerbe/picframe) — the matting, the
metadata overlays, the Home Assistant integration, the portrait pairing — on a
stack that matches the operating system as it is today rather than as it was in
2019.

---

## Why a rebuild

### In plain words

**It fits today's Raspberry Pi OS.** The old version needs a desktop session
running underneath it — a whole graphical environment, just so one photo can be
shown. Current install guides therefore ask for a compositor, autologin, a
handful of hand-written files and a pasted Samba config. picframe3 talks to the
screen directly: Raspberry Pi OS **Lite**, one command to install, nothing else
on the machine that can break.

**It does almost nothing most of the time.** While a photo is on screen,
picframe3 draws exactly zero frames — the picture simply stays on the panel.
pi3d redraws the same unchanged image dozens of times a second, around the
clock. The frame runs cooler and quieter.

**Videos behave like photographs.** Before, a video opened a separate player
window on top of everything: no fade, no caption over it. Here a video is just
another slide — it crossfades in, the overlay composites on top, and it plays
through once.

**Fades look right.** Crossfades blended the old way dip muddy and dark in the
middle. picframe3 blends in linear light, the way image editors do. The mat
board is drawn fresh for whatever panel you have instead of being stretched from
four fixed images, and its colour comes from the photograph's dominant colours
rather than an average that turns a sunset into mud.

**Fewer things that can break.** picframe depends on ten outside packages,
including two that are effectively unmaintained for this purpose; when one of
them stops working with a new Raspberry Pi OS, the frame stops working.
picframe3 needs three — Pillow, numpy, PyYAML — all mainstream and actively
maintained. Everything else is optional. This is the single biggest reason it
should still run in five years.

**It tells you what is wrong.** `picframe3 doctor` checks hardware, drivers and
permissions and prints the fix in plain words instead of failing with a
traceback.

**New photos appear within seconds**, not at the next timed walk, and what is
shown can be filtered by folder, tag, place and date from the web UI or from
Home Assistant. Deletions are journalled, so you can see what was removed and
put it back.

**And it is tested.** 1 540 lines of tests across 14 files, against three small
test files in the original — the unglamorous part that decides whether a change
next year quietly breaks something you notice three weeks later.

The honest caveat: picframe is proven by years of people running it in their
living rooms. picframe3 is new. Everything above is true of its design and most
of it is verified on hardware, but "more robust" is a claim that only months of
running can settle.

### The technical version

`picframe` is built on [pi3d](https://github.com/pi3d/pi3d), a general-purpose
3D engine that predates the Pi's move to the open `vc4`/`v3d` graphics stack.
Everything below follows from that one dependency:

| | picframe (pi3d) | picframe3 |
|---|---|---|
| Display path | pi3d → SDL2 or GLX, needs X11 or a Wayland session | EGL → GBM → DRM/KMS, owns the screen directly |
| Desktop required | yes, in practice | no — runs on Raspberry Pi OS **Lite** |
| Idle cost | redraws at `fps` forever | **zero frames** while a still picture is up |
| Colour | blends in sRGB (fades darken in the middle) | blends in **linear light**, encodes once |
| Video | spawns VLC in its own window | GStreamer frames drawn by the same renderer |
| Text | pi3d `FixedString`, Latin only | Pillow + libraqm: shaping for Arabic, Hebrew, Indic |
| Index | SQLite, `LIKE` filters | SQLite **WAL + FTS5**, normalised tags |
| New photos | picked up on the next timed walk | **inotify**, within seconds |
| Display power | `vcgencmd` / `xset` / `wlr-randr`, depending | DRM **DPMS**, one path everywhere |
| Config errors | `KeyError` deep in the viewer | every key has a default; unknown keys warn |
| Control | MQTT and HTTP wired into the viewer | one command bus; web, MQTT, keys, touch, GPIO are peers |

`moderngl` is deliberately **not** used: it requires a desktop OpenGL 3.3 core
context, and Mesa's `v3d` driver on a Pi 4/5 exposes desktop GL 3.1 while
offering OpenGL **ES** 3.1. The EGL/GLES entry points are bound directly with
`ctypes` — about thirty of them — so there is no compiled extension to build on
the Pi and nothing to rebuild when Mesa updates.

---

## What it does

- Slideshow with **15 GPU transitions** (fade, dissolve, zoom, defocus, push,
  wipe, radial, pixelate, and pi3d's *burn* and *bump* recreated in linear light)
- **Ken Burns** pan and zoom, with the pan clamped so it never runs off the image
- **Procedural mats** — single, double, bevel, float, float-with-shadow, polaroid —
  with the board colour derived from the photograph and a generated paper grain
- **Portrait pairing**: two upright photos side by side on a landscape panel
- Blur-fill, cover, contain, or automatic per-image choice
- Captions from EXIF / IPTC / XMP, reverse-geocoded place names, and a clock
- **Video** playback with audio, hardware decoding where the Pi provides it —
  4K on a Pi 5, up to 1080p on a Pi 4 *(see [which Raspberry
  Pi](docs/INSTALL.md#which-raspberry-pi))*
- **HEIC/HEIF** (iPhone) support
- **Photographs that arrive by themselves** — the setup offers Syncthing
  alongside the file share, installs it, points it at the picture folder and
  prints the id to pair with; the settings page then shows what it is doing,
  pairs it with a phone or a Mac, and can switch it on later if you said no
- **Filter what is shown** — by folder, tags, place and date range, from the
  web UI or from Home Assistant, with a live count of how many pictures a
  filter selects before it is applied
- Web UI, REST API, server-sent events, MQTT with **Home Assistant discovery**
- **The picture itself in Home Assistant** — the photograph on the frame is
  published as an image entity, so a dashboard card shows what is on the wall
  with no camera to set up and nothing shared over the network
- **Screenshots of the frame itself** — it reads its own framebuffer back, so
  what is on the wall can be captured with `curl` or one button in the web UI
- Keyboard, touchscreen gestures, mouse, and GPIO buttons — read from evdev, so
  they work with no desktop installed
- Night-time screen-off and evening-dimming schedules
- **Shuts itself down** — one button in the web interface and one in Home
  Assistant power the Pi off, so the frame can be switched off from the sofa
  and the plug pulled without corrupting the card
- **Reports its own health** — CPU temperature, load, memory, free space and
  the Pi's undervoltage flag, in the web UI, in `doctor`, and as diagnostic
  sensors in Home Assistant
- **Watches its own network** — a frame whose Wi-Fi drops keeps showing the
  picture it already had, so nobody notices until Home Assistant has been
  missing it for hours. picframe3 checks that it can still reach the router,
  reports outages, and mends the link itself: reconnect first, restart
  NetworkManager only if that was not enough, never more than once in half an
  hour, and never a reboot
- **Settings you can find** — the settings page opens on the jobs someone
  actually comes to do (*how fast pictures change*, *when the screen turns
  off*), each card saying what the frame is doing now and holding only the
  three or four settings that job needs. Everything else is one click away in
  the full list, which is generated from the configuration itself, so no
  setting can be missing from it
- `picframe3 doctor` — checks the hardware, drivers, permissions and library and
  tells you the command to fix whatever is missing

---

## Quick start

On **Raspberry Pi OS Lite (64-bit)**, Bookworm or Trixie:

```bash
curl -fsSL https://raw.githubusercontent.com/sapnho/Digital-Picture-Frame-Pi-2026/main/packaging/install.sh | bash
```

That is the whole installation. It fetches the libraries, creates a virtual
environment, sets the permissions, tidies the boot options, and then asks you
where your pictures are, how photographs should get onto the frame (Syncthing,
a network share, or both), whether to enable the web interface and Home
Assistant, how it should look, and whether to start on boot. Every question has a working default.

Then:

```bash
sudo reboot                          # for the new group membership
picframe3 scan                       # index your pictures
sudo systemctl start picframe3@$USER
```

and open `http://<your-pi>.local:9000/`.

There is **no compositor to install, no X compatibility layer, no autostart
file, no console autologin and no unit file to write by hand** — the frame
renders straight onto DRM/KMS and starts before anyone logs in.

Full instructions, and what to remove if you are coming from a pi3d-based
frame, are in [docs/INSTALL.md](docs/INSTALL.md).

---

## Commands

```
picframe3 run          start the frame
picframe3 scan         index the picture folders and exit
picframe3 doctor       check hardware, drivers, permissions, library
picframe3 config       show / set / initialise configuration
picframe3 demo         render sample frames to a PNG, offscreen
picframe3 transitions  list the available transitions
```

`demo` needs no display at all — it renders through the same pipeline into an
image file, which is also how the renderer is tested in CI.

---

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md) — step-by-step installation on a Pi
- [docs/CONFIG.md](docs/CONFIG.md) — every setting, with defaults
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the pieces fit together
- [docs/MIGRATION.md](docs/MIGRATION.md) — moving from picframe
- [docs/API.md](docs/API.md) — REST, SSE and MQTT surfaces

---

## Requirements

- **Raspberry Pi 4, 400 or 5.** picframe3 renders with OpenGL ES 3, which
  Mesa's `v3d` driver provides on those boards. The Pi 2, 3 and Zero 2 W
  have VideoCore IV and stop at ES 2.0; they are not supported.
- Raspberry Pi OS 64-bit, Bookworm or Trixie — **Lite is the recommended image**
- Python 3.11+
- A display on HDMI or DSI

---

## Licence

MIT, as `picframe` is. The web interface sets its type in Archivo, carried in
`src/picframe3/web/fonts/` under the SIL Open Font License 1.1 (`OFL.txt` sits
beside it) so that the page needs nothing from the outside world.

The design owes a great deal to Helge Erbe, Paddy Gaunt and Jeff Godfrey, whose
work established what a good Pi picture frame does.
