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


@needs_gl
def test_transition_reaches_progress_one_on_screen(gl_backend):
    """A wipe must sweep all the way off, not stop one frame short.

    The transition clock used to run out before any frame had been drawn at
    progress 1.0, so the last strip of the outgoing picture stayed on screen
    until the next slide replaced it.
    """
    import time

    renderer = Renderer(gl_backend, background=(0, 0, 0, 1))
    white = Texture.from_image(Image.new("RGB", (160, 90), (255, 255, 255)), srgb=True)
    black = Texture.from_image(Image.new("RGB", (160, 90), (0, 0, 0)), srgb=True)

    renderer.show(Slide(white, owns_texture=False))
    renderer.draw()
    renderer.show(Slide(black, owns_texture=False), transition="wipe_left", duration=1.0)
    renderer._t0 = time.monotonic() - 1.5            # the clock has run out

    assert renderer.progress == 1.0
    assert renderer.in_transition, "the frame at progress 1.0 is still owed"

    renderer.draw()
    assert gl_backend.read_pixels()[..., 0].max() < 40, "the old picture is gone"
    assert not renderer.in_transition
    assert renderer.previous is None, "the outgoing slide is released in that frame"

    renderer.current = None
    renderer.close()
    white.close()
    black.close()


@needs_gl
def test_a_transition_that_will_not_compile_falls_back_to_fade(gl_backend, monkeypatch,
                                                               caplog):
    """And it is only compiled once, not sixty times a second for ever."""
    from picframe3.gfx import transitions as tr

    calls: list[str] = []
    real = tr.fragment_source

    def counting(name: str) -> str:
        calls.append(name)
        return real(name)

    monkeypatch.setitem(tr.TRANSITIONS, "_broken",
                        "vec4 pf_transition(vec2 uv, float p) { not glsl at all }")
    monkeypatch.setattr(tr, "fragment_source", counting)

    renderer = Renderer(gl_backend)
    fade = renderer._program("fade")
    assert renderer._program("_broken") is fade
    assert renderer._program("_broken") is fade
    assert calls.count("_broken") == 1, "the failure has to be cached too"
    assert "_broken" in renderer._failed_programs
    assert "fade" in caplog.text
    renderer.close()
    assert renderer._programs == {}


@needs_gl
def test_effects_sample_the_edge_texel_not_the_background(gl_backend):
    """blur, zoom and bump reach outside the screen rectangle on purpose.

    On an axis the picture covers completely that has to give the edge texel;
    returning the background there drew a dark seam along two edges.  On a
    letterboxed axis the background is genuinely what is there and must stay.
    """
    import time

    renderer = Renderer(gl_backend, background=(0, 0, 0, 1))
    # A square picture on a 16:9 screen: "cover" fills the width exactly, so
    # every blur tap past the left and right edge lands just outside it.
    tex = Texture.from_image(Image.new("RGB", (90, 90), (255, 255, 255)), srgb=True)

    def draw(fit: str):
        renderer.previous = Slide(tex, fit=fit, owns_texture=False)
        renderer.current = Slide(tex, fit=fit, owns_texture=False)
        renderer._active_transition = "blur"
        renderer._t_dur = 1.0
        renderer._t0 = time.monotonic() - 0.5        # widest blur radius
        renderer.draw()
        return gl_backend.read_pixels()

    covered = draw("cover")
    assert covered[45, 0, 0] > 240 and covered[45, 159, 0] > 240

    letterboxed = draw("contain")
    assert letterboxed[45, 0, 0] < 40 and letterboxed[45, 159, 0] < 40

    renderer.previous = renderer.current = None
    renderer.close()
    tex.close()


@needs_gl
def test_overlay_and_picture_dim_at_the_same_rate(gl_backend):
    """Brightness is a physical dimming and must happen in linear light.

    Applied to the sRGB value, as the overlay shader used to, white text fell
    to 128/255 at brightness 0.5 while a white photo pixel sat at 185/255 --
    captions visibly darkened ahead of the picture behind them.
    """
    renderer = Renderer(gl_backend, background=(0, 0, 0, 1))
    tex = Texture.from_image(Image.new("RGB", (160, 90), (255, 255, 255)), srgb=True)
    renderer.show(Slide(tex, owns_texture=False))
    renderer.set_overlay("patch", Image.new("RGBA", (40, 20), (255, 255, 255, 255)),
                         x=10, y=5)
    renderer.brightness = 0.5
    renderer.draw()

    px = gl_backend.read_pixels()
    overlay_white = int(px[10, 20, 0])
    picture_white = int(px[80, 140, 0])
    assert abs(overlay_white - picture_white) <= 4, (overlay_white, picture_white)
    assert overlay_white > 170

    renderer.current = None
    renderer.close()
    tex.close()


def test_kenburns_zoom_survives_a_nonsense_value():
    """A zoom of 0 in the config is a division by zero in the draw path."""
    class FakeTexture:
        width, height, aspect = 1920, 1080, 16 / 9

    slide = Slide.with_random_pan(FakeTexture(), duration=10.0, zoom=0.0)
    slide.started = 0.0
    for t in (0.0, 5.0, 10.0):
        assert slide.xform(16 / 9, now=t) == (1.0, 1.0, 0.0, 0.0)

    hand_built = Slide(FakeTexture(), kenburns=True, kb_zoom=0.0, duration=10.0)
    hand_built.started = 0.0
    assert hand_built.xform(16 / 9, now=10.0)[0] == 1.0


@needs_gl
def test_texture_from_a_greyscale_array(gl_backend):
    """A 2-D frame has no channel axis; indexing one is an IndexError."""
    grey = np.full((8, 8), 200, dtype=np.uint8)
    tex = Texture.from_array(grey)
    assert (tex.width, tex.height) == (8, 8)
    tex.close()


def test_a_flip_stays_pending_until_its_event_is_collected(monkeypatch):
    """No "flip pending" state meant a timed-out wait desynchronised the queue.

    The event stayed in the fd, so the *next* wait returned on it immediately
    and reported a flip that was still in the air.
    """
    import os

    from picframe3.gfx import drm

    dev = drm.DrmDevice.__new__(drm.DrmDevice)
    read_fd, write_fd = os.pipe()
    dev.fd = read_fd
    dev._flip_pending = False
    dev._flip_since = 0.0
    # Long enough that the give-up path below cannot interfere: this test is
    # about a flip that is still legitimately in the air.
    dev.flip_deadline = 30.0

    flips: list[int] = []

    class FakeLib:
        @staticmethod
        def drmModePageFlip(fd, crtc_id, fb_id, flags, data):
            flips.append(flags)
            return 0

        @staticmethod
        def drmHandleEvent(fd, ctx):
            os.read(fd, 1)          # what libdrm does: drain the event
            return 0

    monkeypatch.setattr(drm, "lib", FakeLib)
    try:
        # Nothing arrives: the flip has to stay pending and no completion may
        # be reported, or the caller would release a buffer still on screen.
        result = dev.page_flip(1, 2, timeout=0.01)
        assert result.queued and result.completions == 0
        assert dev.flip_pending

        # While one is in the air the next is not even attempted.
        result = dev.page_flip(1, 3, timeout=0.01)
        assert not result.queued and result.completions == 0
        assert flips == [drm.DRM_MODE_PAGE_FLIP_EVENT]

        # Once the event does arrive it is collected first, and only then is
        # the new buffer handed to the CRTC.
        os.write(write_fd, b"x")
        result = dev.page_flip(1, 3, timeout=0.01)
        assert result.queued and result.completions == 1
        assert len(flips) == 2

        # vsync:false asks for an async flip and does not wait for it.
        os.write(write_fd, b"x")
        result = dev.page_flip(1, 4, timeout=0.01, asynchronous=True, wait=False)
        assert result.queued and result.completions == 1
        assert flips[-1] == drm.DRM_MODE_PAGE_FLIP_EVENT | drm.DRM_MODE_PAGE_FLIP_ASYNC
        assert dev.flip_pending
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_the_front_buffer_is_released_only_on_a_confirmed_flip(monkeypatch):
    """Handing a buffer back while it is still being scanned out is tearing."""
    from picframe3.gfx import backend_kms

    released: list[int] = []

    class FakeGbm:
        @staticmethod
        def gbm_surface_release_buffer(surf, bo):
            released.append(bo)

    monkeypatch.setattr(backend_kms.gbm, "lib", FakeGbm)

    be = backend_kms.KmsBackend.__new__(backend_kms.KmsBackend)
    be._gbm_surf = None
    be._front_bo = None
    be._queued_bos = [101]
    be._flip_completed()
    assert be._front_bo == 101 and released == []      # nothing to replace yet

    be._queued_bos.append(102)                          # queued, not confirmed
    assert released == []
    be._flip_completed()
    assert be._front_bo == 102 and released == [101]

    be._queued_bos.clear()                              # nothing confirmed
    be._flip_completed()
    assert be._front_bo == 102 and released == [101]


def test_a_flip_that_never_completes_is_written_off_rather_than_freezing(monkeypatch):
    """The other half of the same bookkeeping.

    If a completion never arrives -- a DPMS change with a flip in the air, a
    hotplug, a driver hiccup -- holding "pending" for ever means the frame
    never flips again and the picture is frozen for good while the process
    goes on running.  After `flip_deadline` the flip is written off and the
    next frame is allowed to try.
    """
    import os
    import time

    from picframe3.gfx import drm

    dev = drm.DrmDevice.__new__(drm.DrmDevice)
    read_fd, write_fd = os.pipe()
    dev.fd = read_fd
    dev._flip_pending = False
    dev._flip_since = 0.0
    dev.flip_deadline = 0.05

    class FakeLib:
        @staticmethod
        def drmModePageFlip(fd, crtc_id, fb_id, flags, data):
            return 0

        @staticmethod
        def drmHandleEvent(fd, ctx):        # pragma: no cover - never reached
            os.read(fd, 1)
            return 0

    monkeypatch.setattr(drm, "lib", FakeLib)
    try:
        assert dev.page_flip(1, 2, timeout=0.01).queued
        assert dev.flip_pending
        time.sleep(0.06)
        # Past the deadline: the frame gets to flip again.
        assert dev.page_flip(1, 3, timeout=0.01).queued
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_the_graphics_package_imports_on_a_machine_with_no_mesa():
    """Opening the library happens on the first call, never on import.

    `picframe3 doctor` exists to say that EGL or GLES is missing -- and it
    cannot say anything at all if importing the graphics package is what
    fails.  The same goes for `picframe3 transitions`, `--version`, the
    settings page, the overlay layout tests and the packaging job in CI, which
    installs the wheel into a bare container and never draws a frame.
    """
    import subprocess
    import sys
    import textwrap

    program = textwrap.dedent("""
        import ctypes, ctypes.util
        # As close as this gets to a machine with no graphics packages.
        ctypes.util.find_library = lambda name: None
        real = ctypes.CDLL

        def blocked(name, *args, **kwargs):
            if name and any(x in str(name) for x in ("EGL", "GLES", "gbm", "drm")):
                raise OSError(f"no such file: {name}")
            return real(name, *args, **kwargs)

        ctypes.CDLL = blocked

        import picframe3.gfx                    # noqa: F401
        import picframe3.gfx.overlays           # noqa: F401
        from picframe3.gfx import transitions
        assert transitions.resolve_pool([]), "no transitions without a GPU?"

        import picframe3.cli, sys
        sys.argv = ["picframe3", "transitions"]
        raise SystemExit(picframe3.cli.main())
    """)
    result = subprocess.run([sys.executable, "-c", program],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "fade" in result.stdout


def test_waking_the_panel_asks_for_a_modeset_not_just_dpms(monkeypatch):
    """Switching off is DPMS; switching on is DPMS *and* a fresh modeset.

    With the connector powered down the HDMI link is gone -- the television
    says "no cable", not "no signal" -- and the DPMS property going back on is
    not reliably enough to make the set re-acquire it.  The next frame has to
    re-run drmModeSetCrtc, which is exactly what lit the screen at startup.
    """
    from picframe3.gfx import backend_kms

    calls: list[bool] = []

    class FakeDrm:
        def set_dpms(self, connector_id, on):
            calls.append(on)
            return True

    be = backend_kms.KmsBackend.__new__(backend_kms.KmsBackend)
    be._drm = FakeDrm()
    be.output = type("O", (), {"connector_id": 32, "crtc_id": 1})()
    be._powered = True
    be._modeset_done = True

    assert be.set_power(False) is True
    assert calls == [False] and be._powered is False
    assert be._modeset_done is True          # nothing to re-drive while dark

    assert be.set_power(True) is True
    assert calls == [False, True] and be._powered is True
    assert be._modeset_done is False         # the next frame lights it up again


def test_a_connector_without_dpms_still_gets_its_frames(monkeypatch):
    """The honest "no" must not also stop the picture.

    `set_power` returning False is how the app knows to fall back to the
    backlight or to the shader.  If the backend had marked itself unpowered on
    the way out, that fallback would draw a black frame that is never
    presented -- and the last picture would stay on the wall for ever.
    """
    from picframe3.gfx import backend_kms

    class FakeDrm:
        def set_dpms(self, connector_id, on):
            return False                      # no DPMS property on this connector

    be = backend_kms.KmsBackend.__new__(backend_kms.KmsBackend)
    be._drm = FakeDrm()
    be.output = type("O", (), {"connector_id": 32, "crtc_id": 1})()
    be._powered = True
    be._modeset_done = True

    assert be.set_power(False) is False
    assert be._powered is True


def test_nothing_is_presented_while_the_panel_is_off(monkeypatch):
    """A disabled CRTC refuses every flip, so the frame is dropped early.

    The buffer goes straight back to GBM -- the pool holds three or four -- and
    the kernel is never asked, which is what kept an off-period from filling
    the journal with one refusal per frame.
    """
    from picframe3.gfx import backend_kms

    released: list[int] = []
    touched: list[str] = []

    class FakeGbm:
        @staticmethod
        def gbm_surface_lock_front_buffer(surf):
            return 4242

        @staticmethod
        def gbm_surface_release_buffer(surf, bo):
            released.append(bo)

    class FakeDrm:
        def __getattr__(self, name):          # pragma: no cover - must not run
            touched.append(name)
            raise AssertionError("the DRM device was used with the panel off")

    monkeypatch.setattr(backend_kms.gbm, "lib", FakeGbm)
    monkeypatch.setattr(backend_kms.egl, "swap_buffers", lambda dpy, surf: None)

    be = backend_kms.KmsBackend.__new__(backend_kms.KmsBackend)
    be._drm = FakeDrm()
    be._gbm_surf = None
    be._dpy = be._surf = None
    be._powered = False
    be._modeset_done = True
    be._queued_bos = []
    be._front_bo = None
    be._fb_cache = {}

    be.end_frame()

    assert released == [4242] and touched == []
    assert be._queued_bos == [] and be._fb_cache == {}


def test_a_flip_refused_by_a_disabled_crtc_is_a_dropped_frame(monkeypatch):
    """EINVAL from a CRTC that has just been switched off is not a fault.

    The display can go dark between drawing a frame and presenting it -- the
    off-schedule, a button, Home Assistant.  Raising there cost the render loop
    a traceback per frame for the whole of the off-period.
    """
    import os

    from picframe3.gfx import drm

    dev = drm.DrmDevice.__new__(drm.DrmDevice)
    read_fd, write_fd = os.pipe()
    dev.fd = read_fd
    dev._flip_pending = False
    dev._flip_since = 0.0
    dev.flip_deadline = 1.0

    class FakeLib:
        @staticmethod
        def drmModePageFlip(fd, crtc_id, fb_id, flags, data):
            return -22                        # EINVAL: the CRTC is not active

    monkeypatch.setattr(drm, "lib", FakeLib)
    try:
        result = dev.page_flip(1, 2, timeout=0.01)
        assert not result.queued and result.completions == 0
        assert not dev.flip_pending
    finally:
        os.close(read_fd)
        os.close(write_fd)
