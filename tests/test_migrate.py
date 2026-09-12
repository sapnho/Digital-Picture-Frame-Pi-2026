"""Converting a picframe `configuration.yaml`.

The migrator is what somebody with a frame already on the wall meets first, so
the failure that matters is not a crash: it is a setting that looks carried
over and is not, leaving the new frame quietly behaving differently from the
old one.  These tests cover the two that bite -- the language the dates come
out in, and picframe's habit of leaving `show_text` empty and switching the
text on from Home Assistant.
"""

import textwrap

from picframe3.migrate import migrate

OLD = textwrap.dedent("""
    viewer:
      show_text_sz: 80
      show_text_tm: 40.0
      show_text_fm: "%-d. %b %Y"
      show_text: "{show_text}"
      text_justify: "C"
      use_sdl2: True
      display_power: 2
    model:
      pic_dir: "~/Pictures"
      deleted_pictures: "~/DeletedPictures"
      locale: "{locale}"
      time_delay: 200.0
      fade_time: 10.0
    mqtt:
      use_mqtt: True
      server: "192.168.178.40"
      device_id: "picframe"
    http:
      use_http: True
      port: 9000
""")


def _write(tmp_path, *, show_text="", locale="de_DE.utf8"):
    path = tmp_path / "configuration.yaml"
    path.write_text(OLD.format(show_text=show_text, locale=locale), encoding="utf-8")
    return str(path)


def test_the_language_of_the_dates_carries_over(tmp_path):
    """`model.locale` used to be dropped, which turned "12. Sep 2026" into
    "12 Sep 2026" on a German wall with nothing to explain it."""
    config, notes = migrate(_write(tmp_path))
    assert config.viewer.locale == "de_DE.utf8"
    assert not any("locale" in note for note in notes)


def test_the_settings_that_actually_matter_come_across(tmp_path):
    config, _ = migrate(_write(tmp_path))
    assert config.viewer.text_size == 80
    assert config.viewer.text_seconds == 40.0
    assert config.viewer.date_format == "%-d. %b %Y"
    assert config.viewer.text_justify == "C"
    assert config.slideshow.interval == 200.0
    assert config.slideshow.transition_time == 10.0
    assert config.mqtt.host == "192.168.178.40"
    assert config.http.port == 9000


def test_an_empty_show_text_is_explained_rather_than_copied(tmp_path):
    """Empty in picframe usually meant "nothing until I ask", and a frame that
    only copies the emptiness leaves the Show-the-caption button with nothing
    to show."""
    config, notes = migrate(_write(tmp_path, show_text=""))
    assert config.viewer.show_text == ["title", "caption", "date", "location"]
    advice = "\n".join(notes)
    assert "show_text" in advice
    assert "text_seconds: 0" in advice
    assert "peek_seconds" in advice


def test_a_show_text_that_names_elements_is_taken_at_its_word(tmp_path):
    config, notes = migrate(_write(tmp_path, show_text="date location"))
    assert config.viewer.show_text == ["date", "location"]
    assert not any("show_text" in note for note in notes)


def test_the_retired_display_settings_still_say_why(tmp_path):
    _, notes = migrate(_write(tmp_path))
    advice = "\n".join(notes)
    assert "use_sdl2" in advice and "display_power" in advice


def test_writing_the_new_file_round_trips(tmp_path):
    from picframe3.config import Config

    out = tmp_path / "config.yaml"
    migrate(_write(tmp_path), new_path=str(out))
    again = Config.from_file(str(out))
    assert again.viewer.locale == "de_DE.utf8"
    assert again.viewer.text_seconds == 40.0
