"""The settings page is generated, so the generator is what gets tested.

The point of generating it is that a setting can never be missing from the web
interface by omission. These tests hold that line: every field in the config
appears, every one has a control and an explanation, and no secret is ever
echoed back over an API that is usually open on a home network.
"""

import json
from dataclasses import fields, is_dataclass

import pytest

from picframe3 import uischema
from picframe3.config import Config


@pytest.fixture
def built():
    return uischema.schema(Config())


def _every_key():
    config = Config()
    for section in fields(Config):
        obj = getattr(config, section.name)
        if is_dataclass(obj):
            for f in fields(obj):
                yield f"{section.name}.{f.name}"


def test_every_setting_reaches_the_settings_page(built):
    shown = {f["key"] for s in built["sections"] for f in s["fields"]}
    assert shown == set(_every_key())


def test_every_setting_is_explained(built):
    """A control with no sentence under it is a control nobody dares touch."""
    silent = [f["key"] for s in built["sections"] for f in s["fields"] if not f["note"]]
    assert silent == []


def test_every_section_is_introduced(built):
    for section in built["sections"]:
        assert section["label"] and section["prose"], section["name"]


def test_every_control_can_be_drawn(built):
    known = {"bool", "int", "number", "text", "secret", "select", "datalist",
             "csv", "numbers", "json", "pick", "pick-string", "tiers"}
    for section in built["sections"]:
        for f in section["fields"]:
            assert f["kind"] in known, f
            if f["kind"] == "select":
                assert f["choices"] or f["options"], f["key"]
            if f["kind"] in ("pick", "pick-string"):
                assert f["options"] in built["options"], f["key"]


def test_every_option_list_the_controls_ask_for_exists(built):
    for section in built["sections"]:
        for f in section["fields"]:
            if not f["options"]:
                continue
            assert f["options"] in built["options"], f["key"]
            # `folders` is filled in by the running frame from the library, so
            # it is legitimately empty here.
            if f["options"] != "folders":
                assert built["options"][f["options"]], f["key"]


def test_a_select_offers_the_value_it_currently_has(built):
    for section in built["sections"]:
        for f in section["fields"]:
            if f["kind"] != "select" or f["value"] in (None, ""):
                continue
            offered = ([str(c) for c in f["choices"]] if f["choices"]
                       else [o["name"] for o in built["options"][f["options"]]])
            assert str(f["value"]) in offered, f"{f['key']} = {f['value']!r}"


def test_a_labelled_option_list_wins_over_the_bare_choices(built):
    """`sync.folder_type` is both validated against CHOICES and offered as a
    labelled list. The page must draw the labels, not `sendreceive`."""
    field = next(f for s in built["sections"] for f in s["fields"]
                 if f["key"] == "sync.folder_type")
    assert field["kind"] == "select"
    assert field["choices"] is None, "the browser would draw the internal names"
    assert field["options"] == "sync-directions"
    labels = {o["name"]: o["label"] for o in built["options"]["sync-directions"]}
    assert labels["sendreceive"].startswith("Send & receive")
    # …and the frame still refuses anything that is not one of them.
    assert uischema.CHOICES["sync.folder_type"] == [
        "sendreceive", "receiveonly", "sendonly"]


def test_the_schema_survives_a_round_trip_as_json(built):
    assert json.loads(json.dumps(built)) == built


def test_secrets_are_never_sent_to_the_browser(built):
    config = Config()
    config.mqtt.password = "hunter2"
    config.http.auth_password = "hunter2"
    built = uischema.schema(config)
    for section in built["sections"]:
        for f in section["fields"]:
            if f["key"] in uischema.SECRETS:
                assert f["value"] is None and f["default"] is None
                assert f["is_set"] is True
    assert "hunter2" not in json.dumps(built)


def test_get_config_masks_the_passwords():
    config = Config()
    config.mqtt.password = "hunter2"
    masked = uischema.redact(config.as_dict())
    assert masked["mqtt"]["password"] == uischema.REDACTED
    assert masked["http"]["auth_password"] == ""      # not set stays empty
    assert config.mqtt.password == "hunter2", "redact must not mutate the config"


def test_writing_a_masked_password_back_changes_nothing():
    """A read-modify-write of the whole config must not blank the password."""
    config = Config()
    config.mqtt.password = "hunter2"
    masked = uischema.redact(config.as_dict())
    assert masked["mqtt"]["password"] == uischema.REDACTED
    # app._apply_setting refuses this exact value; assert the contract it relies on.
    assert uischema.REDACTED != "" and "mqtt.password" in uischema.SECRETS


def test_restart_only_settings_are_marked_as_such(built):
    live = {f["key"] for s in built["sections"] for f in s["fields"] if f["live"]}
    assert "viewer.mat_style" in live
    assert "slideshow.interval" in live
    assert "display.brightness" in live
    assert "mqtt.host" not in live, "changing the broker needs a reconnect"
    assert "http.port" not in live


def test_the_docs_generator_reads_the_same_definitions():
    """One source for the reference and the page, or they drift."""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "tools" / "gen_config_docs.py"
    spec = importlib.util.spec_from_file_location("gen_config_docs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.NOTES is uischema.NOTES
    assert module.PROSE is uischema.SECTION_PROSE


# -- what a restart is for -------------------------------------------------

def test_needs_restart_is_the_one_answer_everything_uses():
    """The frame, the settings page and the reference must agree about which
    settings a restart is for, so they all ask the same function."""
    assert uischema.needs_restart("mqtt.host")
    assert uischema.needs_restart("http.port")
    assert uischema.needs_restart("library.picture_folders")
    assert not uischema.needs_restart("viewer.mat_style")
    assert not uischema.needs_restart("slideshow.interval")
    assert not uischema.needs_restart("geo.detail")
    assert not uischema.needs_restart("library.subfolder")   # a live exception
    assert not uischema.needs_restart("display.brightness")


def test_the_page_badges_exactly_what_needs_a_restart(built):
    for section in built["sections"]:
        for f in section["fields"]:
            assert f["live"] is not uischema.needs_restart(f["key"]), f["key"]


def test_an_unknown_key_is_treated_as_needing_a_restart():
    """Safer to over-warn than to leave someone waiting for a change that
    never comes."""
    assert uischema.needs_restart("something.new")


# -- the front door: jobs, not fields --------------------------------------
# The cards are a hand-written list over generated settings, which is exactly
# the arrangement that rots: a field is renamed, the card still names the old
# key, and the control silently disappears. These tests are what stops that.

def test_every_job_names_settings_that_exist(built):
    known = {f["key"] for s in built["sections"] for f in s["fields"]}
    for job in built["jobs"]:
        missing = [k for k in job["fields"] if k not in known]
        assert missing == [], f"{job['name']}: {missing}"


def test_no_setting_is_edited_from_two_cards():
    """Two cards offering the same switch is two answers to one question."""
    seen = {}
    for job in uischema.JOBS:
        for key in job["fields"]:
            assert key not in seen, f"{key} is on both {seen.get(key)} and {job['name']}"
            seen[key] = job["name"]


def test_every_card_has_a_kicker_a_title_and_a_sentence(built):
    for job in built["jobs"]:
        assert job["kicker"] and job["title"], job["name"]
        assert job["state"], f"{job['name']} says nothing about the current state"


def test_every_card_points_at_a_real_section(built):
    sections = {s["name"] for s in built["sections"]}
    for job in built["jobs"]:
        assert job["section"] in sections, job["name"]


def test_the_sections_no_card_claims_are_the_rarely_needed_ones(built):
    """Whatever is left over becomes a link of its own on the page, so this
    test is really asking: is anything important left with no way in?"""
    claimed = {job["section"] for job in built["jobs"]}
    left = {s["name"] for s in built["sections"]} - claimed
    assert left == {"http", "logging", "health", "network"}


def test_a_summary_survives_an_awkward_configuration():
    """Empty folders, no caption, a geocoder switched on with no contact: the
    sentence may be sad but it must not be an exception."""
    config = Config()
    config.library.picture_folders = []
    config.viewer.show_text = []
    config.viewer.fit = "cover"
    config.geo.enabled = True
    config.geo.contact = ""
    config.mqtt.enabled = True
    config.mqtt.host = ""
    config.input.touch = config.input.keyboard = config.input.mouse = False
    config.power.enabled = True
    config.power.schedule = {"all": ["22:30-07:00"], "sat": ["23:30-09:00"]}
    for job in uischema.jobs(config):
        assert job["state"], job["name"]


def test_a_summary_says_what_the_frame_is_actually_doing():
    config = Config()
    config.slideshow.interval = 35
    config.slideshow.transition_time = 10.5
    config.display.brightness = 1.0
    config.input.gpio_buttons = {"next": 17}
    said = {job["name"]: job["state"] for job in uischema.jobs(config)}
    assert "35 seconds each" in said["pacing"]
    assert "10.5 s" in said["pacing"]
    # A fraction reads as a fraction: "brightness 1", and it looks like a count.
    assert "brightness 1.0" in said["screen"]
    # `str.capitalize` would have made this "1 gpio button".
    assert "1 GPIO button" in said["buttons"]


def test_the_jobs_travel_with_the_schema_as_json(built):
    assert json.loads(json.dumps(built["jobs"])) == built["jobs"]
