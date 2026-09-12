"""The slide renderer.

Draws at most two textured full-screen quads per frame through one transition
shader, then composites any overlay layers.  That is the whole scene: a picture
frame does not need a scene graph, and pi3d's generic 3D machinery was most of
what made it hard to keep running on current hardware.
"""

from __future__ import annotations

import ctypes
import logging
import math
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import gl, transitions
from .backend import Backend
from .program import Program, ShaderError
from .texture import Texture

_log = logging.getLogger(__name__)

OVERLAY_VERT = """#version 300 es
// Attribute 0 is what the renderer's single VAO feeds; binding it explicitly
// keeps a driver from choosing another slot and drawing nothing at all.
layout(location = 0) in vec2 a_pos;
uniform vec4 u_rect;       // x, y, w, h in normalised device coordinates
out vec2 v_uv;
void main() {
    // Overlay textures come from Texture.from_image, which already flips the
    // PIL image into GL's bottom-up orientation, so a_pos maps to uv directly.
    v_uv = a_pos;
    gl_Position = vec4(u_rect.xy + a_pos * u_rect.zw, 0.0, 1.0);
}
"""

OVERLAY_FRAG = """#version 300 es
precision mediump float;
in vec2 v_uv;
out vec4 fragColor;
uniform sampler2D u_tex;
uniform float u_alpha;
uniform float u_brightness;

// Why this shader converts and the transition shader does not: overlay
// textures are uploaded as plain RGBA8, so sampling returns the sRGB-encoded
// value unchanged, and the result is written straight to the framebuffer.
// Photographs are uploaded as SRGB8_ALPHA8, so there the GPU decodes on the
// way in and the transition shader only has to re-encode once at the end.
// Multiplying u_brightness into an sRGB value dims far too fast: at 0.5 a
// white photo pixel ends up at 185/255 while white text ended up at 128/255,
// so captions visibly darkened ahead of the picture behind them.  Dimming is a
// physical operation and belongs in linear light for both.
vec3 pf_srgb_to_linear(vec3 c) {
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c));
}

vec3 pf_linear_to_srgb(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));
}

void main() {
    vec4 c = texture(u_tex, v_uv);
    vec3 lit = pf_srgb_to_linear(c.rgb) * u_brightness;
    fragColor = vec4(pf_linear_to_srgb(lit), c.a * u_alpha);
}
"""


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _sane_zoom(zoom) -> float:
    """A Ken Burns zoom factor that cannot divide by zero.  1.0 means no zoom."""
    try:
        value = float(zoom)
    except (TypeError, ValueError):
        return 1.0
    return value if value >= 1.0 else 1.0


@dataclass
class Slide:
    """One picture on screen, plus how it should move while it is there."""

    texture: Texture
    fit: str = "cover"                     # "cover" crops, "contain" letterboxes
    kenburns: bool = False
    kb_zoom: float = 1.12
    kb_from: tuple[float, float] = (0.0, 0.0)
    kb_to: tuple[float, float] = (0.0, 0.0)
    duration: float = 30.0
    started: float = field(default_factory=time.monotonic)
    meta: object = None
    #: When False the renderer will not free ``texture`` on retirement.  Used
    #: for shared textures such as the "no pictures" placeholder.
    owns_texture: bool = True
    #: Video frames arrive top-down while GL textures are bottom-up.  Rather
    #: than flipping every frame on the CPU, the sampling transform is
    #: inverted.
    flip_v: bool = False

    @classmethod
    def with_random_pan(cls, texture: Texture, duration: float, zoom: float = 1.12,
                        **kw) -> Slide:
        """Ken Burns with a pan direction chosen so the move stays on-image."""
        # A zoom of 0 out of a mistyped configuration file is not a slow pan,
        # it is a ZeroDivisionError in the draw path; anything below 1.0 would
        # shrink the picture instead of filling the frame, so both mean "off".
        zoom = _sane_zoom(zoom)
        span = (1.0 - 1.0 / zoom) * 0.5
        angle = random.uniform(0, 2 * math.pi)
        vec = (math.cos(angle) * span, math.sin(angle) * span)
        return cls(texture, kenburns=True, kb_zoom=zoom,
                   kb_from=(-vec[0], -vec[1]), kb_to=vec, duration=duration, **kw)

    def xform(self, display_aspect: float, now: float | None = None) -> tuple[float, float, float, float]:
        """uv scale/offset that maps screen space to image space."""
        r = self.texture.aspect / display_aspect if display_aspect else 1.0
        if self.fit == "contain":
            sx, sy = max(1.0, 1.0 / r), max(1.0, r)
        else:
            sx, sy = min(1.0, 1.0 / r), min(1.0, r)
        ox = oy = 0.0
        if self.kenburns and self.duration > 0:
            t = (now or time.monotonic()) - self.started
            t = min(max(t / self.duration, 0.0), 1.0)
            t = t * t * (3.0 - 2.0 * t)          # ease so the drift never jerks
            # A slide built by hand (or from an unvalidated config) can carry
            # any kb_zoom at all, and a value below 1.0 makes this divide by
            # zero part way through the slide rather than at the first frame --
            # the worst kind of crash to diagnose.
            k = 1.0 / (1.0 + (_sane_zoom(self.kb_zoom) - 1.0) * t)
            sx *= k
            sy *= k
            ox = self.kb_from[0] + (self.kb_to[0] - self.kb_from[0]) * t
            oy = self.kb_from[1] + (self.kb_to[1] - self.kb_from[1]) * t
            # never pan past the edge of the image
            lim_x = max(0.0, (1.0 - sx) * 0.5)
            lim_y = max(0.0, (1.0 - sy) * 0.5)
            ox = min(max(ox, -lim_x), lim_x)
            oy = min(max(oy, -lim_y), lim_y)
        if self.flip_v:
            sy = -sy
        return sx, sy, ox, oy


@dataclass
class Overlay:
    """A screen-space RGBA layer: info text, clock, menu, user PNG."""

    texture: Texture
    x: int = 0
    y: int = 0
    alpha: float = 1.0
    visible: bool = True
    z: int = 0

    def close(self) -> None:
        self.texture.close()


class Renderer:
    def __init__(self, backend: Backend, *, background=(0.0, 0.0, 0.0, 1.0),
                 transition: str = "fade", transition_time: float = 2.0):
        self.backend = backend
        self.background = tuple(background)
        self.transition_name = transition
        self.transition_time = transition_time
        #: What ``transition: random`` draws from.  None = the standard pool.
        self.transition_pool: tuple[str, ...] | None = None
        self.brightness = 1.0
        self._rotate = 0

        self._programs: dict[str, Program] = {}
        #: Transitions whose shader does not compile on this GPU.  A failure is
        #: deterministic, so remembering it turns "a full compile every frame,
        #: for ever" into one compile and one log line.
        self._failed_programs: set[str] = set()
        self._overlay_prog = Program(OVERLAY_VERT, OVERLAY_FRAG, "overlay")
        self._quad = self._make_quad()
        self._blank = Texture.solid((0, 0, 0, 255))

        self.current: Slide | None = None
        self.previous: Slide | None = None
        self._t0 = 0.0
        self._t_dur = 0.0
        #: A transition is only finished once a frame has been *drawn* at
        #: progress 1.0.  Without this the clock ran out first, the frame loop
        #: stopped, and e.g. a wipe left the last strip of the previous picture
        #: standing on screen until the next slide came round.
        self._final_frame_pending = False
        self._active_transition = transition
        self.overlays: dict[str, Overlay] = {}

        gl.glDisable(gl.DEPTH_TEST)
        gl.glDisable(gl.CULL_FACE)
        gl.glDisable(gl.DITHER)
        #: Per-frame glGetError costs a pipeline stall, so it is opt-in; the
        #: one-off check below is free and catches a backend or renderer that
        #: came up broken.  Set PICFRAME3_GL_DEBUG=1 to check every frame.
        self._gl_debug = bool(os.environ.get("PICFRAME3_GL_DEBUG"))
        self._check_gl("renderer setup")
        _log.info("renderer ready: %s", backend.describe())

    @staticmethod
    def _check_gl(context: str) -> None:
        """Report a GL error instead of letting it stay invisible.

        Logged rather than raised: a driver quirk on one frame is not worth
        taking the picture frame down for, but it must not go unmentioned
        either -- an unread error flag is how a black screen stays a mystery.
        """
        try:
            gl.check_error(context)
        except gl.GLError as exc:
            _log.warning("%s", exc)

    @property
    def rotate(self) -> int:
        return self._rotate

    @rotate.setter
    def rotate(self, degrees: int) -> None:
        degrees = int(degrees) % 360
        if degrees not in (0, 180):
            _log.warning(
                "display.rotate=%s is not supported in the renderer; rotate the "
                "output in the kernel instead (video=<connector>:<mode>,rotate=90 "
                "in cmdline.txt) so every layer works in real pixels",
                degrees,
            )
            degrees = 0
        self._rotate = degrees

    # -- geometry ----------------------------------------------------------
    def _make_quad(self) -> int:
        verts = gl.floats([0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 1.0])
        vao = gl.gen(gl.glGenVertexArrays)
        gl.glBindVertexArray(vao)
        vbo = gl.gen(gl.glGenBuffers)
        gl.glBindBuffer(gl.ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.ARRAY_BUFFER, ctypes.sizeof(verts), ctypes.byref(verts), gl.STATIC_DRAW)
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 2, gl.FLOAT, gl.FALSE, 0, None)
        gl.glBindVertexArray(0)
        self._vbo = vbo
        # The slide pass wants a -1..1 quad; reuse the same buffer by letting
        # the vertex shader map it, so only one VAO exists.
        return vao

    def _program(self, name: str) -> Program:
        prog = self._programs.get(name)
        if prog is not None:
            return prog
        if name in self._failed_programs:
            if name == "fade":
                # The fallback itself is what failed, so there is nothing left
                # to fall back to; recursing here would only hang the frame.
                raise ShaderError("the fade shader does not build on this GPU")
            return self._program("fade")
        try:
            prog = Program(transitions.VERTEX_SHADER_UNIT_QUAD,
                           transitions.fragment_source(name), f"slide[{name}]")
        except Exception as exc:
            self._failed_programs.add(name)
            if name == "fade":
                # Without the plain crossfade there is nothing left to fall
                # back to, and drawing no picture at all is not a recovery.
                raise
            _log.warning("transition %r does not build on this GPU; using fade "
                         "instead: %s", name, exc)
            return self._program("fade")
        self._programs[name] = prog
        _log.debug("compiled transition shader %s", name)
        return prog

    # -- slides ------------------------------------------------------------
    def show(self, slide: Slide, *, transition: str | None = None,
             duration: float | None = None) -> None:
        """Start a transition from whatever is on screen to ``slide``."""
        name = transition or self.transition_name
        if name == "random":
            name = random.choice(self.transition_pool or transitions.RANDOM_POOL)
        if name not in transitions.TRANSITIONS:
            _log.warning("unknown transition %r; using fade", name)
            name = "fade"
        if self.previous is not None and self.previous is not self.current:
            stale, self.previous = self.previous, None
            self._retire(stale)
        self.previous = self.current
        self.current = slide
        slide.started = time.monotonic()
        self._active_transition = name
        self._t_dur = self.transition_time if duration is None else duration
        self._t0 = time.monotonic()
        self._final_frame_pending = True
        if self.previous is None:
            self._t_dur = min(self._t_dur, 0.001)

    def _retire(self, slide: Slide | None) -> None:
        if slide is None or slide.texture is self._blank:
            return
        if not slide.owns_texture:
            return
        if self.current is slide or self.previous is slide:
            return
        slide.texture.close()

    @property
    def in_transition(self) -> bool:
        # True for one frame longer than the clock: the frame at progress 1.0
        # still has to be drawn, and it is the only one that shows the incoming
        # picture complete.
        return self._final_frame_pending or (time.monotonic() - self._t0) < self._t_dur

    @property
    def progress(self) -> float:
        if self._t_dur <= 0:
            return 1.0
        return min(1.0, (time.monotonic() - self._t0) / self._t_dur)

    # -- overlays ----------------------------------------------------------
    def set_overlay(self, name: str, image, *, x: int = 0, y: int = 0,
                    alpha: float = 1.0, z: int = 0) -> None:
        """Replace a named overlay layer.  ``image=None`` removes it."""
        old = self.overlays.pop(name, None)
        if old is not None:
            old.close()
        if image is None:
            return
        if self._rotate == 180:
            image = image.rotate(180)
            x = self.backend.width - x - image.width
            y = self.backend.height - y - image.height
        tex = Texture.from_image(image, srgb=False, mipmap=False, anisotropy=1.0)
        self.overlays[name] = Overlay(tex, x=x, y=y, alpha=alpha, z=z)

    def overlay_alpha(self, name: str, alpha: float) -> None:
        ov = self.overlays.get(name)
        if ov is not None:
            ov.alpha = max(0.0, min(1.0, alpha))

    def has_overlay(self, name: str) -> bool:
        return name in self.overlays

    # -- drawing -----------------------------------------------------------
    def draw(self, after_draw: Callable[[], None] | None = None) -> None:
        """Draw one frame.

        ``after_draw`` runs once the scene is complete but before the frame is
        presented — the only moment at which the pixels about to appear can be
        read back.
        """
        b = self.backend
        b.begin_frame()
        bg = [_srgb_to_linear(c) for c in self.background[:3]]
        gl.glClearColor(*bg, 1.0)
        gl.glClear(gl.COLOR_BUFFER_BIT)

        now = time.monotonic()
        if self.current is not None:
            self._draw_slide(now)
        else:
            # Nothing to draw means nothing can still be owed a final frame,
            # and leaving the flag set would keep the loop awake for ever.
            self._final_frame_pending = False
        self._draw_overlays()
        if after_draw is not None:
            try:
                after_draw()
            except Exception:  # pragma: no cover - caller's problem, not ours
                _log.exception("after_draw hook failed")
        if self._gl_debug:
            self._check_gl("draw")
        b.end_frame()

    def _draw_slide(self, now: float) -> None:
        aspect = self.backend.width / self.backend.height
        p = self.progress
        # The transition's own shader is used right up to and including the
        # frame at progress 1.0 -- that final frame is what completes a wipe or
        # a push.  Only once there is no outgoing slide left does the plain
        # fade program take over as the cheapest way to draw one picture.
        prog = self._program(self._active_transition if self.previous is not None else "fade")
        prog.use()

        cur = self.current
        prev = self.previous or cur
        assert cur is not None

        prev.texture.bind(0)
        prog.set1i("u_from", 0)
        cur.texture.bind(1)
        prog.set1i("u_to", 1)

        fx = prev.xform(aspect, now)
        tx = cur.xform(aspect, now)
        if self._rotate == 180:
            fx = tuple(-v for v in fx)
            tx = tuple(-v for v in tx)
        loc = prog.uniform("u_from_xform")
        if loc >= 0:
            gl.glUniform4f(loc, *fx)
        loc = prog.uniform("u_to_xform")
        if loc >= 0:
            gl.glUniform4f(loc, *tx)

        prog.set1f("u_progress", 1.0 if self.previous is None else p)
        prog.set1f("u_brightness", self.brightness)
        prog.set1f("u_time", now)
        prog.set2f("u_resolution", self.backend.width, self.backend.height)
        bg = [_srgb_to_linear(c) for c in self.background[:3]]
        prog.set4f("u_background", bg[0], bg[1], bg[2], 1.0)

        gl.glDisable(gl.BLEND)
        gl.glBindVertexArray(self._quad)
        gl.glDrawArrays(gl.TRIANGLE_STRIP, 0, 4)

        if p >= 1.0:
            # This frame *was* the final one: the incoming picture has now been
            # on screen complete, so the outgoing slide can go and the loop may
            # stop.  Retiring it here rather than at the next show() also means
            # its texture is freed a slide earlier.
            self._final_frame_pending = False
            if self.previous is not None:
                done, self.previous = self.previous, None
                self._retire(done)

    def _draw_overlays(self) -> None:
        visible = [o for o in self.overlays.values() if o.visible and o.alpha > 0.002]
        if not visible:
            return
        gl.glEnable(gl.BLEND)
        gl.glBlendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA)
        prog = self._overlay_prog
        prog.use()
        gl.glBindVertexArray(self._quad)
        W, H = self.backend.width, self.backend.height
        for ov in sorted(visible, key=lambda o: o.z):
            ov.texture.bind(0)
            prog.set1i("u_tex", 0)
            prog.set1f("u_alpha", ov.alpha)
            prog.set1f("u_brightness", self.brightness)
            x0 = (ov.x / W) * 2.0 - 1.0
            y0 = 1.0 - ((ov.y + ov.texture.height) / H) * 2.0
            w = (ov.texture.width / W) * 2.0
            h = (ov.texture.height / H) * 2.0
            loc = prog.uniform("u_rect")
            if loc >= 0:
                gl.glUniform4f(loc, x0, y0, w, h)
            gl.glDrawArrays(gl.TRIANGLE_STRIP, 0, 4)
        gl.glDisable(gl.BLEND)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        for ov in self.overlays.values():
            ov.close()
        self.overlays.clear()
        prev, cur = self.previous, self.current
        self.previous = self.current = None
        self._retire(prev)
        self._retire(cur)
        for prog in self._programs.values():
            prog.close()
        # The programs are gone; leaving their names in the cache would hand
        # out deleted GL objects if the renderer were used again after close().
        self._programs.clear()
        self._failed_programs.clear()
        self._overlay_prog.close()
        self._blank.close()
        gl.delete(gl.glDeleteVertexArrays, self._quad)
        gl.delete(gl.glDeleteBuffers, self._vbo)
