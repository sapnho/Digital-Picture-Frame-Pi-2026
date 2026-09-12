"""Asking for the caption, and the language it is written in.

picframe's owners built something out of its text switches that picframe never
named: a frame that writes *nothing* over the picture, until somebody wants to
know where a photograph was taken and presses a button, and then writes the
date and the place for long enough to read them.  Two settings say that here
(`viewer.text_seconds: 0` and `viewer.peek_seconds`), and three surfaces have
to be able to press it: Home Assistant, the web interface, the keyboard.

The date it writes is also the one place a frame speaks a language, which is
why the locale belongs in this file rather than next to the clock.
"""

import time

import pytest

from picframe3.config import Config, set_time_locale
from picframe3.events import Action, Command


class FakeOverlay:
    def __init__(self):
        self.alpha = 0.0


class FakeRenderer:
    def __init__(self):
        self.in_transition = False
        self.current = None
        self.brightness = 1.0
        self.overlays = {"info": FakeOverlay()}
        self.cleared = []

    def has_overlay(self, name):
        return name in self.overlays

    def overlay_alpha(self, name, alpha):
        self.overlays[name].alpha = max(0.0, min(1.0, alpha))

    def set_overlay(self, name, image, **kw):
        if image is None:
            self.cleared.append(name)


def _frame(**viewer):
    from picframe3.app import PicFrame

    cfg = Config()
    cfg.slideshow.transition_time = 2.0
    for key, value in viewer.items():
        setattr(cfg.viewer, key, value)
    frame = PicFrame(cfg)
    frame.renderer = FakeRenderer()
    frame._slide_started = 0.0
    frame._info_until = cfg.viewer.text_seconds
    return frame


# -- how long a caption asked for stays up ---------------------------------

def test_a_reveal_lasts_longer_than_the_caption_after_a_change():
    """The whole point of the setting: 40 seconds to read it, without every
    slide's caption lasting 40 seconds."""
    frame = _frame(text_seconds=16.0, peek_seconds=40.0)
    assert frame._peek_seconds() == 40.0


def test_a_frame_that_has_never_heard_of_peeking_behaves_as_before():
    frame = _frame(text_seconds=16.0, peek_seconds=0.0)
    assert frame._peek_seconds() == 16.0


def test_the_command_may_name_its_own_duration():
    frame = _frame(text_seconds=16.0, peek_seconds=40.0)
    assert frame._peek_seconds({"seconds": 90}) == 90.0
    assert frame._peek_seconds({"seconds": "12.5"}) == 12.5


@pytest.mark.parametrize("payload", [{}, {"seconds": None}, {"seconds": "soon"},
                                     {"seconds": 0}, {"seconds": -5}])
def test_nonsense_in_the_payload_falls_back_to_the_setting(payload):
    frame = _frame(text_seconds=16.0, peek_seconds=40.0)
    assert frame._peek_seconds(payload) == 40.0


# -- the fade that goes with it --------------------------------------------

def test_a_long_reveal_still_fades_in():
    """The ramp is a quarter of however long the caption is up for, so a
    40-second reveal arrives at the same speed as a 16-second caption rather
    than snapping on."""
    frame = _frame(text_seconds=16.0, peek_seconds=40.0)
    frame._info_until = 2.0 + 40.0          # start is transition_time = 2.0
    assert frame._info_fade(2.0)[0] == pytest.approx(0.0)
    assert 0.0 < frame._info_fade(2.75)[0] < 1.0
    assert frame._info_fade(4.0)[0] == pytest.approx(1.0)


def test_captions_off_by_default_does_not_make_the_reveal_instant():
    """`text_seconds: 0` writes nothing by itself.  The reveal it is paired
    with still has to fade: with the fade taken from text_seconds it was
    0.001 s, which is a snap."""
    frame = _frame(text_seconds=0.0, peek_seconds=40.0)
    frame._slide_started = 0.0
    frame._info_until = 2.0 + 40.0
    assert 0.0 < frame._info_fade(2.5)[0] < 1.0


def test_with_no_reveal_in_force_nothing_is_written():
    frame = _frame(text_seconds=0.0)
    frame._info_until = 0.0
    alpha, moving = frame._info_fade(10.0)
    assert alpha == 0.0 and not moving


# -- the three surfaces ----------------------------------------------------

@pytest.mark.asyncio
async def test_asking_for_the_caption_turns_it_on_and_sets_its_clock():
    frame = _frame(text_seconds=0.0, peek_seconds=30.0)
    frame.show_info = False
    await frame.handle(Command(Action.INFO_SHOW, source="test"))
    assert frame.show_info is True
    # No picture on screen in this fixture, so only the flag is asserted here;
    # the timing is covered above through _peek_seconds and _info_fade.
    assert frame._dirty


@pytest.mark.asyncio
async def test_hiding_the_caption_takes_it_away_now():
    frame = _frame(text_seconds=16.0)
    frame.show_info = True
    await frame.handle(Command(Action.INFO_HIDE, source="test"))
    assert frame.show_info is False


@pytest.mark.asyncio
async def test_toggle_still_toggles():
    frame = _frame()
    frame.show_info = True
    await frame.handle(Command(Action.INFO_TOGGLE, source="test"))
    assert frame.show_info is False
    await frame.handle(Command(Action.INFO_TOGGLE, source="test"))
    assert frame.show_info is True


def test_the_state_document_says_which_elements_are_written():
    """Home Assistant's caption box has to be able to show what the caption
    currently says, or it is a box that forgets."""
    frame = _frame(show_text=["date", "location"])
    assert frame.state().caption_fields == ["date", "location"]


# -- Home Assistant --------------------------------------------------------

@pytest.mark.parametrize("payload,expected", [
    ("on", Action.INFO_SHOW),
    ("ON", Action.INFO_SHOW),
    ("1", Action.INFO_SHOW),
    ("off", Action.INFO_HIDE),
    ("OFF", Action.INFO_HIDE),
])
@pytest.mark.asyncio
async def test_the_captions_switch_shows_and_hides(payload, expected):
    pytest.importorskip("aiomqtt")
    from picframe3.control.mqtt import MqttBridge

    frame = _frame()
    sent = []
    frame.bus.submit = sent.append
    bridge = MqttBridge(frame, frame.config.mqtt)
    await bridge._dispatch(f"{bridge.prefix}/captions/set", payload)
    assert [c.action for c in sent] == [expected]


@pytest.mark.asyncio
async def test_the_button_reveals_the_caption():
    pytest.importorskip("aiomqtt")
    from picframe3.control.mqtt import MqttBridge

    frame = _frame()
    sent = []
    frame.bus.submit = sent.append
    bridge = MqttBridge(frame, frame.config.mqtt)
    await bridge._dispatch(f"{bridge.prefix}/info_show/set", "press")
    assert [c.action for c in sent] == [Action.INFO_SHOW]


@pytest.mark.parametrize("typed,expected", [
    ("date, location", ["date", "location"]),
    ("  date ,location ", ["date", "location"]),
    ("", []),
    (" , ", []),
])
@pytest.mark.asyncio
async def test_the_caption_box_takes_a_comma_separated_list(typed, expected):
    pytest.importorskip("aiomqtt")
    from picframe3.control.mqtt import MqttBridge

    frame = _frame()
    sent = []
    frame.bus.submit = sent.append
    bridge = MqttBridge(frame, frame.config.mqtt)
    await bridge._dispatch(f"{bridge.prefix}/caption_fields/set", typed)
    assert len(sent) == 1
    assert sent[0].action is Action.SET_CONFIG
    assert sent[0].payload == {"key": "viewer.show_text", "value": expected}


def test_home_assistant_is_offered_all_three_ways_in():
    pytest.importorskip("aiomqtt")
    from picframe3.control.mqtt import MqttBridge

    frame = _frame()
    entities = MqttBridge(frame, frame.config.mqtt).discovery_entities()
    offered = {object_id: component for component, object_id, _ in entities}
    assert offered.get("captions") == "switch"
    assert offered.get("caption_fields") == "text"
    assert offered.get("info_show") == "button"


# -- the language the date is written in -----------------------------------

def test_no_locale_asked_for_changes_nothing():
    assert set_time_locale("") is True


def test_a_locale_the_system_has_not_generated_is_reported_not_ignored():
    """The failure that matters: a frame configured for German dates, showing
    English ones, with nothing anywhere to say why."""
    assert set_time_locale("zz_ZZ.UTF-8") is False


def test_the_locale_reaches_strftime():
    installed = set_time_locale("de_DE.UTF-8")
    if not installed:
        pytest.skip("de_DE.UTF-8 is not generated on this machine")
    try:
        assert "Dezember" in time.strftime("%B", time.struct_time(
            (2026, 12, 24, 12, 0, 0, 3, 358, 0)))
    finally:
        set_time_locale("C")


def test_the_locale_is_a_setting_the_running_frame_picks_up():
    """It is in `viewer`, so it applies without a restart -- and the applier
    has to be the thing that sets it, because strftime reads the C library."""
    from picframe3 import uischema

    assert not uischema.needs_restart("viewer.locale")
