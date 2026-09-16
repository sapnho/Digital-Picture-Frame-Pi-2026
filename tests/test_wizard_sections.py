"""``picframe3 setup`` run again: ask what to change, then only about that."""

from __future__ import annotations

import pytest

from picframe3 import wizard
from picframe3.config import Config


@pytest.mark.parametrize("answer, expected", [
    ("", []),
    ("6", ["dates"]),
    ("dates", ["dates"]),
    ("locale", ["dates"]),
    ("5, 1", ["pictures", "look"]),
    ("2 6", ["copying", "dates"]),
    ("all", ["all"]),
    (["mqtt", "geo"], ["mqtt", "geo"]),
    ("9", None),
    ("colour", None),
])
def test_parse_sections(answer, expected):
    assert wizard.parse_sections(answer) == expected


def test_summaries_say_what_is_set():
    config = Config()
    config.viewer.locale = "de_DE.UTF-8"
    config.mqtt.enabled = True
    config.mqtt.host = "ha.local"
    config.mqtt.device_name = "PictureFrame26"
    config.power.schedule = {"all": ["22:30-06:45"]}
    lines = wizard.section_summaries(config, share=False)
    assert lines["dates"] == "Deutsch (de_DE)"
    assert "PictureFrame26" in lines["mqtt"]
    assert "22:30" in lines["look"] and "06:45" in lines["look"]
    assert set(lines) == {key for key, _t, _n in wizard.SECTIONS}


def test_night_off_reads_the_existing_range():
    assert wizard.night_off({"all": ["22:30-06:45"]}) == ("22:30", "06:45")
    assert wizard.night_off({}) == ("23:00", "07:00")


def _answers(monkeypatch, *replies):
    queue = list(replies)
    monkeypatch.setattr(wizard, "_read", lambda prompt: queue.pop(0))
    return queue


def _config_file(tmp_path, **viewer):
    config = Config()
    config.library.picture_folders = [str(tmp_path / "pics")]
    config.mqtt.enabled = True
    config.mqtt.host = "ha.local"
    config.mqtt.device_name = "PictureFrame26"
    for key, value in viewer.items():
        setattr(config.viewer, key, value)
    target = tmp_path / "config.yaml"
    config.save(str(target))
    return target


def test_changing_only_the_date_language(tmp_path, monkeypatch):
    target = _config_file(tmp_path)
    monkeypatch.setattr(wizard, "_tty", lambda: True)
    monkeypatch.setattr(wizard, "samba_configured", lambda: False)
    monkeypatch.setattr(wizard, "install_date_languages", lambda c: True)
    monkeypatch.setattr(wizard, "restart_frame", lambda user: None)
    monkeypatch.setattr(wizard, "date_language_choices", lambda c, built=None: [
        ("", "System default"), ("de_DE.UTF-8", "Deutsch (de_DE)")])
    for name in ("check_hardware", "choose_pictures", "setup_copying",
                 "setup_web", "setup_mqtt", "setup_look", "setup_geo"):
        monkeypatch.setattr(wizard, name, lambda *a, _n=name, **k:
                            pytest.fail(f"{_n} should not have been asked"))
    queue = _answers(monkeypatch, "6", "2")

    assert wizard.run(str(target)) == 0
    assert queue == []
    saved = Config.load(str(target))
    assert saved.viewer.locale == "de_DE.UTF-8"
    assert saved.mqtt.device_name == "PictureFrame26"       # untouched


def test_named_part_skips_the_list(tmp_path, monkeypatch):
    target = _config_file(tmp_path, locale="de_DE.UTF-8")
    monkeypatch.setattr(wizard, "_tty", lambda: True)
    monkeypatch.setattr(wizard, "install_date_languages", lambda c: True)
    monkeypatch.setattr(wizard, "restart_frame", lambda user: None)
    monkeypatch.setattr(wizard, "date_language_choices", lambda c, built=None: [
        ("", "System default"), ("de_DE.UTF-8", "Deutsch"), ("fr_FR.UTF-8", "Français")])
    queue = _answers(monkeypatch, "fr_FR")

    assert wizard.run(str(target), sections=["dates"]) == 0
    assert queue == []
    assert Config.load(str(target)).viewer.locale == "fr_FR.UTF-8"


def test_return_changes_nothing(tmp_path, monkeypatch, capsys):
    target = _config_file(tmp_path)
    before = target.read_text()
    monkeypatch.setattr(wizard, "_tty", lambda: True)
    monkeypatch.setattr(wizard, "samba_configured", lambda: False)
    _answers(monkeypatch, "")
    assert wizard.run(str(target)) == 0
    assert target.read_text() == before
    assert "Nothing changed" in capsys.readouterr().out


def test_unknown_part_is_refused(tmp_path, monkeypatch):
    target = _config_file(tmp_path)
    monkeypatch.setattr(wizard, "_tty", lambda: True)
    assert wizard.run(str(target), sections=["colour"]) == 2


@pytest.mark.parametrize("answer, expected", [
    ("", "de_DE.UTF-8"), ("1", ""), ("3", "fr_FR.UTF-8"), ("sv_SE", "sv_SE.UTF-8"),
    ("sv-SE", "sv_SE.UTF-8"), ("system", ""), ("12", None), ("german", None),
])
def test_date_language_answer(answer, expected):
    choices = [("", "System"), ("de_DE.UTF-8", "Deutsch"), ("fr_FR.UTF-8", "Français")]
    assert wizard.date_language_answer(answer, choices, "de_DE.UTF-8") == expected


def test_rerun_keeps_existing_answers_as_defaults(tmp_path, monkeypatch):
    """Return through Home Assistant and the look keeps what was there."""
    target = _config_file(tmp_path, show_clock=True)
    config = Config.load(str(target))
    config.power.schedule = {"all": ["22:30-06:45"]}
    config.slideshow.kenburns = True
    _answers(monkeypatch, *[""] * 20)
    monkeypatch.setattr(wizard, "ask_secret", lambda q, **k: pytest.fail("password asked"))
    wizard.setup_mqtt(config)
    wizard.setup_look(config)
    assert config.mqtt.enabled and config.mqtt.device_name == "PictureFrame26"
    assert config.viewer.show_clock and config.slideshow.kenburns
    assert config.power.schedule == {"all": ["22:30-06:45"]}
