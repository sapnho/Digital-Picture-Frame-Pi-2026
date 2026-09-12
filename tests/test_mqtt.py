"""Home Assistant discovery.

The frame publishes one JSON state document and lets every entity read it with
a template, which is cheap on the wire but easy to get subtly wrong: a typo in
a template shows up in Home Assistant as a silently empty sensor, not as an
error.  These tests read the templates and check that everything they reach for
is really in the document.
"""

import asyncio
import io
import json
import re

import pytest

from picframe3 import health
from picframe3.config import Config
from picframe3.control.mqtt import MqttBridge
from picframe3.events import State
from picframe3.network import NetworkWatch

pytest.importorskip("aiomqtt")


class _StubApp:
    """Just enough of PictureFrameApp for discovery to be built."""

    def __init__(self):
        self.config = Config()
        self.started = 0.0
        self.bus = _RecordingBus()

    def state(self) -> State:
        return State()


class _RecordingBus:
    """Catches what the bridge asks the frame to do, instead of doing it."""

    def __init__(self):
        self.commands = []

    def submit(self, command):
        self.commands.append(command)
        return True

    def subscribe(self, listener):
        return lambda: None


@pytest.fixture
def bridge():
    app = _StubApp()
    return MqttBridge(app, app.config.mqtt)


def _sample_state() -> dict:
    """A state document with a picture on screen, as the frame publishes it."""
    state = State().as_dict()
    state["current"] = {
        "basename": "lobster.jpg", "folder": "/photos/Normandy", "title": "Lunch",
        "caption": "", "location": "Carteret, France", "latitude": 49.37,
        "longitude": -1.79, "taken_at": 1753524000.0,
        "taken_iso": "2026-07-26T12:00:00+02:00", "tags": ["france", "holiday"],
        "tags_text": "france, holiday", "make": "Apple", "model": "iPhone 15",
        "lens": None, "f_number": 1.6, "exposure_time": "1/2300s", "iso": 50,
        "focal_length": 5.96, "shown_as": "mat", "has_position": True,
    }
    # As Library.stats() fills it in.
    # The real thing, whatever this machine can measure: readings it cannot
    # take are None, and the templates have to survive that too.
    state["health"] = health.sample("")
    state["network"] = NetworkWatch(target="192.168.1.1").snapshot()
    state["library"] = {"files": 31, "videos": 0, "hidden": 0, "shown_min": 2,
                        "shown_max": 3, "never_shown": 0, "oldest": None,
                        "newest": None, "bytes": 0, "folders": 2, "tags": 5,
                        "db_path": "/tmp/i.db", "db_bytes": 0}
    state["removed"] = {"count": 4, "folder": "/deleted",
                        "journal": "/deleted/removals.jsonl",
                        "last_basename": "IMG_4312.jpg",
                        "last_removed_iso": "2026-09-12T14:03:11+02:00",
                        "last_folder": "/photos/Normandy"}
    return state


def test_every_entity_has_its_own_unique_id(bridge):
    ids = [payload["unique_id"] for _, _, payload in bridge.discovery_entities()]
    assert len(ids) == len(set(ids)), "Home Assistant would drop the duplicates"


def test_the_picture_sensors_are_all_there(bridge):
    """The frame publishes the date, the place and the tags as their own
    entities, not only as attributes nobody can put on a dashboard."""
    names = {object_id for _, object_id, _ in bridge.discovery_entities()}
    assert {"taken", "place", "tags", "camera", "title", "folder"} <= names


def test_no_template_reads_a_field_the_frame_does_not_publish(bridge):
    """The check that actually catches typos."""
    state = _sample_state()
    for _, object_id, payload in bridge.discovery_entities():
        for key, value in payload.items():
            if not key.endswith("template") or not isinstance(value, str):
                continue
            for field in re.findall(r"value_json\.current\.(\w+)", value):
                assert field in state["current"], f"{object_id}.{key}: current.{field}"
            for field in re.findall(r"value_json\.library\.(\w+)", value):
                assert field in state["library"], f"{object_id}.{key}: library.{field}"
            for field in re.findall(r"value_json\.removed\.(\w+)", value):
                assert field in state["removed"], f"{object_id}.{key}: removed.{field}"
            for field in re.findall(r"value_json\.health\.(\w+)", value):
                assert field in state["health"], f"{object_id}.{key}: health.{field}"
            for field in re.findall(r"value_json\.(\w+)", value):
                assert field in state, f"{object_id}.{key}: {field}"


def test_the_place_sensor_carries_coordinates_for_the_map_card(bridge):
    place = next(p for _, object_id, p in bridge.discovery_entities()
                 if object_id == "place")
    template = place["json_attributes_template"]
    assert "latitude" in template and "longitude" in template


def test_discovery_payloads_are_json(bridge):
    for _, _, payload in bridge.discovery_entities():
        json.dumps({k: v for k, v in payload.items() if v is not None})


# -- the Pi's own vital signs ----------------------------------------------

def test_the_health_sensors_are_announced(bridge):
    names = {object_id for _, object_id, _ in bridge.discovery_entities()}
    assert {"cpu_temp", "cpu_load", "memory", "disk_free", "undervoltage"} <= names


def test_every_health_sensor_is_diagnostic(bridge):
    """They belong under the device's diagnostics, not among the controls
    somebody put on a dashboard."""
    health_entities = {o for _, o in MqttBridge.HEALTH_ENTITIES}
    for _component, object_id, payload in bridge.discovery_entities():
        if object_id in health_entities:
            assert payload["entity_category"] == "diagnostic", object_id


def test_a_numeric_health_template_is_guarded_against_a_missing_reading(bridge):
    """An unguarded template pushes the string "None" into a temperature
    sensor the day the thermal zone disappears, and Home Assistant logs an
    error every thirty seconds for ever."""
    for _, object_id, payload in bridge.discovery_entities():
        template = payload.get("value_template", "")
        if "value_json.health" not in template or object_id == "undervoltage":
            continue
        assert "is not none" in template, object_id


def test_switching_the_reporting_off_takes_the_sensors_out_again(bridge):
    bridge.app.config.health.enabled = False
    names = {object_id for _, object_id, _ in bridge.discovery_entities()}
    assert not names & {o for _, o in MqttBridge.HEALTH_ENTITIES}
    # ...and every one of them is withdrawn rather than left unavailable.
    assert set(bridge.retired_entities()) == set(MqttBridge.HEALTH_ENTITIES)


def test_nothing_is_withdrawn_while_the_reporting_is_on(bridge):
    assert bridge.retired_entities() == []


# -- what has been taken out of the library --------------------------------

def test_the_removed_sensors_are_announced(bridge):
    """The pi3d frame needed an external watchdog script to count a folder.
    The frame already knows, so it publishes the number itself."""
    names = {object_id for _, object_id, _ in bridge.discovery_entities()}
    assert {"removed", "last_removed"} <= names


def test_the_removed_sensor_says_where_the_last_one_came_from(bridge):
    """A count alone answers "how many"; the attributes answer "which one, and
    from where" -- which is the question somebody actually asks."""
    sensor = next(p for _, object_id, p in bridge.discovery_entities()
                  if object_id == "removed")
    template = sensor["json_attributes_template"]
    assert "came_from" in template and "journal" in template


# -- the picture itself ----------------------------------------------------

def test_the_picture_is_offered_as_an_image_entity(bridge):
    """A dashboard can show what is on the frame without a camera to set up."""
    image = next(p for component, _, p in bridge.discovery_entities()
                 if component == "image")
    assert image["image_topic"] == bridge.image_topic
    assert image["content_type"] == "image/jpeg"
    # Home Assistant's image schema has no state_topic, and one unknown key
    # makes it reject the whole entity.
    assert "state_topic" not in image
    assert "value_template" not in image


class _FakeClient:
    def __init__(self):
        self.published: list[tuple[str, object]] = []

    async def publish(self, topic, payload, **_kwargs):
        self.published.append((topic, payload))


def _picture(tmp_path, name="one.jpg", colour=(200, 60, 40)):
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", (2400, 1600), colour).save(path, "JPEG")
    return {"id": 1, "path": str(path), "mtime": 1.0, "is_video": False}


async def test_the_picture_is_sent_once_and_then_only_when_it_changes(bridge, tmp_path):
    client = _FakeClient()
    current = _picture(tmp_path)

    await bridge._publish_image(client, current)
    assert [topic for topic, _ in client.published] == [bridge.image_topic]
    assert client.published[0][1][:2] == b"\xff\xd8", "not a JPEG"

    await bridge._publish_image(client, current)
    assert len(client.published) == 1, "a state update must not re-send the picture"

    later = _picture(tmp_path, "two.jpg", (20, 90, 160))
    later["id"] = 2
    await bridge._publish_image(client, later)
    assert len(client.published) == 2


async def test_the_picture_is_scaled_to_the_configured_width(bridge, tmp_path):
    from PIL import Image

    bridge.config.image_width = 320
    client = _FakeClient()
    await bridge._publish_image(client, _picture(tmp_path))
    sent = Image.open(io.BytesIO(client.published[0][1]))
    assert max(sent.size) == 320


async def test_turning_it_off_sends_nothing(bridge, tmp_path):
    bridge.config.publish_image = False
    client = _FakeClient()
    await bridge._publish_image(client, _picture(tmp_path))
    assert client.published == []


async def test_a_picture_that_cannot_be_read_is_not_retried_forever(bridge, tmp_path):
    """A broken file must not be re-rendered on every state update."""
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a photograph")
    client = _FakeClient()
    current = {"id": 9, "path": str(broken), "mtime": 1.0, "is_video": False}
    await bridge._publish_image(client, current)
    await bridge._publish_image(client, current)
    assert client.published == []


# ==========================================================================
# Inbound: the commands Home Assistant actually sends
# ==========================================================================

def _dispatch(bridge, name, payload):
    """One message on one entity's `.../set` topic, as the broker delivers it."""
    asyncio.run(bridge._dispatch(bridge.entity_topic(name), payload))
    return [(c.action.value, c.payload) for c in bridge.app.bus.commands]


def _command_topics(bridge):
    """Every topic an announced entity publishes to, with its entity name."""
    out = {}
    for _, object_id, payload in bridge.discovery_entities():
        topic = payload.get("command_topic")
        if topic:
            out[object_id] = topic
    return out


def test_zero_and_one_on_the_display_topic_still_mean_off_and_on(bridge):
    """The payload every hand-written automation has always used.

    Reading `0` as a brightness left the screen black with Home Assistant
    showing the light as on -- and because the brightness is a saved setting,
    that zero went into the config file and survived the restart.
    """
    from picframe3.events import Action

    for payload, expected in (("0", Action.DISPLAY_OFF), ("1", Action.DISPLAY_ON),
                              ("off", Action.DISPLAY_OFF), ("on", Action.DISPLAY_ON)):
        bridge.app.bus.commands.clear()
        asyncio.run(bridge._dispatch(bridge.entity_topic("display"), payload))
        actions = [c.action for c in bridge.app.bus.commands]
        assert actions == [expected], f"{payload!r} -> {actions}"

    # A real slider drag is still a brightness, and still turns the screen on.
    bridge.app.bus.commands.clear()
    asyncio.run(bridge._dispatch(bridge.entity_topic("display"), "128"))
    actions = [c.action for c in bridge.app.bus.commands]
    assert actions == [Action.DISPLAY_ON, Action.BRIGHTNESS]


def test_every_announced_button_and_control_is_actually_handled(bridge):
    """The regression: the Restart button was announced, Home Assistant drew
    it, and `_dispatch` had never heard of it — so pressing it logged
    "unhandled MQTT topic" and did nothing. Held for every entity at once, so
    the next announced control cannot repeat it."""
    bridge.app.config.http.allow_delete = True    # or Remove is refused, not unhandled
    unhandled = []
    for object_id, topic in _command_topics(bridge).items():
        bridge.app.bus.commands.clear()
        asyncio.run(bridge._dispatch(topic, "press"))
        if not bridge.app.bus.commands:
            unhandled.append(object_id)
    assert unhandled == [], f"announced but not handled: {unhandled}"


def test_the_restart_button_restarts_the_frame(bridge):
    assert _dispatch(bridge, "restart", "press") == [("restart", {})]


def test_the_shutdown_button_powers_the_pi_off(bridge):
    """Announced on purpose, unlike `quit`: it is the frame's power button."""
    assert _dispatch(bridge, "shutdown", "press") == [("shutdown", {})]


def test_every_announced_command_topic_is_one_the_bridge_subscribes_to(bridge):
    """`<prefix>/#` used to cover everything by accident. With two narrow
    filters, an entity announced on a topic outside them would be a control
    that silently does nothing."""
    allowed = set(bridge.subscriptions())
    for object_id, topic in _command_topics(bridge).items():
        ok = topic in allowed or (
            topic.startswith(bridge.prefix + "/") and topic.endswith("/set")
            and topic.count("/") == bridge.prefix.count("/") + 2)
        assert ok, f"{object_id} publishes to {topic}, which nothing listens on"


def test_the_bridge_no_longer_subscribes_to_the_whole_prefix(bridge):
    assert f"{bridge.prefix}/#" not in bridge.subscriptions()
    assert bridge.subscriptions() == [f"{bridge.prefix}/cmd", f"{bridge.prefix}/+/set"]


# -- the brightness slider --------------------------------------------------

def test_the_light_sends_its_brightness_rather_than_the_word_on(bridge):
    """`command_on_template` was the constant "on", so Home Assistant rendered
    the slider value and then threw it away: the dashboard moved, the frame
    did not."""
    jinja2 = pytest.importorskip("jinja2")
    payload = dict(bridge.discovery_entities()[0][2])
    assert payload["unique_id"].endswith("_display")
    template = jinja2.Template(payload["command_on_template"])
    assert template.render(brightness=128).strip() == "128"
    assert template.render().strip() == "on", "a plain switch-on still says on"


def test_a_rendered_brightness_reaches_the_frame_as_a_brightness(bridge):
    got = _dispatch(bridge, "display", "128")
    assert ("brightness", {"value": pytest.approx(128 / 255)}) in got
    assert ("display_on", {}) in got, "a brightness implies the screen is on"


def test_the_word_on_and_the_word_off_still_work(bridge):
    assert _dispatch(bridge, "display", "on") == [("display_on", {})]
    bridge.app.bus.commands.clear()
    assert _dispatch(bridge, "display", "off") == [("display_off", {})]


# -- what the broker may not ask for ---------------------------------------

def test_quit_is_refused_however_it_is_dressed_up(bridge, caplog):
    """Anything on the broker could publish this, and stopping the frame is
    not something a picture-frame entity ever needs to do."""
    with caplog.at_level("WARNING"):
        asyncio.run(bridge._dispatch(bridge.command_topic, '{"action": "quit"}'))
    assert bridge.app.bus.commands == []
    assert "refusing quit" in caplog.text


def test_delete_over_mqtt_follows_the_same_switch_as_the_web_ui(bridge, caplog):
    bridge.app.config.http.allow_delete = False
    with caplog.at_level("WARNING"):
        asyncio.run(bridge._dispatch(bridge.command_topic, "delete"))
    assert bridge.app.bus.commands == []
    assert "allow_delete" in caplog.text

    bridge.app.config.http.allow_delete = True
    asyncio.run(bridge._dispatch(bridge.command_topic, "delete"))
    assert [c.action.value for c in bridge.app.bus.commands] == ["delete"]


def test_a_setting_over_mqtt_cannot_point_the_frame_at_a_startup_file(bridge, caplog):
    with caplog.at_level("WARNING"):
        asyncio.run(bridge._dispatch(
            bridge.command_topic,
            '{"action": "set_config", "key": "logging.file", "value": "~/.bashrc"}'))
    assert bridge.app.bus.commands == []
    assert "refusing a setting" in caplog.text


def test_an_ordinary_setting_over_mqtt_still_goes_through(bridge):
    asyncio.run(bridge._dispatch(
        bridge.command_topic,
        '{"action": "set_config", "key": "slideshow.interval", "value": 90}'))
    assert [c.action.value for c in bridge.app.bus.commands] == ["set_config"]
