# The interface's icon

The mark is "Solid frame": a white picture frame on the interface's accent
red, `#ec3013` — the same red as the save button and the section kickers, so
the browser tab matches the page it opens. No corner radius, no gradient, no
shadow, which is the same handwriting as the rest of the web UI.

## The master

`src/picframe3/web/favicon.svg` is the master. Everything else is drawn from
its numbers; if the art changes, regenerate the rasters rather than editing
them.

Geometry on the 64-unit viewBox, stroke centred on the rectangle:

| | rect | stroke |
|---|---|---|
| `favicon.svg` | `x 13  y 15`, `38 × 34` | `6` |
| `icon-maskable.svg` | `x 20.1  y 21.3`, `23.6 × 21.1` | `3.72` |

The frame is a little wider than tall (11:10), which is what keeps it
reading as a picture frame rather than a square. The maskable variant is the
same mark inset to the 62 % safe zone with the red bleeding to the edge, so
an Android launcher may crop it to a circle, a squircle or anything else
without cutting into the frame.

Tokens: field `#ec3013`, aperture `#ffffff`, page ink `#201e1d`, ground
`#f3f2f2`.

## The files

All in `src/picframe3/web/`, served by name because `web/` is mounted at the
server root:

| File | For |
|---|---|
| `favicon.svg` | current browsers, and the master |
| `favicon.ico` | 16/32/48 px, for browsers that still ask for a `.ico` |
| `apple-touch-icon.png` | 180 px, iOS home screen (iOS rounds the corners itself) |
| `favicon-192x192.png`, `favicon-512x512.png` | Android home screen, referenced by the manifest |
| `icon-maskable-512x512.png`, `icon-maskable.svg` | Android, cropped to the launcher's shape |
| `manifest.webmanifest` | names the app, points at the icons, `display: standalone` |

`index.html` carries four `<link>` lines for these, all relative: the
interface is opened by IP, by hostname and by `.local` name alike.
`tools/check_package_data.py` lists every one of them, so a wheel that
forgets an icon fails the check instead of shipping a frame with a blank tab.

The two `theme-color` metas in `index.html` are *not* the icon's red on
purpose: they follow the page background per theme, which is what the
browser's own chrome should match.

## Regenerating the rasters

Any SVG rasteriser will do. From the repository root, with Pillow:

```python
from PIL import Image, ImageDraw

RED, WHITE = "#ec3013", "#ffffff"
PLAIN = dict(x=13, y=15, w=38, h=34, sw=6)
MASK = dict(x=20.1, y=21.3, w=23.6, h=21.1, sw=3.72)

def render(size, m, ss=8):
    """The two rectangles the SVG draws, supersampled then downsampled."""
    k, half = size * ss / 64, m["sw"] / 2
    im = Image.new("RGB", (size * ss, size * ss), RED)
    d = ImageDraw.Draw(im)
    d.rectangle([v * k for v in (m["x"] - half, m["y"] - half,
                                 m["x"] + m["w"] + half, m["y"] + m["h"] + half)], fill=WHITE)
    d.rectangle([v * k for v in (m["x"] + half, m["y"] + half,
                                 m["x"] + m["w"] - half, m["y"] + m["h"] - half)], fill=RED)
    return im.resize((size, size), Image.LANCZOS)

render(180, PLAIN).save("src/picframe3/web/apple-touch-icon.png", optimize=True)
render(192, PLAIN).save("src/picframe3/web/favicon-192x192.png", optimize=True)
render(512, PLAIN).save("src/picframe3/web/favicon-512x512.png", optimize=True)
render(512, MASK).save("src/picframe3/web/icon-maskable-512x512.png", optimize=True)
render(256, PLAIN).save("src/picframe3/web/favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
```

Supersampling by 8 and downsampling is what keeps the frame's edges clean at
16 px, where a direct draw would land them between pixels.
