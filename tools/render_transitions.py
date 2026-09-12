#!/usr/bin/env python3
"""Render every transition at several progress points into one contact sheet.

Run offscreen, so this works in CI or a container:
    python3 tools/render_transitions.py out.png
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw

from picframe3.gfx import Renderer, Slide, Texture, create_backend, transitions

W, H = 384, 216
STEPS = (0.0, 0.25, 0.5, 0.75, 1.0)


def swatch(colour, other, label):
    im = Image.new("RGB", (960, 540), colour)
    d = ImageDraw.Draw(im)
    for i in range(0, 960, 60):
        d.line([(i, 0), (i, 540)], fill=other, width=3)
    for j in range(0, 540, 60):
        d.line([(0, j), (960, j)], fill=other, width=3)
    d.ellipse([330, 120, 630, 420], fill=other)
    d.text((20, 20), label, fill=(255, 255, 255))
    return im


def main(out="transitions.png"):
    backend = create_backend("headless", width=W, height=H)
    backend.make_current()
    r = Renderer(backend, background=(0, 0, 0, 1))
    a = Texture.from_image(swatch((200, 70, 40), (250, 210, 120), "A"), srgb=True)
    b = Texture.from_image(swatch((30, 80, 150), (150, 220, 250), "B"), srgb=True)

    names = transitions.names()
    sheet = Image.new("RGB", (W * len(STEPS), H * len(names)), (20, 20, 20))
    draw = ImageDraw.Draw(sheet)
    for row, name in enumerate(names):
        for col, p in enumerate(STEPS):
            r.previous = Slide(a, owns_texture=False)
            r.current = Slide(b, owns_texture=False)
            r._active_transition = name
            r._t_dur = 1.0
            r._t0 = time.monotonic() - p
            r.draw()
            frame = Image.fromarray(backend.read_pixels(), "RGBA").convert("RGB")
            sheet.paste(frame, (col * W, row * H))
        draw.text((6, row * H + 6), name, fill=(255, 255, 0))
    r.previous = r.current = None
    sheet.save(out)
    print(f"wrote {out}  ({len(names)} transitions x {len(STEPS)} steps)")
    backend.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "transitions.png")
