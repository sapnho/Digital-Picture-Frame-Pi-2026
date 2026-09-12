"""Home Assistant discovery.

The frame publishes one JSON state document and lets every entity read it with
a template, which is cheap on the wire but easy to get subtly wrong: a typo in
a template shows up in Home Assistant as a silently empty sensor, not as an
error.  These tests read the templates and check that everything they reach for
is really in the document.
"""

import json
import re

import pytest

from picframe3.config import Config
from picframe3.control.mqtt import MqttBridge
from picframe3.events import State

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
    state["library"] = {"files": 31, "videos": 0, "hidden": 0, "shown_min": 2,
                        "shown_max": 3, "never_shown": 0, "oldest": None,
                        "newest": None, "bytes": 0, "folders": 2, "tags": 5,
                        "db_path": "/tmp/i.db", "db_bytes": 0}
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
