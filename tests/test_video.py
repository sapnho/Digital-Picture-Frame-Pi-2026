"""Video playback: who decides when a video slide is over.

The rule these tests pin down is that the *player* ends a video slide, never the
slideshow clock.  A video runs for as long as it plays -- its own length, not
the interval a still picture gets -- and it only starts once it has finished
fading in.
"""

import time
import types

import pytest

from picframe3.media import video as video_module

# ---------------------------------------------------------------------------
# A GStreamer stand-in.  Only the handful of entry points VideoPlayer touches.
# ---------------------------------------------------------------------------

class FakeBus:
    def __init__(self):
        self.queue = []

    def post_eos(self):
        self.queue.append(types.SimpleNamespace(type=FakeGst.MessageType.EOS))

    def pop_filtered(self, mask):
        return self.queue.pop(0) if self.queue else None


class FakeAppsink:
    def emit(self, name, *args):
        return None                     # no frames: these tests are about timing


class FakeBin:
    def __init__(self, sink):
        self.sink = sink

    def get_by_name(self, name):
        return self.sink


class FakePipeline:
    def __init__(self):
        self.states = []
        self.props = {}
        self.seeks = 0
        self.bus = FakeBus()

    def set_property(self, key, value):
        self.props[key] = value

    def set_state(self, state):
        self.states.append(state)

    def get_bus(self):
        return self.bus

    def seek_simple(self, fmt, flags, pos):
        self.seeks += 1

    def query_position(self, fmt):
        return True, 0

    def query_duration(self, fmt):
        return True, 10 * FakeGst.SECOND

    @property
    def state(self):
        return self.states[-1] if self.states else None


class FakeGst:
    SECOND = 1_000_000_000
    CLOCK_TIME_NONE = 2 ** 64 - 1
    State = types.SimpleNamespace(PLAYING="playing", PAUSED="paused", NULL="null")
    Format = types.SimpleNamespace(TIME=1)
    SeekFlags = types.SimpleNamespace(FLUSH=1, KEY_UNIT=2)
    MessageType = types.SimpleNamespace(ERROR=1, EOS=2, WARNING=4)
    MapFlags = types.SimpleNamespace(READ=1)

    last_pipeline: FakePipeline | None = None

    @staticmethod
    def parse_bin_from_description(desc, ghost):
        return FakeBin(FakeAppsink())

    class ElementFactory:
        @staticmethod
        def make(name, _):
            FakeGst.last_pipeline = FakePipeline()
            return FakeGst.last_pipeline


@pytest.fixture
def player(monkeypatch):
    monkeypatch.setattr(video_module, "_gst_ready", True)
    monkeypatch.setattr(video_module, "Gst", FakeGst)
    p = video_module.VideoPlayer((16, 9))
    yield p
    p.stop()


def _age(player, seconds):
    """Pretend the video has been running for ``seconds``."""
    player._started -= seconds


def test_a_video_plays_to_the_end_by_default(player):
    player.play("/x/clip.mp4")
    _age(player, 3600)                  # an hour in, with no cap set
    assert player.poll() is None         # no frame in the fake, but still live
    assert player.playing


def test_max_seconds_stops_the_player_itself(player):
    player.max_seconds = 30.0
    player.play("/x/clip.mp4")
    _age(player, 29.0)
    player.poll()
    assert player.playing
    _age(player, 2.0)
    player.poll()
    assert not player.playing
    # Torn down, not left paused.  The last frame is already a texture and
    # stays on screen for the crossfade either way; what a paused pipeline does
    # keep is the one hardware decoder the Pi has, and the next video needs it.
    assert FakeGst.last_pipeline.state == FakeGst.State.NULL


def test_end_of_stream_ends_playback_when_not_looping(player):
    player.play("/x/clip.mp4")
    FakeGst.last_pipeline.bus.post_eos()
    player.poll()
    assert not player.playing
    assert FakeGst.last_pipeline.seeks == 0


def test_looping_repeats_only_inside_its_window(player):
    player.loop = True
    player.max_seconds = 60.0
    player.play("/x/clip.mp4")

    FakeGst.last_pipeline.bus.post_eos()
    player.poll()
    assert player.playing and FakeGst.last_pipeline.seeks == 1

    _age(player, 61.0)
    FakeGst.last_pipeline.bus.post_eos()
    player.poll()
    assert not player.playing
    assert FakeGst.last_pipeline.seeks == 1      # no further repeat


def test_pausing_does_not_eat_the_window(player):
    player.max_seconds = 30.0
    player.play("/x/clip.mp4")
    _age(player, 10.0)
    player.pause(True)
    _age(player, 100.0)                 # a long time spent paused
    player._paused_at -= 100.0
    player.pause(False)
    assert player.elapsed == pytest.approx(10.0, abs=1.0)
    assert not player.expired()


# ---------------------------------------------------------------------------
# The slideshow side
# ---------------------------------------------------------------------------

class FakeTexture:
    def update(self, data, w, h):
        self.last = (w, h)


class FakeSlide:
    def __init__(self, meta):
        self.meta = meta
        self.texture = FakeTexture()
        self.flip_v = False
        self.kenburns = False
        self.started = time.monotonic()
        self.duration = 0.0


class FakeRenderer:
    def __init__(self, slide, in_transition=False):
        self.current = slide
        self.in_transition = in_transition


class FakePlayer:
    def __init__(self, playing=True):
        self.playing = playing
        self.stopped = False

    def poll(self):
        return None

    def stop(self):
        self.stopped = True


def _frame(**slideshow):
    from picframe3.app import PicFrame
    from picframe3.config import Config

    cfg = Config()
    for key, value in slideshow.items():
        setattr(cfg.slideshow, key, value)
    return PicFrame(cfg)


def _video_slide(path="/x/clip.mp4"):
    meta = types.SimpleNamespace(is_video=True, video_path=path, fit="cover")
    return FakeSlide(meta)


def _record(duration, is_video=True):
    from picframe3.library.db import Record

    fields = dict.fromkeys(Record.__dataclass_fields__)
    fields.update(id=1, path="/x/clip.mp4", folder="/x", basename="clip.mp4",
                  ext=".mp4", mtime=0.0, size=1, is_video=is_video,
                  orientation=1, is_portrait=False, duration=duration,
                  play_count=0, hidden=False, indexed_at=0.0, tags=[])
    return Record(**fields)


def test_a_video_gets_its_own_length_not_the_interval():
    frame = _frame(interval=15.0, transition_time=2.0)
    assert frame._slide_duration([_record(300.0)]) == pytest.approx(302.0)
    assert frame._slide_duration([_record(None, is_video=False)]) == 15.0


def test_max_seconds_shortens_the_expected_length():
    frame = _frame(interval=15.0, transition_time=2.0, video_max_seconds=30.0)
    assert frame._slide_duration([_record(300.0)]) == pytest.approx(32.0)
    assert frame._video_window() == 30.0


def test_looping_repeats_inside_the_still_interval():
    frame = _frame(interval=180.0, transition_time=2.0, video_loop=True)
    assert frame._video_window() == 180.0
    assert frame._slide_duration([_record(8.0)]) == pytest.approx(182.0)


def test_without_looping_a_video_runs_to_the_end():
    frame = _frame()
    assert frame._video_window() == 0.0          # 0 = play it out


def test_the_video_waits_for_the_fade_to_finish():
    frame = _frame()
    started = []
    frame.renderer = FakeRenderer(_video_slide(), in_transition=True)
    frame._start_video = lambda path: started.append(path)
    frame._next_change_at = time.monotonic()     # would be due right now

    frame._tick_video()
    assert started == []                          # nothing moves mid-crossfade
    assert frame._next_change_at > time.monotonic()   # and the slide is held

    frame.renderer.in_transition = False
    frame._tick_video()
    assert started == ["/x/clip.mp4"]


def test_the_clock_never_cuts_a_playing_video_short():
    frame = _frame(interval=1.0)
    frame.renderer = FakeRenderer(_video_slide())
    frame.video = FakePlayer(playing=True)
    frame._video_started = True
    frame._next_change_at = time.monotonic() - 5.0    # long overdue

    frame._tick_video()
    assert frame._next_change_at > time.monotonic()


def test_the_slide_ends_the_moment_the_player_does():
    frame = _frame(interval=600.0)
    frame.renderer = FakeRenderer(_video_slide())
    frame.video = FakePlayer(playing=False)
    frame._video_started = True
    frame._next_change_at = time.monotonic() + 600.0

    frame._tick_video()
    assert frame._next_change_at <= time.monotonic()


def test_a_video_slide_counts_as_moving_until_it_starts():
    frame = _frame()
    frame.renderer = FakeRenderer(_video_slide())
    assert frame._video_pending()
    assert frame._animating(time.monotonic())
    frame._video_started = True
    assert not frame._video_pending()


def test_the_countdown_reports_what_is_left_of_the_film():
    frame = _frame()
    frame.renderer = FakeRenderer(_video_slide())
    frame._video_started = True
    frame.video = FakePlayer(playing=True)
    frame.video.duration, frame.video.position = 90.0, 12.0
    frame.video.loop, frame.video.max_seconds, frame.video.elapsed = False, 0.0, 12.0
    frame._next_change_at = time.monotonic() + 0.5   # pushed, as it is every tick

    assert frame._next_change_in(time.monotonic()) == pytest.approx(78.0)

    frame.video.max_seconds, frame.video.elapsed = 30.0, 12.0
    assert frame._next_change_in(time.monotonic()) == pytest.approx(18.0)


# ---------------------------------------------------------------------------
# Poster frames: the loader thread must always come back
# ---------------------------------------------------------------------------

class FakePosterSink:
    """Records what was pulled, and how patiently."""

    def __init__(self, sample=None):
        self.sample = sample
        self.calls = []

    def emit(self, name, *args):
        self.calls.append((name, args))
        return self.sample


class FakePosterPipeline:
    def __init__(self, sink, change):
        self.sink = sink
        self.change = change
        self.states = []

    def get_by_name(self, name):
        return self.sink

    def set_state(self, state):
        self.states.append(state)

    def get_state(self, timeout):
        return self.change, None, None

    def query_duration(self, fmt):
        return False, 0


def _poster_gst(monkeypatch, sink, change):
    """FakeGst wired up for poster_frame, plus the pipeline it will build."""
    pipeline = FakePosterPipeline(sink, change)
    gst = types.SimpleNamespace(
        SECOND=FakeGst.SECOND,
        State=FakeGst.State,
        Format=FakeGst.Format,
        SeekFlags=FakeGst.SeekFlags,
        MapFlags=FakeGst.MapFlags,
        StateChangeReturn=types.SimpleNamespace(SUCCESS="success", ASYNC="async",
                                               FAILURE="failure"),
        parse_launch=lambda desc: pipeline,
    )
    monkeypatch.setattr(video_module, "_gst_ready", True)
    monkeypatch.setattr(video_module, "Gst", gst)
    return gst, pipeline


def test_a_video_that_never_prerolls_does_not_hang_the_loader(monkeypatch):
    """The blocker: one broken file used to take a loader thread with it.

    ``pull-preroll`` blocks for ever, and the state change that said the file
    was never going to preroll was thrown away.  A handful of such files and
    the frame has no loader threads left.
    """
    sink = FakePosterSink()
    gst, pipeline = _poster_gst(monkeypatch, sink, "failure")

    assert video_module.poster_frame("/x/broken.mp4", (32, 24), timeout=1.0) is None
    assert [name for name, _ in sink.calls] == [], "it waited for a preroll anyway"
    assert pipeline.states[-1] == FakeGst.State.NULL, "the decoder was not released"


def test_the_preroll_pull_always_has_a_deadline(monkeypatch):
    sink = FakePosterSink(sample=None)
    gst, pipeline = _poster_gst(monkeypatch, sink, "success")

    assert video_module.poster_frame("/x/slow.mp4", (32, 24), timeout=2.0) is None
    assert [name for name, _ in sink.calls] == ["try-pull-preroll"]
    assert sink.calls[0][1] == (2 * FakeGst.SECOND,)
    assert pipeline.states[-1] == FakeGst.State.NULL
