"""Render representative frames offscreen.

Everything the frame will put on a screen -- preparation, mats, transitions,
captions, clock -- goes through the same code here, but into a PNG instead of a
display.  It is how the renderer is checked without hardware, and it lets you
see what a setting does before carrying the Pi to the wall.
"""

from __future__ import annotations

import logging
import os
import time

from PIL import Image, ImageDraw

from .config import Config
from .gfx import Renderer, Slide, Texture, create_backend
from .gfx import overlays as overlay_builders
from .gfx.textstyle import TextStyle
from .media import PrepareOptions, prepare_image, read_metadata
from .media.mat import MatStyle

_log = logging.getLogger(__name__)

STEPS = (0.0, 0.35, 0.65, 1.0)


def _synthetic(index: int, size: tuple[int, int]) -> Image.Image:
    """A stand-in photograph, so the demo works with an empty library."""
    import colorsys

    w, h = size
    hue = (index * 0.17) % 1.0
    base = tuple(int(c * 255) for c in colorsys.hls_to_rgb(hue, 0.45, 0.55))
    sky = tuple(int(c * 255) for c in colorsys.hls_to_rgb((hue + 0.08) % 1, 0.72, 0.45))
    img = Image.new("RGB", (w, h), sky)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(h - 1, 1)
        draw.line([(0, y), (w, y)],
                  fill=tuple(int(sky[i] * (1 - t) + base[i] * t) for i in range(3)))
    draw.ellipse([w * 0.62, h * 0.10, w * 0.82, h * 0.30],
                 fill=(255, 246, 214))
    ridge = [(0, h * 0.72)]
    for i in range(1, 9):
        ridge.append((w * i / 8, h * (0.52 + 0.22 * ((i * 7) % 5) / 5)))
    ridge.append((w, h))
    ridge.append((0, h))
    draw.polygon(ridge, fill=tuple(max(0, c - 60) for c in base))
    return img


def _collect(folder: str | None, count: int, size: tuple[int, int]) -> list:
    if folder:
        paths: list[str] = []
        for dirpath, _dirs, files in os.walk(os.path.expanduser(folder)):
            for name in sorted(files):
                path = os.path.join(dirpath, name)
                from .media.metadata import is_supported, is_video

                if is_supported(path) and not is_video(path):
                    paths.append(path)
                if len(paths) >= count:
                    break
            if len(paths) >= count:
                break
        if paths:
            return [read_metadata(p) for p in paths]
        _log.warning("no usable pictures in %s; using generated images", folder)
    return []


def render_demo(
    output: str,
    folder: str | None = None,
    *,
    width: int = 960,
    height: int = 540,
    transition: str = "fade",
    config: Config | None = None,
) -> str:
    cfg = config or Config()
    backend = create_backend("headless", width=width, height=height)
    backend.make_current()
    renderer = Renderer(backend, background=tuple(cfg.display.background),
                        transition=transition, transition_time=1.0)

    metas = _collect(folder, 3, (width, height))
    options = PrepareOptions(
        fit="mat",
        mat_style=MatStyle(style="double", tolerance=0.0),
        background=(0, 0, 0),
    )

    prepared: list[Image.Image] = []
    labels: list[str] = []
    for i in range(3):
        if i < len(metas) and metas[i] is not None:
            image = prepare_image([metas[i]], (width, height), options)
            labels.append(os.path.basename(metas[i].path))
        else:
            image = None
        if image is None:
            source = _synthetic(i, (1400, 1000) if i % 2 else (900, 1200))
            image = prepare_image([_fake_meta(source)], (width, height), options,
                                  images=[source])
            labels.append(f"sample {i + 1}")
        prepared.append(image)

    textures = [Texture.from_image(im, srgb=True) for im in prepared]
    sheet = Image.new("RGB", (width * len(STEPS), height * 2), (10, 10, 12))

    # Row 1: a transition between the first two pictures.
    renderer.previous = Slide(textures[0], owns_texture=False)
    renderer.current = Slide(textures[1], owns_texture=False)
    renderer._active_transition = transition
    renderer._t_dur = 1.0
    for col, p in enumerate(STEPS):
        renderer._t0 = time.monotonic() - p
        renderer.draw()
        sheet.paste(_frame(backend), (col * width, 0))

    # Row 2: the third picture with the overlays a running frame would show.
    renderer.previous = None
    renderer.current = Slide(textures[2], owns_texture=False)
    renderer._t_dur = 0.0
    style = TextStyle(font_path=cfg.viewer.font, size=max(14, height // 22))
    info = overlay_builders.info_bar(
        ["Ordesa y Monte Perdido", "12 September 2026", "Huesca, Spain"],
        (width, height), style, scrim_opacity=0.55)
    if info:
        renderer.set_overlay("info", info.image, x=info.x, y=info.y, alpha=1.0, z=10)
    clock = overlay_builders.clock(
        (width, height), TextStyle(font_path=cfg.viewer.font,
                                   size=max(28, height // 7), justify="R"),
        fmt="%H:%M", position="TR")
    if clock:
        renderer.set_overlay("clock", clock.image, x=clock.x, y=clock.y, alpha=1.0, z=20)
    for col, brightness in enumerate((1.0, 0.75, 0.5, 0.25)):
        renderer.brightness = brightness
        renderer.draw()
        sheet.paste(_frame(backend), (col * width, height))

    draw = ImageDraw.Draw(sheet)
    draw.text((8, 6), f"transition: {transition}", fill=(255, 220, 90))
    draw.text((8, height + 6), "overlays + brightness 100/75/50/25%", fill=(255, 220, 90))

    renderer.previous = renderer.current = None
    renderer.close()
    for tex in textures:
        tex.close()
    backend.close()

    output = os.path.abspath(os.path.expanduser(output))
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    sheet.save(output)
    return output


def _frame(backend) -> Image.Image:
    return Image.fromarray(backend.read_pixels(), "RGBA").convert("RGB")


def _fake_meta(image: Image.Image):
    from .media.metadata import PhotoMeta

    meta = PhotoMeta(path="<generated>")
    meta.width, meta.height = image.size
    return meta
