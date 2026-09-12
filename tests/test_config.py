import pytest

from picframe3.config import Config


def test_defaults_are_complete():
    cfg = Config()
    assert cfg.slideshow.interval > 0
    assert cfg.display.backend == "auto"
    assert cfg.library.picture_folders


def test_dotted_get_and_set():
    cfg = Config()
    cfg.set("slideshow.interval", 42)
    assert cfg.get("slideshow.interval") == 42.0
    assert cfg.get("nothing.here", "fallback") == "fallback"


@pytest.mark.parametrize("raw,expected", [("on", True), ("0", False), ("TRUE", True)])
def test_boolean_strings_are_accepted(raw, expected):
    """MQTT sends 'on'/'off'; YAML sends real booleans.  Both must work."""
    cfg = Config()
    cfg.set("viewer.show_clock", raw)
    assert cfg.get("viewer.show_clock") is expected


def test_optional_list_survives_none():
    cfg = Config.from_dict({"viewer": {"mat_outer_color": None}})
    assert cfg.get("viewer.mat_outer_color") is None
    cfg.set("viewer.mat_outer_color", [220, 215, 205])
    assert cfg.get("viewer.mat_outer_color") == [220, 215, 205]


def test_unknown_keys_do_not_raise(caplog):
    cfg = Config.from_dict({"slideshow": {"interval": 12, "nope": 1}, "bogus": {}})
    assert cfg.get("slideshow.interval") == 12
    assert "nope" in caplog.text


def test_bad_value_keeps_default(caplog):
    cfg = Config.from_dict({"slideshow": {"interval": "not a number"}})
    assert cfg.get("slideshow.interval") == Config().slideshow.interval


def test_save_and_reload_roundtrip(tmp_path):
    cfg = Config()
    cfg.set("slideshow.transition", "zoom")
    cfg.set("library.picture_folders", ["/a", "/b"])
    path = cfg.save(str(tmp_path / "c.yaml"))
    again = Config.from_file(path)
    assert again.get("slideshow.transition") == "zoom"
    assert again.get("library.picture_folders") == ["/a", "/b"]


def test_set_rejects_unknown_key():
    with pytest.raises(KeyError):
        Config().set("slideshow.does_not_exist", 1)
