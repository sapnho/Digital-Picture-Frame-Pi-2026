# Architecture

```
                         ┌──────────────────────────────────────────┐
   keyboard / touch ───► │                                          │
   GPIO buttons     ───► │              Command bus                 │
   MQTT             ───► │   (events.py — one Command type,         │
   Web / REST       ───► │    one State snapshot, many observers)   │
                         └───────────────┬──────────────────────────┘
                                         │
                         ┌───────────────▼──────────────────────────┐
                         │            PicFrame (app.py)             │
                         │   wiring, render loop, power, overlays   │
                         │  SlideshowController (slideshow.py):     │
                         │   what is on screen, and for how long    │
                         │  ConfigApplier (settings.py):            │
                         │   one table, setting → effect            │
                         └───┬───────────────┬──────────────┬───────┘
                             │               │              │
              ┌──────────────▼───┐  ┌────────▼───────┐  ┌───▼────────────┐
              │    Playlist      │  │  SlideLoader   │  │    Renderer    │
              │ shuffle, filters │  │ decode + mat   │  │  GLES3 / EGL   │
              └────────┬─────────┘  │ in a thread    │  └───┬────────────┘
                       │            └────────┬───────┘      │
              ┌────────▼─────────┐           │          ┌───▼────────────┐
              │  Library (SQLite │◄──────────┘          │    Backend     │
              │  WAL + FTS5)     │                      │  KMS │ headless│
              └────────▲─────────┘                      └───┬────────────┘
                       │                                    │
              ┌────────┴─────────┐                   ┌──────▼───────┐
              │ Scanner + inotify│                   │  DRM / GBM   │
              └──────────────────┘                   │  the screen  │
                                                     └──────────────┘
```

## Where the slideshow lives

`app.py` is the appliance: it builds everything, runs the render loop, keeps
the overlays and the power schedule, and answers for the whole frame in
`state()`. What it deliberately does *not* hold any more is the slideshow.

`slideshow.py` has one implementation of "show this picture", used by the next
and previous commands, by the jump from the web gallery, and by the first slide
at startup — and one `asyncio.Lock` around it. Preparing a slide takes a second
or two on a Pi and the coroutine yields while it happens; before the lock, a
key press in that window ran a second advance concurrently, drew from the
playlist twice and left the caption describing one picture while the screen
showed another.

`settings.py` is the other half of the same idea: one table mapping a changed
setting to its effect on the running frame, used both when a single setting
arrives from MQTT or the settings page and when the whole config file is
re-read. There used to be three partial implementations of that, and the
reload path applied about a tenth of what it then reported as applied.

## The display path

There is no display server. The process opens `/dev/dri/cardN`, becomes DRM
master, allocates buffers through GBM, and creates an EGL context on the GBM
platform. Each frame:

1. GLES draws into the GBM surface's back buffer
2. `eglSwapBuffers` hands the buffer to GBM
3. `gbm_surface_lock_front_buffer` takes the finished buffer
4. it is wrapped in a DRM framebuffer (cached per buffer object, so this
   happens two or three times in the life of the process, not per frame)
5. `drmModePageFlip` queues it for the next vblank and the loop waits for the
   flip event
6. the buffer that was previously on screen is released back to GBM

`src/picframe3/gfx/drm.py`, `gbm.py`, `egl.py` and `gl.py` are hand-written
`ctypes` bindings — roughly 30 GL entry points and the mode-setting subset of
libdrm. That is a deliberate trade: it removes PyOpenGL and moderngl (which
cannot create a context on the Pi's `v3d` driver at all, since it needs desktop
GL 3.3 and `v3d` offers 3.1 plus GLES 3.1), and it means nothing has to be
compiled on the Pi or rebuilt when Mesa is updated.

### Why not a Wayland client?

It would work, and the backend interface has room for one. But it requires a
compositor to be running, which on a dedicated frame is several hundred
megabytes and an extra copy of every frame, and it inherits the labwc/wlroots
issues that current Raspberry Pi OS still has with GPU resets and with
connectors whose state reads as "unknown". Owning the screen is both simpler
and more robust for an appliance.

## The render loop

The loop is adaptive, and this is the single biggest behavioural difference
from pi3d:

- **While something is moving** — a transition, a Ken Burns drift, a video, a
  caption fading — it renders at `display.fps_limit`. With `vsync` on, the page
  flip already blocks until the vblank, so the loop only sleeps out whatever is
  left of the frame budget; sleeping a whole frame *after* the flip would beat
  against the refresh rate and turn an even fade into an uneven one.
- **While a still picture is on screen** it renders *nothing at all*. On KMS the
  last flipped frame stays on the panel by itself, so the correct number of
  frames to draw is zero. The loop wakes a few times a second only to notice
  commands and the next slide becoming due.

pi3d redraws at its configured frame rate forever, which is why a picframe Pi
sits at a constant few percent of CPU and a warm GPU between pictures.

## Colour

Photographs are uploaded as `GL_SRGB8_ALPHA8`, so the GPU decodes them to linear
light on every sample. Transitions, mats and brightness all operate on linear
values, and the fragment shader encodes back to sRGB exactly once, on output.

This is not pedantry: a crossfade between a white frame and a black frame
blended in sRGB passes through RGB 128, which the eye reads as much darker than
half way. Blended in linear light and re-encoded it passes through about 188,
which is what "half way" actually looks like. `tests/test_render.py` asserts
this.

## Preparation happens on the CPU, once

A slide changes every few minutes. Compositing mats, blur-fills, portrait pairs
and letterboxing in Pillow at load time therefore costs nothing, and buys
LANCZOS resampling and a real Gaussian blur. The GPU is left with one job:
sample two textures and mix them.

The loader prepares the *next* slide in a worker thread while the current one is
still up, so the transition never waits on a JPEG decode. Pillow releases the
GIL for decode and resample, so threads suffice and the image data is not
copied between processes.

## Video

GStreamer decodes into RGBA frames, scaled and padded by `videoscale` to
exactly the panel size, pulled non-blockingly from an `appsink` on the render
loop and uploaded with `glTexSubImage2D` into the slide's existing texture.
Because the video *is* the slide texture, it crossfades in and out like any
photograph and the captions and clock composite over it unchanged.

The sink bin is constructed explicitly rather than inside a `parse_launch`
string, because `playbin3` does not expose its children until it reaches
`PAUSED` — a detail that costs an hour if you find it the hard way.

Video frames arrive top-down while GL textures are bottom-up. Rather than
flipping several megabytes per frame on the CPU, the slide's sampling transform
is inverted (`Slide.flip_v`).

Playback starts only once the crossfade has finished: the poster frame fades in
as a still picture and the film begins on a fully opaque screen, so nothing is
half-transparent while it moves and the opening second is not thrown away.

**The player ends a video slide, not the clock.** While a video runs the next
change is pushed half a second ahead on every tick, so it plays for its own
length however short the picture interval is; when the player stops — end of
stream, `video_max_seconds` reached, or the `video_loop` window used up — the
slide ends at once. The cap and the loop window are enforced inside
`VideoPlayer`, which pauses itself and leaves the last frame on screen to be
faded out, because only the player knows when it has really finished.

## Index

SQLite in WAL mode, so a background scan writing never blocks the render loop's
reads, and a power cut mid-scan cannot corrupt the file. Tags are normalised
into their own table; titles, captions, tags, paths and place names are indexed
in an FTS5 table. Play counts and last-played timestamps are persisted, which is
what lets the shuffle avoid repeats across reboots.

Geocoding is deliberately **not** part of indexing. A scan that made a
rate-limited network request per photograph would take three hours for ten
thousand files, and — because a later rescan skips files that have not changed
— it would also be the only chance a photograph ever got at a place name.
Instead the scan uses the cache only, the picture going on screen triggers a
single lookup if it needs one, and a background pass trickles through the rest
at the one request per second OpenStreetMap asks for.

`inotify` (bound with ctypes, watching each directory recursively) picks up new
photographs within seconds, which matters when the folder is fed by Syncthing or
a Nextcloud client. A periodic full walk remains as a backstop and takes over
automatically if the kernel's watch limit is exhausted.

## Control

Every input path produces the same `Command` and every observer sees the same
`State`. The renderer knows nothing about MQTT, HTTP knows nothing about the
playlist, and adding a control surface means writing one file that talks to the
bus. In `picframe` each interface reached into the viewer's internals, which is
why the viewer had to know what `display_power` mode the user was on.

## Getting photographs in

Two roads, and the frame owns neither of them. The Samba share is a block in
`smb.conf` the wizard writes between two markers, so it can be lifted back out
without a parser. Syncthing is another program with its own web interface, and
`picframe3.sync` is deliberately only the frame's side of it: which folder it
should keep, which way photographs travel, where its page listens, who it is
paired with — read and written over Syncthing's REST API on localhost, with
the API key out of its own config file, which is readable because Syncthing
runs as the same user as the frame.

The three things that need root — installing the package, running it at boot,
stopping it — are the interesting part, because the settings page offering
them is a page that is deliberately open on the LAN. They go through two fixed
oneshot units (`picframe3-syncthing-on@`, `picframe3-syncthing-off@`) that run
a helper script knowing only the words `on` and `off`, and a polkit rule
naming exactly those two units and `syncthing@` for one user. The frame's own
unit sets `NoNewPrivileges=yes`, so there is no other route: "the web
interface can install Syncthing" cannot widen into "the web interface can run
anything as root", because the units take no argument but the user name.

`manage-unit-files` is deliberately not granted — systemd exposes no unit name
to polkit for it, so allowing it would mean allowing *any* unit to be enabled
at boot. That is why enabling is inside the root helper rather than something
the frame does itself.

## Testing without a Pi

The `headless` backend creates a surfaceless EGL context and renders into a
framebuffer object, which any machine with Mesa can do — including CI and a
container. `tests/test_render.py` runs all fifteen transition shaders through
real GLES and reads the pixels back; `picframe3 demo` renders a contact sheet of
transitions, mats and overlays to a PNG. Every screenshot in this project was
produced that way.

## What CI does not cover

Worth knowing before trusting a green tick. CI runs the renderer against
llvmpipe, checks that `docs/CONFIG.md` and `config/picframe3.example.yaml`
still match the dataclasses, lints the shell, verifies the systemd unit with
`systemd-analyze`, and installs the wheel into a clean virtual environment to
prove every data file the frame opens at runtime is really inside it.

It does not, and largely cannot, cover:

- **`packaging/install.sh` actually running.** It is parsed and linted, never
  executed: it installs apt packages, creates users' groups and writes to
  `/etc`, and a runner is not a Pi. Everything it does on a fresh machine —
  the download path, the apt fallback that installs packages one at a time,
  the venv rebuild after a Python upgrade — is tested by hand.
- **The wizard's system changes.** Writing the unit, the udev rule, the polkit
  rules and the Samba block all need root and a real systemd, udev, polkit and
  Samba. The unit *text* is verified; the act of installing it is not.
- **Syncthing itself.** The folder object, the addresses, the device ids and
  the permission files are unit-tested; installing the package, enabling the
  service and a real pairing between two machines are done by hand. A mock of
  Syncthing's API would only ever test the mock.
- **`/boot/firmware/cmdline.txt`.** No runner has one. The single-line rule and
  the backup are enforced in code and reviewed by reading.
- **Real hardware.** No DRM master, no KMS page flip, no vsync, no DSI panel,
  no HDMI hotplug, no v3d driver. llvmpipe compiles the same shaders and
  produces the same pixels, but says nothing about frame pacing, tearing, or
  what the Pi's GPU does with a 4K texture.
- **GStreamer video playback.** PyGObject comes from apt through
  `--system-site-packages`, which is an arrangement no CI job reproduces.
- **MQTT against a real broker, and Home Assistant discovery.** The payloads
  are unit-tested; that Home Assistant makes the entities it should is not.
- **Anything that takes time.** Thermal throttling, undervoltage, an SD card
  filling up, a Wi-Fi link dropping at 3am and the watchdog mending it,
  inotify under a kernel watch limit — the failures a frame on a wall actually
  has are all long-running, and none of them fit in a CI job.
- **Upgrading.** Running the installer over an older install, and the OS
  upgrade that breaks a venv, are single-shot situations on a machine with
  history. CI always starts from nothing.
