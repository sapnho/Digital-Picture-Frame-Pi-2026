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


def test_wizard_finds_a_terminal_when_stdin_is_a_pipe(monkeypatch):
    """`curl … | bash` makes stdin the script, not a terminal.

    The wizard must still reach the person through /dev/tty, or the
    recommended install silently takes every default and never asks
    anything — which is how a frame ends up with no network share.
    """
    import io
    import sys

    from picframe3 import wizard

    monkeypatch.setattr(wizard, "_TERMINAL", None)
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped input\n"))
    fake_tty = io.StringIO("from the terminal\n")
    monkeypatch.setattr(wizard, "_TERMINAL", fake_tty)

    assert wizard._tty() is True
    assert wizard._read("prompt: ") == "from the terminal"


def test_wizard_reports_no_terminal_when_there_is_none(monkeypatch):
    import io
    import sys

    from picframe3 import wizard

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(wizard, "_TERMINAL", False)
    assert wizard._tty() is False
