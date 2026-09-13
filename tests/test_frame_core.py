"""The frame's own behaviour: power, settings, and one slide at a time.

These cover the faults that made a working frame stop being one -- a screen
that could not be switched off, a screen that never came back on, a setting
that crashed the render loop, and two "next picture" requests racing each
other -- rather than anything about pictures.
"""

import asyncio
import time

import pytest

from picframe3.app import PicFrame
from picframe3.config import Config
from picframe3.settings import changed_keys, restart_required


def _frame(**power) -> PicFrame:
    cfg = Config()
    for key, value in power.items():
        setattr(cfg.power, key, value)
    return PicFrame(cfg)


# -- the display schedule ---------------------------------------------------

def test_the_display_can_be_switched_off_with_no_schedule():
    """The shipped default used to undo every manual switch within a second."""
    frame = _frame(enabled=True, schedule={})
    frame.set_display = lambda on: setattr(frame, "display_on", on)

    frame.display_on = False                     # as if somebody had pressed off
    frame.apply_power_schedule()
    assert frame.display_on is False


def test_turning_the_schedule_off_does_not_black_the_screen():
    """`power.enabled: false` reads as "no schedule", not "no picture"."""
    frame = _frame(enabled=False, schedule={"all": ["23:00-07:00"]})
    frame.set_display = lambda on: setattr(frame, "display_on", on)

    frame.apply_power_schedule()
    assert frame.display_on is True


def test_a_schedule_still_switches_the_screen_at_its_edges():
    frame = _frame(enabled=True, schedule={"all": ["23:00-07:00"]})
    switched = []
    frame.set_display = lambda on: (switched.append(on),
                                    setattr(frame, "display_on", on))

    frame.power.display_should_be_on = lambda: False
    frame.apply_power_schedule()
    assert switched == [False]

    # Somebody switches it back on inside the off period: the schedule has
    # already had its say and must not fight them every second.
    frame.display_on = True
    frame.apply_power_schedule()
    assert switched == [False]

    # Morning: already on, so there is nothing to do -- but the schedule has
    # noted where it now stands, which is what lets it act again tonight.
    frame.power.display_should_be_on = lambda: True
    frame.apply_power_schedule()
    assert switched == [False]

    frame.power.display_should_be_on = lambda: False     # tonight
    frame.apply_power_schedule()
    assert switched == [False, False]
    assert frame.display_on is False


# -- settings that used to reach the render loop ---------------------------

def test_a_nonsense_clock_position_is_refused_rather_than_stored():
    frame = PicFrame(Config())
    frame.applier.apply = lambda key: None          # no renderer in this test
    frame._apply_setting("viewer.clock_position", "")
    assert frame.config.viewer.clock_position == "TR"


def test_an_interval_of_zero_is_clamped_not_obeyed():
    """Zero turns the render loop into a busy loop decoding twenty a second."""
    frame = PicFrame(Config())
    frame.applier.apply = lambda key: None
    frame._apply_setting("slideshow.interval", 0)
    assert frame.config.slideshow.interval >= 1.0


def test_a_secret_never_reaches_the_log(caplog):
    frame = PicFrame(Config())
    frame.applier.apply = lambda key: None
    with caplog.at_level("INFO"):
        frame._apply_setting("mqtt.password", "hunter2")
    assert frame.config.mqtt.password == "hunter2"
    assert "hunter2" not in caplog.text


def test_a_setting_may_not_point_the_frame_outside_its_own_places():
    frame = PicFrame(Config())
    frame.applier.apply = lambda key: None
    before = frame.config.logging.file
    frame._apply_setting("logging.file", "~/.bashrc")
    assert frame.config.logging.file == before


def test_the_brightness_counts_as_an_unsaved_change():
    frame = PicFrame(Config())
    frame.set_brightness(0.4)
    assert frame._config_dirty is True


# -- reloading the file ----------------------------------------------------

def test_a_reload_applies_everything_that_changed():
    before = Config().as_dict()
    fresh = Config()
    fresh.display.brightness = 0.3
    fresh.slideshow.order = "name"
    fresh.http.port = 9001
    changed = set(changed_keys(before, fresh.as_dict()))
    assert changed == {"display.brightness", "slideshow.order", "http.port"}
    # ... and says honestly which of them a running frame cannot pick up.
    assert restart_required(sorted(changed)) == {"http.port"}


# -- one slide at a time ---------------------------------------------------

class _Playlist:
    """Hands out one picture per call and counts how often it was asked."""

    def __init__(self):
        self.calls = 0
        self.round = 1

    def next(self):
        self.calls += 1
        return []               # empty: the controller shows the placeholder

    def previous(self):
        return self.next()


def test_two_requests_at_once_do_not_draw_from_the_playlist_twice():
    """Preparing a slide yields; a key press in that window used to race it."""
    frame = PicFrame(Config())
    frame.playlist = _Playlist()
    frame.loader = object()
    shown = []
    frame.slideshow._show_placeholder = lambda: shown.append(time.monotonic())

    async def main():
        await asyncio.gather(frame._advance(), frame._advance())

    asyncio.run(main())
    assert frame.playlist.calls == 2        # serialised, not interleaved
    assert len(shown) == 2


def test_an_unreadable_run_gives_up_instead_of_recursing(tmp_path):
    """A folder that lost its permissions used to be a RecursionError."""
    from picframe3 import slideshow as slideshow_module

    frame = PicFrame(Config())

    # A file that is *there* and will not decode -- which is a different case
    # from a file that has gone, and the one this test is about.  A missing
    # path is forgotten and skipped long before the loader sees it.
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a picture")

    class _Record:
        id = 1
        path = str(broken)

        def as_meta(self):
            return self

    class _Endless:
        round = 1

        def next(self):
            return [_Record()]

        def previous(self):
            return self.next()

        def refresh(self):
            pass

    class _Loader:
        def prefetched_for(self, metas):
            return False

        def cancel_prefetch(self):
            pass

        async def load(self, metas):
            return None                     # never readable

    hidden = []
    frame.playlist = _Endless()
    frame.loader = _Loader()
    frame.library = type("L", (), {
        "set_hidden": lambda self, i, v: hidden.append(i),
        "forget": lambda self, paths: 0,
    })()
    frame.slideshow._show_placeholder = lambda: None

    asyncio.run(frame._advance())
    assert len(hidden) == slideshow_module.MAX_SKIPPED


def test_a_picture_past_the_decode_limit_is_not_hidden(tmp_path):
    """A setting refused it, so raising the setting has to bring it back.

    Hiding is for a file that will not decode, and ``hidden`` is cleared only
    when a file's bytes change -- so hiding this one would mean the picture
    stayed gone after the limit went up, with nothing to say why.
    """
    from picframe3.media.prepare import TooLargeToDecode

    frame = PicFrame(Config())
    scan = tmp_path / "scan.png"
    scan.write_bytes(b"pretend this is 200 megapixels")

    class _Record:
        id = 7
        path = str(scan)

        def as_meta(self):
            return self

    class _Once:
        def __init__(self):
            self.calls = 0

        def next(self):
            self.calls += 1
            return [_Record()]

        def previous(self):
            return self.next()

        def refresh(self):
            pass

    class _Loader:
        def prefetched_for(self, metas):
            return False

        def cancel_prefetch(self):
            pass

        async def load(self, metas):
            raise TooLargeToDecode(str(scan), (16000, 12000), 64_000_000)

    hidden = []
    frame.playlist = _Once()
    frame.loader = _Loader()
    frame.library = type("L", (), {
        "set_hidden": lambda self, i, v: hidden.append(i),
        "forget": lambda self, paths: 0,
    })()
    frame.slideshow._show_placeholder = lambda: None

    asyncio.run(frame._advance())
    assert hidden == [], "an oversized picture was hidden and cannot come back"


# -- screenshots -----------------------------------------------------------

def test_two_screenshot_requests_share_one_capture():
    """One slot meant the first caller waited out its timeout for nothing."""
    frame = PicFrame(Config())

    async def main():
        first = asyncio.ensure_future(frame.screenshot(timeout=2))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(frame.screenshot(timeout=2))
        await asyncio.sleep(0)
        request = frame._capture_request
        assert request is not None
        request.set_result(_pixels())
        return await asyncio.gather(first, second)

    def _pixels():
        import numpy as np

        return np.zeros((4, 4, 4), dtype="uint8")

    both = asyncio.run(main())
    assert both[0] == both[1]


# -- quitting --------------------------------------------------------------

def test_quit_says_it_was_a_quit():
    """Under systemd a clean exit is not a stop; the CLI needs to know why."""
    frame = PicFrame(Config())
    asyncio.run(frame.handle(_command("quit")))
    assert frame.quit_requested is True
    assert frame._stop.is_set()


# -- shutting the Pi down ---------------------------------------------------

def test_shutdown_stops_the_loop_and_asks_for_a_power_off():
    """The flag the CLI reads once the loop has ended -- and it has to be a
    different one from `restart_requested`, or a frame asked to shut down is
    started straight back up."""
    frame = PicFrame(Config())
    asyncio.run(frame.handle(_command("shutdown")))
    assert frame.power_off_requested is True
    assert frame.restart_requested is False
    assert frame._stop.is_set()


def test_the_state_says_whether_the_frame_can_power_itself_off(monkeypatch):
    """The web interface hides the button on this, so it must follow whether
    there is a systemd to ask rather than being a hopeful `True`."""
    frame = PicFrame(Config())
    monkeypatch.setattr("picframe3.app.shutil.which", lambda name: None)
    assert frame.can_power_off() is False
    monkeypatch.setattr("picframe3.app.shutil.which", lambda name: "/bin/" + name)
    assert frame.can_power_off() is True


def _command(action: str):
    from picframe3.events import Command

    return Command.parse({"action": action}, source="test")


# -- library totals --------------------------------------------------------

def test_the_library_totals_are_counted_at_most_every_few_seconds():
    """state() is rebuilt several times a second during a transition."""
    frame = PicFrame(Config())
    counted = []

    class _Library:
        def stats(self):
            counted.append(1)
            return {"files": 3}

        def hold_summary(self):
            return {"held": 0, "came_back": 0}

    frame.library = _Library()
    for _ in range(20):
        frame._library_stats()
    assert len(counted) == 1

    frame.invalidate_stats()
    frame._library_stats()
    assert len(counted) == 2


@pytest.mark.parametrize("value,expected", [
    (["0", "0", "0", "1"], [0.0, 0.0, 0.0, 1.0]),
    ([0, 0, 0, 1], [0.0, 0.0, 0.0, 1.0]),
])
def test_a_colour_typed_as_text_becomes_numbers(value, expected):
    """Four strings in a list of floats used to fail much later, in the renderer."""
    cfg = Config()
    assert cfg.set("display.background", value) == expected


def test_a_malformed_schedule_is_refused_rather_than_emptied():
    """Silently turning it into {} deletes the schedule and says nothing."""
    cfg = Config()
    cfg.power.schedule = {"all": ["23:00-07:00"]}
    with pytest.raises(ValueError):
        cfg.set("power.schedule", "every night")
    assert cfg.power.schedule == {"all": ["23:00-07:00"]}


# -- the second look -------------------------------------------------------

def test_a_reload_does_not_leave_unsaved_changes_behind():
    """Applying the brightness marks the config dirty -- quite rightly.

    Clearing the flag before applying therefore left every reload reporting
    unsaved changes for ever, with the save banner up and `POST /api/restart`
    writing the file back unasked.
    """
    import tempfile
    from pathlib import Path

    fresh = Config()
    fresh.display.brightness = 0.42
    path = Path(tempfile.mkdtemp()) / "config.yaml"
    fresh.save(str(path))

    frame = PicFrame(Config())
    frame.config.source_path = str(path)
    frame.applier.apply = lambda key: frame.set_brightness(
        frame.config.display.brightness)
    frame._reload_config()
    assert frame.config.display.brightness == pytest.approx(0.42)
    assert frame._config_dirty is False


def test_the_dimming_schedule_leaves_a_switched_off_screen_alone():
    """On a panel with no DPMS and no backlight, "off" IS brightness zero.

    The maintenance loop reasserted the scheduled brightness every second and
    undid it, so the screen came back on by itself a second after every
    switch-off.
    """
    frame = PicFrame(Config())
    frame.display_on = False

    class _Renderer:
        brightness = 0.0

    frame.renderer = _Renderer()
    # What the loop does, in one line:
    if frame.display_on:
        frame.renderer.brightness = frame.power.brightness(
            default=frame.config.display.brightness)
    assert frame.renderer.brightness == 0.0


def test_the_deleted_folder_is_not_part_of_the_library():
    """Put it under the pictures -- as picframe did -- and inotify brought
    every removed photograph straight back into the index."""
    cfg = Config()
    cfg.library.picture_folders = ["~/Pictures"]
    cfg.library.deleted_folder = "~/Pictures/.deleted-by-frame"
    frame = PicFrame(cfg)
    assert ".deleted-by-frame" in frame._scan_exclusions()


# -- the slideshow follows the screen ---------------------------------------

class _FakeVideo:
    """Just enough of VideoPlayer to see what the frame asks of it."""

    def __init__(self):
        self.paused = False
        self.calls: list[bool] = []

    def pause(self, paused: bool = True) -> None:
        self.paused = paused
        self.calls.append(paused)


def test_a_film_is_paused_with_the_screen_and_resumed_with_it():
    """A video used to run on behind a dark panel.

    It would be over -- or well past the part worth seeing -- by the time
    anyone switched the screen back on, and with sound on it played to an empty
    room.  The film now stops with the picture and picks up where it left off.
    """
    frame = _frame()
    frame.video = _FakeVideo()

    frame.set_display(False)
    assert frame.video.paused is True

    frame.set_display(True)
    assert frame.video.paused is False
    assert frame.video.calls == [True, False]


def test_switching_the_screen_on_does_not_un_pause_the_slideshow():
    """Two different pauses, and the screen only owns one of them."""
    frame = _frame()
    frame.video = _FakeVideo()
    frame.paused = True                          # somebody pressed pause first

    frame.set_display(False)
    frame.set_display(True)
    assert frame.video.paused is True            # still paused, as asked


def test_the_picture_gets_a_full_interval_after_the_screen_comes_back():
    """Not the remainder of an interval that ran out in the dark."""
    frame = _frame()
    frame.config.slideshow.interval = 120.0

    frame.set_display(False)
    frame._next_change_at = time.monotonic() - 60.0      # long overdue
    frame.set_display(True)

    assert frame._next_change_at - time.monotonic() == pytest.approx(120.0, abs=1.0)


def test_the_loop_idles_while_the_screen_is_off():
    """Nothing is due when the slideshow is on hold, so stop waking for it."""
    frame = _frame()
    frame._next_change_at = time.monotonic() - 5.0       # would mean 0.05s spins

    assert frame._sleep_for(time.monotonic()) == pytest.approx(0.05)
    frame.set_display(False)
    assert frame._sleep_for(time.monotonic()) == pytest.approx(0.5)
