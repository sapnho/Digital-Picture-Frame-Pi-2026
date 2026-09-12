"""Transition effects.

Each entry is a GLSL ES 3.0 body for

    vec4 pf_transition(vec2 uv, float p)

where ``p`` runs 0 -> 1 and the helpers ``pf_from(uv)`` / ``pf_to(uv)`` sample
the outgoing and incoming slides in **linear** light.  Adding an effect means
adding one string here; nothing else in the renderer has to change.
"""

from __future__ import annotations

VERTEX_SHADER = """#version 300 es
in vec2 a_pos;
out vec2 v_uv;
void main() {
    v_uv = a_pos * 0.5 + 0.5;
    gl_Position = vec4(a_pos, 0.0, 1.0);
}
"""

FRAGMENT_TEMPLATE = """#version 300 es
precision highp float;

in vec2 v_uv;
out vec4 fragColor;

uniform sampler2D u_from;
uniform sampler2D u_to;
uniform vec4  u_from_xform;   // xy = uv scale, zw = uv offset
uniform vec4  u_to_xform;
uniform float u_progress;     // 0 -> 1
uniform float u_brightness;   // display dimming, linear multiplier
uniform vec4  u_background;   // linear RGBA shown outside the image
uniform vec2  u_resolution;
uniform float u_time;

const float PI = 3.14159265359;

// sRGB <-> linear.  Textures are uploaded as SRGB8_ALPHA8 so sampling already
// returns linear light; only the final write has to be re-encoded.
vec3 pf_linear_to_srgb(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));
}

vec4 pf_sample(sampler2D tex, vec4 xform, vec2 uv) {
    vec2 c = (uv - 0.5) * xform.xy + 0.5 + xform.zw;
    if (c.x < 0.0 || c.x > 1.0 || c.y < 0.0 || c.y > 1.0) {
        return u_background;
    }
    return texture(tex, c);
}

vec4 pf_from(vec2 uv) { return pf_sample(u_from, u_from_xform, uv); }
vec4 pf_to(vec2 uv)   { return pf_sample(u_to,   u_to_xform,   uv); }

float pf_hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

float pf_luma(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }

// smooth, symmetric ease so a fade neither snaps at the start nor lingers
float pf_ease(float t) { return t * t * (3.0 - 2.0 * t); }

%%BODY%%

void main() {
    vec4 c = pf_transition(v_uv, clamp(u_progress, 0.0, 1.0));
    fragColor = vec4(pf_linear_to_srgb(c.rgb * u_brightness), 1.0);
}
"""


TRANSITIONS: dict[str, str] = {}


def register(name: str, body: str) -> None:
    TRANSITIONS[name] = body.strip()


register("fade", """
vec4 pf_transition(vec2 uv, float p) {
    return mix(pf_from(uv), pf_to(uv), pf_ease(p));
}
""")

register("dissolve", """
vec4 pf_transition(vec2 uv, float p) {
    // Per-pixel threshold with a soft ramp, so grain appears and resolves
    // rather than popping.  Blue-noise-ish via two decorrelated hashes.
    float n = 0.5 * (pf_hash(floor(uv * u_resolution)) +
                     pf_hash(floor(uv * u_resolution * 0.37) + 17.0));
    float t = smoothstep(n - 0.15, n + 0.15, p * 1.3 - 0.15);
    return mix(pf_from(uv), pf_to(uv), t);
}
""")

register("wipe_left", """
vec4 pf_transition(vec2 uv, float p) {
    float edge = 1.0 - p * (1.0 + 0.08) ;
    float t = smoothstep(edge, edge + 0.08, 1.0 - uv.x);
    return mix(pf_from(uv), pf_to(uv), t);
}
""")

register("wipe_right", """
vec4 pf_transition(vec2 uv, float p) {
    float edge = p * 1.08 - 0.08;
    float t = smoothstep(edge, edge + 0.08, uv.x);
    return mix(pf_to(uv), pf_from(uv), t);
}
""")

register("wipe_up", """
vec4 pf_transition(vec2 uv, float p) {
    float edge = p * 1.08 - 0.08;
    float t = smoothstep(edge, edge + 0.08, uv.y);
    return mix(pf_to(uv), pf_from(uv), t);
}
""")

register("wipe_down", """
vec4 pf_transition(vec2 uv, float p) {
    float edge = 1.0 - p * 1.08;
    float t = smoothstep(edge, edge + 0.08, 1.0 - uv.y);
    return mix(pf_from(uv), pf_to(uv), t);
}
""")

register("push_left", """
vec4 pf_transition(vec2 uv, float p) {
    float e = pf_ease(p);
    if (uv.x > 1.0 - e) return pf_to(vec2(uv.x - (1.0 - e), uv.y));
    return pf_from(vec2(uv.x + e, uv.y));
}
""")

register("push_up", """
vec4 pf_transition(vec2 uv, float p) {
    float e = pf_ease(p);
    if (uv.y > 1.0 - e) return pf_to(vec2(uv.x, uv.y - (1.0 - e)));
    return pf_from(vec2(uv.x, uv.y + e));
}
""")

register("zoom", """
vec4 pf_transition(vec2 uv, float p) {
    float e = pf_ease(p);
    // outgoing pushes gently towards the viewer, incoming settles back
    vec2 a = (uv - 0.5) / mix(1.0, 0.88, e) + 0.5;
    vec2 b = (uv - 0.5) / mix(1.12, 1.0, e) + 0.5;
    return mix(pf_from(a), pf_to(b), e);
}
""")

register("blur", """
vec4 pf_transition(vec2 uv, float p) {
    // Defocus out, focus in.  Nine taps on a rotating disc keeps the cost
    // acceptable on a Pi 4 while avoiding visible sampling artefacts.
    float e = pf_ease(p);
    float r = sin(e * PI) * 0.015;
    vec4 a = vec4(0.0), b = vec4(0.0);
    for (int i = 0; i < 9; i++) {
        float ang = float(i) * (2.0 * PI / 9.0) + e * 2.0;
        vec2 o = vec2(cos(ang), sin(ang)) * r * vec2(1.0, u_resolution.x / u_resolution.y);
        a += pf_from(uv + o);
        b += pf_to(uv + o);
    }
    a /= 9.0; b /= 9.0;
    return mix(a, b, e);
}
""")

register("burn", """
vec4 pf_transition(vec2 uv, float p) {
    // pi3d's "burn": the outgoing frame blows out towards white in its bright
    // areas first.  Reproduced here in linear light, which is where it was
    // always supposed to happen.
    vec4 a = pf_from(uv);
    vec4 b = pf_to(uv);
    float e = pf_ease(p);
    float heat = smoothstep(0.0, 0.55, e) * (1.0 - smoothstep(0.45, 1.0, e));
    vec3 glow = mix(a.rgb, b.rgb, e) + heat * (0.35 + 0.65 * pf_luma(a.rgb));
    return vec4(mix(mix(a.rgb, b.rgb, e), glow, heat), 1.0);
}
""")

register("bump", """
vec4 pf_transition(vec2 uv, float p) {
    // Displacement driven by the outgoing image's luminance -- pi3d's "bump".
    float e = pf_ease(p);
    float amp = sin(e * PI) * 0.03;
    vec4 a = pf_from(uv);
    vec2 d = vec2(pf_luma(a.rgb) - 0.5) * amp;
    return mix(pf_from(uv + d), pf_to(uv - d), e);
}
""")

register("radial", """
vec4 pf_transition(vec2 uv, float p) {
    vec2 d = uv - 0.5;
    float ang = atan(d.y, d.x) + PI;
    float t = smoothstep(0.0, 0.25, p * 2.0 * PI * 1.05 - ang);
    return mix(pf_from(uv), pf_to(uv), t);
}
""")

register("pixelate", """
vec4 pf_transition(vec2 uv, float p) {
    float e = sin(pf_ease(p) * PI);
    float blocks = mix(u_resolution.y, 12.0, e);
    vec2 q = (floor(uv * blocks) + 0.5) / blocks;
    return mix(pf_from(q), pf_to(q), pf_ease(p));
}
""")

register("none", """
vec4 pf_transition(vec2 uv, float p) {
    return p < 0.5 ? pf_from(uv) : pf_to(uv);
}
""")


#: Reasonable pool for ``transition: random``.
RANDOM_POOL = ("fade", "dissolve", "zoom", "blur", "push_left", "push_up",
               "wipe_left", "wipe_up", "radial")


def fragment_source(name: str) -> str:
    try:
        body = TRANSITIONS[name]
    except KeyError:
        raise KeyError(
            f"unknown transition {name!r}; available: {', '.join(sorted(TRANSITIONS))}"
        ) from None
    return FRAGMENT_TEMPLATE.replace("%%BODY%%", body)


def names() -> list[str]:
    return sorted(TRANSITIONS)
