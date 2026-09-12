"""How the caption fades in and out.

The caption is the one thing that moves while an otherwise finished still
picture is on screen, so it is also the one thing that can be given away by the
loop's idle sleep.  These tests pin down that the loop stays awake for the fade
and that the ramp it draws is eased rather than a straight line.
"""

import pytest


class FakeOverlay:
    def __init__(self, alpha=0.0):
        self.alpha = alpha


class FakeRenderer:
    """Only what the fade touches."""

    def __init__(self):
        self.in_transition = False
        self.current = None
        self.overlays = {"info": FakeOverlay()}

    def has_overlay(self, name):
        return name in self.overlays

    def overlay_alpha(self, name, alpha):
        self.overlays[name].alpha = max(0.0, min(1.0, alpha))


def _frame(**viewer):
    from picframe3.app import PicFrame
    from picframe3.config import Config

    cfg = Config()
    cfg.slideshow.transition_time = 2.0
    for key, value in viewer.items():
        setattr(cfg.viewer, key, value)
    frame = PicFrame(cfg)
    frame.renderer = FakeRenderer()
    frame._slide_started = 0.0
    frame._info_until = cfg.viewer.text_seconds
    return frame


def _samples(frame, t0, t1, fps=30.0):
    step = 1.0 / fps
    out, t = [], t0
    while t <= t1:
        out.append(frame._info_fade(t)[0])
        t += step
    return out


def test_a_fading_caption_keeps_the_loop_drawing():
    """Without this the loop idles at two wake-ups a second and the fade
    arrives in three visible steps."""
    frame = _frame(text_seconds=16.0)            # fade = 1.5 s, starts at 2.0 s

    assert frame._animating(2.3)                 # fading in
    assert not frame._animating(8.0)             # fully up, nothing moves
    assert frame._animating(15.0)                # fading out
    assert not frame._animating(17.0)            # gone


def test_the_loop_idles_again_once_the_caption_is_up():
    frame = _frame(text_seconds=16.0)
    frame._next_change_at = 180.0
    assert frame._sleep_for(8.0) == pytest.approx(0.5)


def test_the_fade_is_not_paced_a_whole_frame_after_the_flip():
    """The KMS page flip already blocked until the vblank.  Sleeping a full
    frame on top of it would beat against the panel's refresh."""
    frame = _frame(text_seconds=16.0)
    frame.config.display.fps_limit = 30.0
    frame._last_frame_at = 2.3 - 1 / 60          # the flip cost one vblank
    assert frame._sleep_for(2.3) == pytest.approx(1 / 30 - 1 / 60, abs=1e-6)

    frame._last_frame_at = 2.3 - 1.0             # already late: do not wait
    assert frame._sleep_for(2.3) == 0.0


def test_the_ramp_is_smooth_and_eased():
    frame = _frame(text_seconds=16.0)
    up = _samples(frame, 2.0, 3.5)

    assert up[0] == pytest.approx(0.0, abs=0.01)
    assert up[-1] == pytest.approx(1.0, abs=0.01)
    assert up == sorted(up)                              # never goes backwards
    steps = [b - a for a, b in zip(up, up[1:], strict=False)]
    assert max(steps) < 0.05                             # no visible jump
    assert steps[0] < max(steps) / 2                     # eased in
    assert steps[-1] < max(steps) / 2                    # eased out


def test_the_caption_stays_up_while_paused():
    frame = _frame(text_seconds=16.0)
    frame.paused = True
    assert frame._info_fade(100.0) == (1.0, False)


def test_ticking_writes_the_alpha_through_to_the_renderer():
    frame = _frame(text_seconds=16.0)
    frame.config.viewer.show_clock = False
    frame._tick_overlays(2.75)
    assert 0.0 < frame.renderer.overlays["info"].alpha < 1.0
    assert frame._dirty
