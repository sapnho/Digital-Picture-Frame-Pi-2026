"""Renderer tests.  These drive real EGL/GLES through the offscreen backend."""

import numpy as np
import pytest
from conftest import needs_gl
from PIL import Image

from picframe3.gfx import Renderer, Slide, Texture, create_backend, transitions


@pytest.fixture
def gl_backend():
    backend = create_backend("headless", width=160, height=90)
    backend.make_current()
    yield backend
    backend.close()


@needs_gl
def test_clear_colour_reaches_the_framebuffer(gl_backend):
    renderer = Renderer(gl_backend, background=(1.0, 0.0, 0.0, 1.0))
    renderer.draw()
    px = gl_backend.read_pixels()
    assert px[45, 80, 0] > 240 and px[45, 80, 1] < 15
    renderer.close()


@needs_gl
@pytest.mark.parametrize("name", transitions.names())
def test_every_transition_compiles_and_draws(gl_backend, name):
    renderer = Renderer(gl_backend)
    a = Texture.from_image(Image.new("RGB", (160, 90), (255, 0, 0)), srgb=True)
    b = Texture.from_image(Image.new("RGB", (160, 90), (0, 0, 255)), srgb=True)
    renderer.show(Slide(a, owns_texture=False))
    renderer.show(Slide(b, owns_texture=False), transition=name, duration=1.0)
    renderer.draw()
    assert gl_backend.read_pixels().shape == (90, 160, 4)
    renderer.previous = renderer.current = None
    renderer.close()
    a.close()
    b.close()


@needs_gl
def test_fade_midpoint_is_between_the_two_slides(gl_backend):
    import time

    renderer = Renderer(gl_backend)
    a = Texture.from_image(Image.new("RGB", (160, 90), (255, 255, 255)), srgb=True)
    b = Texture.from_image(Image.new("RGB", (160, 90), (0, 0, 0)), srgb=True)
    renderer.previous = Slide(a, owns_texture=False)
    renderer.current = Slide(b, owns_texture=False)
    renderer._active_transition = "fade"
    renderer._t_dur = 1.0
    renderer._t0 = time.monotonic() - 0.5
    renderer.draw()
    mid = int(gl_backend.read_pixels()[45, 80, 0])
    # Blending happens in linear light, so mid-grey lands well above 128 once
    # re-encoded to sRGB.  A naive sRGB-space blend would give ~128.
    assert 170 < mid < 210, mid
    renderer.previous = renderer.current = None
    renderer.close()
    a.close()
    b.close()


@needs_gl
def test_overlay_lands_where_it_is_placed(gl_backend):
    renderer = Renderer(gl_backend, background=(0, 0, 0, 1))
    patch = Image.new("RGBA", (40, 20), (0, 255, 0, 255))
    renderer.set_overlay("test", patch, x=10, y=5)
    renderer.draw()
    px = gl_backend.read_pixels()
    assert px[10, 20, 1] > 200, "overlay should be opaque green near its top-left"
    assert px[80, 20, 1] < 40, "and absent well below it"
    renderer.close()


def test_slide_transform_cover_and_contain():
    class FakeTexture:
        width, height, aspect = 2000, 1000, 2.0

    slide = Slide(FakeTexture(), fit="cover")          # image wider than 16:9
    sx, sy, _, _ = slide.xform(16 / 9)
    assert sx < 1.0 and sy == 1.0                       # crop horizontally

    slide = Slide(FakeTexture(), fit="contain")
    sx, sy, _, _ = slide.xform(16 / 9)
    assert sx == 1.0 and sy > 1.0                       # letterbox vertically


def test_kenburns_pan_stays_inside_the_image():
    class FakeTexture:
        width, height, aspect = 1920, 1080, 16 / 9

    slide = Slide.with_random_pan(FakeTexture(), duration=10.0, zoom=1.3)
    for t in (0.0, 0.5, 1.0):
        slide.started = 0.0
        sx, sy, ox, oy = slide.xform(16 / 9, now=t * 10.0)
        assert abs(ox) <= (1 - sx) / 2 + 1e-6
        assert abs(oy) <= (1 - sy) / 2 + 1e-6


def test_flip_v_inverts_the_sampling():
    class FakeTexture:
        width, height, aspect = 1920, 1080, 16 / 9

    normal = Slide(FakeTexture()).xform(16 / 9)
    flipped = Slide(FakeTexture(), flip_v=True).xform(16 / 9)
    assert flipped[1] == -normal[1]


@needs_gl
def test_half_turn_rotates_the_picture(gl_backend):
    """A 180° mount must flip the image, not just the overlays."""

    top_half = np.zeros((90, 160, 3), dtype=np.uint8)
    top_half[:45] = (255, 255, 255)
    source = Image.fromarray(top_half, "RGB")

    renderer = Renderer(gl_backend, background=(0, 0, 0, 1))
    tex = Texture.from_image(source, srgb=True)
    renderer.show(Slide(tex, owns_texture=False))
    renderer.draw()
    upright = gl_backend.read_pixels()

    renderer.rotate = 180
    renderer.draw()
    turned = gl_backend.read_pixels()

    assert upright[10, 80, 0] > 200 and upright[80, 80, 0] < 50
    assert turned[10, 80, 0] < 50 and turned[80, 80, 0] > 200
    renderer.current = None
    renderer.close()
    tex.close()


@needs_gl
def test_rotate_rejects_quarter_turns(gl_backend, caplog):
    renderer = Renderer(gl_backend)
    renderer.rotate = 90
    assert renderer.rotate == 0
    assert "kernel" in caplog.text
    renderer.close()


@pytest.mark.parametrize("version,expected", [
    ("OpenGL ES 3.1 Mesa 23.2.1-1~bpo12+rpt3", (3, 1)),   # Raspberry Pi OS, Pi 4/5
    ("OpenGL ES 3.2 Mesa 25.2.8", (3, 2)),
    ("OpenGL ES 2.0 Mesa 20.3.5", (2, 0)),                # VideoCore IV, Pi 2/3
    ("OpenGL ES-CM 1.1", (1, 1)),
    ("nonsense", None),
    ("", None),
])
def test_parses_the_gl_version_string(version, expected):
    from picframe3.gfx import gl

    assert gl.parse_es_version(version) == expected


def test_es_version_gate():
    """A Pi 3 must be told plainly, not left to fail at the first shader."""
    from picframe3.gfx import gl

    assert gl.es_version_ok("OpenGL ES 3.1 Mesa 23.2.1")
    assert not gl.es_version_ok("OpenGL ES 2.0 Mesa 20.3.5")
    assert not gl.es_version_ok("")


def test_random_transitions_come_only_from_the_chosen_pool():
    """Untick the ones you dislike and they never come up again."""
    from picframe3.gfx import transitions

    assert transitions.resolve_pool(["fade", "zoom"]) == ("fade", "zoom")
    assert transitions.resolve_pool([]) == transitions.RANDOM_POOL
    assert transitions.resolve_pool(None) == transitions.RANDOM_POOL
    # A renamed or mistyped transition must not stop the frame starting.
    assert transitions.resolve_pool(["fade", "not_a_transition"]) == ("fade",)
    assert transitions.resolve_pool(["not_a_transition"]) == transitions.RANDOM_POOL
    assert set(transitions.RANDOM_POOL) <= set(transitions.TRANSITIONS)
