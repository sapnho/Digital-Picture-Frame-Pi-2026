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
