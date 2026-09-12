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
- **Video** playback with audio, hardware decoding where the Pi provides it
- **HEIC/HEIF** (iPhone) support
- Web UI, REST API, server-sent events, MQTT with **Home Assistant discovery**
- **Screenshots of the frame itself** — it reads its own framebuffer back, so
  what is on the wall can be captured with `curl` or one button in the web UI
- Keyboard, touchscreen gestures, mouse, and GPIO buttons — read from evdev, so
  they work with no desktop installed
- Night-time screen-off and evening-dimming schedules
- `picframe3 doctor` — checks the hardware, drivers, permissions and library and
  tells you the command to fix whatever is missing

---

## Quick start

On **Raspberry Pi OS Lite (64-bit)**, Bookworm or Trixie:

```bash
# from the source tarball
tar xzf picframe3-*-source.tar.gz
bash picframe3/packaging/install.sh

# or, once this is published
curl -fsSL https://raw.githubusercontent.com/picframe3/picframe3/main/packaging/install.sh | bash
```

That is the whole installation. It fetches the libraries, creates a virtual
environment, sets the permissions, tidies the boot options, and then asks you
where your pictures are, whether you want a network share to drop photographs
onto, whether to enable the web interface and Home Assistant, how it should
look, and whether to start on boot. Every question has a working default.

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

MIT, as `picframe` is. The design owes a great deal to Helge Erbe, Paddy Gaunt
and Jeff Godfrey, whose work established what a good Pi picture frame does.
