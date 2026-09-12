"""Choosing the mode to set on the panel.

A connector's preferred mode is right almost always, and wrong in the one case
that put this setting here: a 4K television asks for 2160p60, which a Pi 4
cannot clock without `hdmi_enable_4kp60` and which not every HDMI cable will
carry.  What must never happen is the frame refusing to start, or silently
running at a resolution nobody chose without saying so.
"""

import logging

import pytest

from picframe3.gfx.drm import DRM_MODE_TYPE_PREFERRED, DrmDevice, mode_name, parse_mode


class FakeMode:
    """Only the four fields the picker reads."""

    def __init__(self, w, h, hz, preferred=False):
        self.hdisplay = w
        self.vdisplay = h
        self.refresh_hz = float(hz)
        self.type = DRM_MODE_TYPE_PREFERRED if preferred else 0

    def __repr__(self):
        return f"<{self.hdisplay}x{self.vdisplay}@{self.refresh_hz:g}>"


class FakeConnector:
    def __init__(self, *modes, name="HDMI-A-1"):
        self.modes = list(modes)
        self.count_modes = len(modes)
        self.name = name


def a_4k_television():
    """What a Pi 4 sees on a 4K set: 2160p60 preferred, and it cannot do it."""
    return FakeConnector(
        FakeMode(3840, 2160, 60, preferred=True),
        FakeMode(3840, 2160, 30),
        FakeMode(1920, 1080, 60),
        FakeMode(1920, 1080, 50),
    )


# -- reading the setting ---------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("3840x2160@30", (3840, 2160, 30.0)),
    ("1920x1080", (1920, 1080, None)),
    (" 1920 x 1080 @ 59.94 ", (1920, 1080, 59.94)),
    ("3840×2160", (3840, 2160, None)),          # the × somebody pastes in
    ("1920x1080@60Hz", (1920, 1080, 60.0)),
    ("", None),
    ("big", None),
    ("1920", None),
])
def test_a_mode_is_understood_however_it_is_written(text, expected):
    assert parse_mode(text) == expected


def test_a_mode_is_written_the_same_way_it_is_read():
    assert mode_name(FakeMode(3840, 2160, 30.0)) == "3840x2160@30"
    assert parse_mode(mode_name(FakeMode(1920, 1080, 60.0))) == (1920, 1080, 60.0)


# -- choosing ---------------------------------------------------------------

def test_nothing_asked_for_takes_the_preferred_mode():
    chosen = DrmDevice._pick_mode(a_4k_television())
    assert (chosen.hdisplay, chosen.refresh_hz) == (3840, 60.0)


def test_the_mode_asked_for_wins_over_the_preferred_one():
    """The reason the setting exists."""
    chosen = DrmDevice._pick_mode(a_4k_television(), "3840x2160@30")
    assert (chosen.hdisplay, chosen.vdisplay, chosen.refresh_hz) == (3840, 2160, 30.0)


def test_a_size_without_a_refresh_takes_the_fastest_of_that_size():
    chosen = DrmDevice._pick_mode(a_4k_television(), "1920x1080")
    assert chosen.refresh_hz == 60.0


def test_a_size_without_a_refresh_prefers_the_preferred_one():
    connector = FakeConnector(
        FakeMode(1920, 1080, 50, preferred=True),
        FakeMode(1920, 1080, 60),
    )
    chosen = DrmDevice._pick_mode(connector, "1920x1080")
    assert chosen.refresh_hz == 50.0


def test_a_refresh_is_matched_loosely_enough_for_59_94():
    connector = FakeConnector(FakeMode(1920, 1080, 59.94))
    chosen = DrmDevice._pick_mode(connector, "1920x1080@60")
    assert chosen.refresh_hz == pytest.approx(59.94)


# -- getting it wrong -------------------------------------------------------

def test_a_mode_the_screen_does_not_offer_falls_back_rather_than_failing():
    """The frame is on a wall: a typo in the config must not be the reason it
    stops showing photographs."""
    chosen = DrmDevice._pick_mode(a_4k_television(), "2560x1440@60")
    assert (chosen.hdisplay, chosen.refresh_hz) == (3840, 60.0)


def test_the_fallback_says_what_the_screen_does_offer(caplog):
    with caplog.at_level(logging.WARNING, logger="picframe3.gfx.drm"):
        DrmDevice._pick_mode(a_4k_television(), "2560x1440@60")
    message = caplog.text
    assert "2560x1440@60" in message
    assert "3840x2160@30" in message and "1920x1080@60" in message
    assert "HDMI-A-1" in message


def test_nonsense_in_the_setting_says_how_to_write_it(caplog):
    with caplog.at_level(logging.WARNING, logger="picframe3.gfx.drm"):
        chosen = DrmDevice._pick_mode(a_4k_television(), "4k")
    assert "1920x1080" in caplog.text           # the shape it wants
    assert chosen.refresh_hz == 60.0            # and it still starts


def test_the_offered_list_does_not_repeat_itself(caplog):
    connector = FakeConnector(
        FakeMode(1920, 1080, 60, preferred=True),
        FakeMode(1920, 1080, 60),
        FakeMode(1920, 1080, 60),
    )
    with caplog.at_level(logging.WARNING, logger="picframe3.gfx.drm"):
        DrmDevice._pick_mode(connector, "800x600")
    assert caplog.text.count("1920x1080@60") == 1


# -- the setting reaches the backend ---------------------------------------

def test_the_mode_is_threaded_from_the_config_to_the_connector(monkeypatch):
    """Four functions deep, and each one had to be given the argument; a
    setting that stops one layer short of the driver does nothing at all."""
    from picframe3.gfx import backend as backend_module

    seen = {}

    class FakeKms:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(backend_module, "_log", logging.getLogger("test"))
    import sys
    import types
    fake = types.ModuleType("picframe3.gfx.backend_kms")
    fake.KmsBackend = FakeKms
    monkeypatch.setitem(sys.modules, "picframe3.gfx.backend_kms", fake)
    backend_module.create_backend("kms", mode="3840x2160@30")
    assert seen["mode"] == "3840x2160@30"


def test_find_output_asks_the_card_for_that_mode():
    device = DrmDevice.__new__(DrmDevice)          # no card opened
    asked = {}

    class FakeOutput:
        name = "HDMI-A-1"

    def fake_outputs(include_unknown=True, mode=""):
        asked["mode"] = mode
        return [FakeOutput()]

    device.outputs = fake_outputs
    device.find_output(None, "3840x2160@30")
    assert asked["mode"] == "3840x2160@30"


def test_the_running_frame_hands_the_mode_to_the_backend(monkeypatch):
    """The end of the chain: config -> app.setup -> create_backend. A setting
    that stops one layer short of the driver does nothing at all."""
    from picframe3 import app as app_module
    from picframe3.config import Config

    class Reached(Exception):
        pass

    seen = {}

    def fake_create_backend(kind, **kw):
        seen.update(kw)
        raise Reached

    monkeypatch.setattr(app_module, "create_backend", fake_create_backend)
    config = Config()
    config.display.mode = "3840x2160@30"
    with pytest.raises(Reached):
        app_module.PicFrame(config).setup()
    assert seen["mode"] == "3840x2160@30"
