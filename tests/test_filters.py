"""Choosing which pictures are in the running.

The selection logic itself has been in the playlist from the start; what these
tests cover is the part that was missing -- being able to *reach* it from the
sofa.  Three surfaces have to agree about one filter: the browser panel, Home
Assistant's boxes, and the frame's own state document.  The failure mode worth
testing for is not a wrong SQL clause, it is a text box in Home Assistant that
silently wipes the date range every time somebody types a place name into it.
"""

import os

import pytest

from picframe3.library.playlist import (
    ANY_OTHER,
    ANYTHING,
    Filters,
    Playlist,
    date_text,
    parse_date,
    split_tags,
)

# -- reading what a person typed -------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("2024-07-14", "2024-07-14"),      # Home Assistant and the browser
    ("14.07.2024", "2024-07-14"),      # somebody typing by hand in Germany
    ("2024/07/14", "2024-07-14"),
    ("", ""),                           # an empty box means no limit
    ("   ", ""),
    ("not a date", ""),
])
def test_a_date_is_understood_however_it_arrives(text, expected):
    assert date_text(parse_date(text)) == expected


def test_an_upper_limit_means_the_whole_of_that_day():
    """Midnight would quietly drop everything taken during the last day."""
    start = parse_date("2024-07-14")
    end = parse_date("2024-07-14", end_of_day=True)
    assert end - start == pytest.approx(86399, abs=1)


def test_a_timestamp_from_an_older_filter_still_works():
    assert parse_date(1720915200.0) == 1720915200.0


def test_tags_arrive_as_a_line_of_text_or_as_a_list():
    assert split_tags("holiday, france") == ["holiday", "france"]
    assert split_tags(" holiday ;france\nitaly ") == ["holiday", "france", "italy"]
    assert split_tags(["holiday", "holiday"]) == ["holiday"]
    assert split_tags("") == []


# -- patching --------------------------------------------------------------

def test_setting_one_filter_leaves_the_others_alone():
    """The whole reason a patch and not a replacement: Home Assistant sends
    one text box at a time."""
    f = Filters().merged({"tags": "holiday", "date_from": "2024-01-01"})
    f = f.merged({"location": "France"})
    assert f.tags == ["holiday"]
    assert date_text(f.date_from) == "2024-01-01"
    assert f.location_contains == "France"


def test_the_switch_moves_the_tags_between_any_and_all():
    f = Filters().merged({"tags": "holiday, france"})
    assert f.tags_any == ["holiday", "france"] and f.tags_all == []
    f = f.merged({"tags_match_all": "on"})
    assert f.tags_all == ["holiday", "france"] and f.tags_any == []
    assert f.tags == ["holiday", "france"]          # same tags, either way
    f = f.merged({"tags_match_all": False})
    assert f.tags_any == ["holiday", "france"] and f.tags_all == []


def test_a_filter_restored_from_disk_knows_which_way_it_was_set():
    """tags_all with the switch off would ask for all of them and say it was
    asking for any of them."""
    assert Filters(tags_all=["a", "b"]).tags_match_all is True


def test_a_filter_survives_a_round_trip_through_the_state_document():
    f = Filters().merged({"tags": "a, b", "tags_match_all": True,
                          "location": "Rome", "date_to": "2024-07-14"})
    again = Filters.from_dict(f.as_dict())
    assert again.as_dict() == f.as_dict()


def test_the_dropdowns_word_for_everything_is_not_a_search_term():
    f = Filters().merged({"folder": ANYTHING})
    assert f.subfolder == ""
    assert Filters().merged({"folder": ANY_OTHER}).subfolder == ""


def test_reset_clears_the_lot():
    f = Filters().merged({"tags": "a", "location": "Rome", "date_from": "2020-01-01"})
    assert f.active
    assert not f.merged({"reset": True}).active


def test_an_empty_box_clears_just_that_one():
    f = Filters().merged({"tags": "a", "location": "Rome"})
    f = f.merged({"location": ""})
    assert f.location_contains == "" and f.tags == ["a"]


def test_it_can_say_what_it_is_showing():
    f = Filters().merged({"tags": "a, b", "tags_match_all": True, "location": "Rome"})
    assert "a and b" in f.describe() and "Rome" in f.describe()
    assert Filters().describe() == "everything"


# -- against a real index --------------------------------------------------

@pytest.fixture
def tagged(library, photo_dir):
    """Two of the pictures carry tags, one of them a place as well."""
    for name, tags, place in (
        ("2024/exif.jpg", ["holiday", "mountains"], "Chamonix, France"),
        ("2024/img01.jpg", ["holiday"], "Rome, Italy"),
    ):
        path = str(photo_dir / name)
        record = library.by_path(path)
        meta = record.as_meta()
        meta.tags = tags
        stat = os.stat(path)
        file_id = library.upsert(meta, mtime=stat.st_mtime, size=stat.st_size)
        library.set_location(file_id, place)   # as the geocoder fills it in
    return library


def test_the_count_is_available_before_the_filter_is_applied(tagged):
    """So the panel can count down while you type instead of making you apply
    a filter to the wall to find out it matches nothing."""
    playlist = Playlist(tagged, persist=False)
    everything = playlist.size
    assert playlist.count_for(Filters().merged({"tags": "holiday"})) == 2
    assert playlist.count_for(Filters().merged({"tags": "nonesuch"})) == 0
    assert playlist.size == everything          # nothing was applied


def test_any_of_the_tags_or_all_of_them(tagged):
    playlist = Playlist(tagged, persist=False)
    both = Filters().merged({"tags": "holiday, mountains"})
    assert playlist.count_for(both) == 2
    assert playlist.count_for(both.merged({"tags_match_all": True})) == 1


def test_the_place_filter_matches_any_part_of_the_name(tagged):
    playlist = Playlist(tagged, persist=False)
    assert playlist.count_for(Filters().merged({"location": "France"})) == 1
    assert playlist.count_for(Filters().merged({"location": "Rome, Italy"})) == 1
    assert playlist.count_for(Filters().merged({"location": "Iceland"})) == 0


def test_the_grid_and_the_slideshow_draw_from_the_same_set(tagged):
    playlist = Playlist(tagged, filters=Filters().merged({"tags": "holiday"}),
                        persist=False)
    assert len(playlist.selection()) == playlist.size == 2


# -- the frame, and the three surfaces on it -------------------------------

@pytest.fixture
def frame(tmp_path, photo_dir):
    """A frame with a library and no screen: enough to filter with."""
    from picframe3.app import PicFrame
    from picframe3.config import Config
    from picframe3.library.db import Library
    from picframe3.library.scanner import Scanner

    config = Config()
    config.library.picture_folders = [str(photo_dir)]
    config.library.database = str(tmp_path / "library.db3")
    app = PicFrame(config)
    app.library = Library(config.library.database)
    app.scanner = Scanner(app.library, [str(photo_dir)])
    app.scanner.scan()
    for name, tags in (("2024/exif.jpg", ["holiday"]), ("2023/img00.jpg", ["work"])):
        path = str(photo_dir / name)
        record = app.library.by_path(path)
        meta = record.as_meta()
        meta.tags = tags
        stat = os.stat(path)
        file_id = app.library.upsert(meta, mtime=stat.st_mtime, size=stat.st_size)
        app.library.set_location(file_id, "Chamonix, France")
    app.playlist = Playlist(app.library, persist=False)
    app._advance = _noop
    yield app
    app.library.close()


async def _noop(*args, **kwargs):
    return None


def test_the_state_document_carries_the_filter(frame):
    """Every surface has to be able to show the filter it is offering to
    change; a text box that forgets what it is filtering on is worse than no
    box at all."""
    frame.set_filters({"tags": "holiday"})
    filters = frame.state().as_dict()["filters"]
    assert filters["tags_text"] == "holiday"
    assert filters["active"] is True
    assert filters["matching"] == frame.playlist.size == 1


def test_the_folder_dropdown_offers_short_names(frame):
    assert frame.folder_choices() == ["2023", "2024"]


def test_a_folder_typed_by_hand_is_reported_as_other(frame):
    """Rather than as a state Home Assistant's select has no option for, which
    it logs a warning about on every single update."""
    frame.set_filters({"folder": "2024"})
    assert frame.state().as_dict()["filters"]["folder_choice"] == "2024"
    frame.set_filters({"folder": "img0"})
    assert frame.state().as_dict()["filters"]["folder_choice"] == ANY_OTHER


def test_the_folder_filter_and_the_setting_stay_in_step(frame):
    """They are two views of one thing: the settings page and the filter panel
    disagreeing about which folder is showing is a bug report waiting to
    happen."""
    frame.set_filters({"folder": "2024"})
    assert frame.config.library.subfolder == "2024"


@pytest.mark.asyncio
async def test_the_command_bus_patches_rather_than_replaces(frame):
    from picframe3.events import Action, Command

    await frame.handle(Command(Action.SET_FILTERS, {"tags": "holiday"}))
    await frame.handle(Command(Action.SET_FILTERS, {"location": "France"}))
    assert frame.playlist.filters.tags == ["holiday"]
    assert frame.playlist.filters.location_contains == "France"
    await frame.handle(Command(Action.SET_FILTERS, {"reset": True}))
    assert not frame.playlist.filters.active


# -- the web API -----------------------------------------------------------

@pytest.fixture
def client(frame):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from picframe3.control.http import HttpServer

    return TestClient(HttpServer(frame, frame.config.http).api)


def test_the_panel_gets_the_filter_and_everything_it_can_offer(client):
    body = client.get("/api/filters").json()
    assert body["filters"]["active"] is False
    assert {f["name"] for f in body["folders"]} == {"2023", "2024"}
    assert "holiday" in {t["name"] for t in body["tags"]}
    assert "Chamonix, France" in {p["name"] for p in body["locations"]}
    assert body["matching"] == body["total"]


def test_the_panel_can_count_before_it_commits(client, frame):
    body = client.post("/api/filters/preview", json={"tags": "holiday"}).json()
    assert body["matching"] == 1
    assert frame.playlist.filters.active is False      # nothing was applied


def test_posting_a_filter_reaches_the_frame(client, frame):
    assert client.post("/api/filters", json={"tags": "holiday"}).json()["ok"]
    command = frame.bus.drain()[-1]
    assert command.payload == {"tags": "holiday"}


def test_the_grid_can_show_exactly_what_the_slideshow_draws_from(client, frame):
    frame.set_filters({"tags": "holiday"})
    rows = client.get("/api/library/photos?selected=1").json()
    assert len(rows) == 1 and rows[0]["tags"] == ["holiday"]
    assert len(client.get("/api/library/photos").json()) > 1


def test_the_filters_route_is_not_swallowed_by_the_action_shortcut(client):
    """POST /api/<anything> is a command shortcut; /api/filters must win."""
    assert client.post("/api/filters", json={"tags": "x"}).status_code == 200


# -- Home Assistant --------------------------------------------------------

def test_every_filter_template_reads_a_field_the_frame_really_publishes(frame):
    """The check that catches a typo before it becomes a silently empty box in
    Home Assistant."""
    pytest.importorskip("aiomqtt")
    import re

    from picframe3.control.mqtt import MqttBridge

    published = frame.state().as_dict()["filters"]
    for _component, object_id, payload in MqttBridge(frame, frame.config.mqtt) \
            .discovery_entities():
        for key, value in payload.items():
            if not key.endswith("template") or not isinstance(value, str):
                continue
            for field in re.findall(r"value_json\.filters\.(\w+)", value):
                assert field in published, f"{object_id}.{key}: filters.{field}"


def test_no_two_entities_share_an_object_id(frame):
    """They may not: the object id is what names the entity in Home Assistant,
    and "tags" is already what the picture on screen is tagged with."""
    pytest.importorskip("aiomqtt")
    from picframe3.control.mqtt import MqttBridge

    ids = [object_id for _, object_id, _ in
           MqttBridge(frame, frame.config.mqtt).discovery_entities()]
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)


def test_the_folder_dropdown_offers_the_folders_that_exist(frame):
    pytest.importorskip("aiomqtt")
    from picframe3.control.mqtt import MqttBridge

    folder = next(p for _, object_id, p in MqttBridge(frame, frame.config.mqtt)
                  .discovery_entities() if object_id == "folder_filter")
    assert folder["options"] == [ANYTHING, ANY_OTHER, "2023", "2024"]


@pytest.mark.parametrize("topic,payload,expected", [
    ("tags", "holiday, france", {"tags": "holiday, france"}),
    ("tags_match_all", "on", {"tags_match_all": True}),
    ("location", "Rome", {"location": "Rome"}),
    ("date_from", "2024-01-01", {"date_from": "2024-01-01"}),
    ("date_to", "", {"date_to": ""}),
    ("folder_filter", "2024", {"subfolder": "2024"}),
    ("location_filter", "Rome", {"location": "Rome"}),
    ("clear_filters", "press", {"reset": True}),
])
@pytest.mark.asyncio
async def test_each_home_assistant_box_sends_only_itself(frame, topic, payload,
                                                         expected):
    pytest.importorskip("aiomqtt")
    from picframe3.control.mqtt import MqttBridge

    bridge = MqttBridge(frame, frame.config.mqtt)
    await bridge._dispatch(f"{bridge.prefix}/{topic}/set", payload)
    command = frame.bus.drain()[-1]
    assert command.payload == expected
