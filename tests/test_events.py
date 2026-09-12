import types

from picframe3.events import Action, Bus, Command, State


def test_parses_bare_action():
    assert Command.parse("next").action is Action.NEXT


def test_parses_json_with_payload():
    command = Command.parse('{"action":"brightness","value":0.4}')
    assert command.action is Action.BRIGHTNESS
    assert command.payload["value"] == 0.4


def test_rejects_unknown():
    assert Command.parse("frobnicate") is None
    assert Command.parse('{"action":"frobnicate"}') is None
    assert Command.parse(b"pause").action is Action.PAUSE


def test_state_is_json_serialisable():
    import json

    json.dumps(State().as_dict())


def test_bus_fans_state_out():
    bus = Bus()
    seen = []
    unsubscribe = bus.subscribe(seen.append)
    bus.publish(State(paused=True))
    assert seen[-1].paused
    unsubscribe()
    bus.publish(State(paused=False))
    assert len(seen) == 1


# -- restarting ------------------------------------------------------------

def test_restart_is_a_command_like_any_other():
    """So the web UI, MQTT, a keyboard and Home Assistant all reach it through
    the same bus rather than each growing its own path."""
    assert Command.parse("restart").action is Action.RESTART
    assert Command.parse({"action": "restart"}).action is Action.RESTART


def test_request_restart_stops_the_loop_and_asks_to_come_back():
    import asyncio

    from picframe3.app import PicFrame

    frame = types.SimpleNamespace(
        _stop=asyncio.Event(), restart_requested=False)
    PicFrame.request_restart(frame)
    assert frame.restart_requested is True
    assert frame._stop.is_set(), "the render loop has to actually end"


def test_systemd_is_detected_from_the_environment(monkeypatch):
    """Under systemd a clean exit IS the restart; by hand it has to re-exec."""
    from picframe3.app import PicFrame

    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    assert PicFrame.under_systemd() is False
    monkeypatch.setenv("INVOCATION_ID", "3f2c")
    assert PicFrame.under_systemd() is True


def test_the_state_carries_what_is_waiting_on_a_restart():
    fresh = State()
    assert fresh.restart_required == [] and fresh.unsaved_changes is False
    assert State().restart_required is not fresh.restart_required, \
        "a shared mutable default would leak between frames"
