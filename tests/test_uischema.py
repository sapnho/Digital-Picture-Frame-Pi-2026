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
    known = {"bool", "int", "number", "text", "secret", "select",
             "csv", "numbers", "json", "pick", "pick-string"}
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
            if f["options"]:
                assert built["options"][f["options"]], f["key"]


def test_a_select_offers_the_value_it_currently_has(built):
    for section in built["sections"]:
        for f in section["fields"]:
            if f["kind"] != "select" or f["value"] in (None, ""):
                continue
            offered = ([str(c) for c in f["choices"]] if f["choices"]
                       else [o["name"] for o in built["options"][f["options"]]])
            assert str(f["value"]) in offered, f"{f['key']} = {f['value']!r}"


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
