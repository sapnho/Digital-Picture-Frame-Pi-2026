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
                         │  slideshow state machine, timing, power  │
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
  caption fading — it renders at `display.fps_limit`.
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

## Testing without a Pi

The `headless` backend creates a surfaceless EGL context and renders into a
framebuffer object, which any machine with Mesa can do — including CI and a
container. `tests/test_render.py` runs all fifteen transition shaders through
real GLES and reads the pixels back; `picframe3 demo` renders a contact sheet of
transitions, mats and overlays to a PNG. Every screenshot in this project was
produced that way.
