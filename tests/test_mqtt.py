"""Home Assistant discovery.

The frame publishes one JSON state document and lets every entity read it with
a template, which is cheap on the wire but easy to get subtly wrong: a typo in
a template shows up in Home Assistant as a silently empty sensor, not as an
error.  These tests read the templates and check that everything they reach for
is really in the document.
"""

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

    def state(self) -> State:
        return State()


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
