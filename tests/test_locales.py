"""Building the date languages, and offering only the ones that are built.

`viewer.locale` can only switch to a locale the system has generated, and a
fresh Raspberry Pi OS Lite generates one -- so `de_DE.UTF-8` failed quietly
and the captions stayed English.  `picframe3 setup` (which the installer runs
on every install and update) now builds the configured one -- and nothing
else -- and the settings page says which languages are built and which are not.
"""

import datetime as dt
import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest

from picframe3 import locales, uischema, wizard
from picframe3.config import Config

LOCALE_GEN = """\
# This file lists locales that you wish to have built. You can find a list
# of valid supported locales at /usr/share/i18n/SUPPORTED, and you can add
# user defined locales to /usr/local/share/i18n/SUPPORTED. If you change
# this file, you need to rerun locale-gen.

# de_DE ISO-8859-1
# de_DE.UTF-8 UTF-8
en_GB.UTF-8 UTF-8
# fr_FR.UTF-8 UTF-8
"""

SUPPORTED = """\
de_DE.UTF-8 UTF-8
de_DE ISO-8859-1
en_GB.UTF-8 UTF-8
fr_FR.UTF-8 UTF-8
sv_SE.UTF-8 UTF-8
"""


# ------------------------------------------------------------------ names
@pytest.mark.parametrize("spelling", ["de_DE.UTF-8", "de_DE.utf8", "de_DE", "de-DE",
                                      "de_de.UTF-8", " de_DE.UTF-8 "])
def test_every_way_of_writing_a_language_is_the_same_language(spelling):
    assert locales.key(spelling) == "de_de.utf8"
    assert locales.canonical(spelling) == "de_DE.UTF-8"


def test_a_modifier_survives():
    assert locales.canonical("ca_ES@valencia") == "ca_ES.UTF-8@valencia"


def test_only_what_was_asked_for_is_built_once_each():
    """Nothing is built up front: the configured language and named extras."""
    assert locales.wanted("de_DE.utf8", "sv_SE, da_DK de_DE") == [
        "de_DE.UTF-8", "sv_SE.UTF-8", "da_DK.UTF-8"]
    assert locales.wanted("") == []
    assert locales.wanted("C") == []


# ------------------------------------------------------------- locale.gen
def test_a_commented_language_is_switched_on():
    plan = locales.plan(LOCALE_GEN, SUPPORTED, ["de_DE.UTF-8"])
    assert plan.enabled == ["de_DE.UTF-8"]
    assert "\nde_DE.UTF-8 UTF-8\n" in plan.text


def test_the_iso_8859_line_is_not_mistaken_for_utf8():
    """`de_DE ISO-8859-1` has no encoding in its name; read carelessly it
    passes for `de_DE`, which means UTF-8 here, and gets switched on instead."""
    plan = locales.plan(LOCALE_GEN, SUPPORTED, ["de_DE"])
    assert "# de_DE ISO-8859-1" in plan.text
    assert "\nde_DE.UTF-8 UTF-8\n" in plan.text


def test_one_already_on_is_left_alone_and_nothing_is_rewritten():
    plan = locales.plan(LOCALE_GEN, SUPPORTED, ["en_GB.UTF-8"])
    assert plan.already == ["en_GB.UTF-8"]
    assert not plan.changed
    assert plan.text == LOCALE_GEN


def test_a_language_with_no_line_is_added_from_supported():
    plan = locales.plan(LOCALE_GEN, SUPPORTED, ["sv_SE"])
    assert plan.enabled == ["sv_SE.UTF-8"]
    assert plan.text.endswith("# fr_FR.UTF-8 UTF-8\nsv_SE.UTF-8 UTF-8\n")


def test_a_typo_is_reported_and_never_written():
    """A bad line would make locale-gen fail for every other language too."""
    plan = locales.plan(LOCALE_GEN, SUPPORTED, ["zz_ZZ.UTF-8", "fr_FR"])
    assert plan.unknown == ["zz_ZZ.UTF-8"]
    assert "zz_ZZ" not in plan.text
    assert plan.enabled == ["fr_FR.UTF-8"]


def test_the_header_comments_survive():
    plan = locales.plan(LOCALE_GEN, SUPPORTED, ["de_DE", "fr_FR"])
    assert plan.text.startswith("# This file lists locales that you wish to have built.")
    assert "# this file, you need to rerun locale-gen." in plan.text


# ------------------------------------------------------------ what is built
def _locale_a(*names):
    return lambda *a, **k: SimpleNamespace(stdout="\n".join(names) + "\n", returncode=0)


def test_only_real_utf8_languages_are_listed():
    run = _locale_a("C", "C.utf8", "POSIX", "de_DE.utf8", "de_DE", "en_GB.utf8",
                    "de_DE.UTF-8")
    assert locales.available(run) == ["de_DE.UTF-8", "en_GB.UTF-8"]


def test_no_locale_command_means_nothing_listed():
    def boom(*a, **k):
        raise FileNotFoundError("locale")
    assert locales.available(boom) == []


def test_the_menu_starts_with_the_system_default():
    items = locales.options("", built=[])
    assert items[0]["name"] == ""
    assert items[0]["label"].startswith("System default")


def test_common_languages_are_offered_but_marked_not_built():
    """Nothing is built up front, so the menu must still let the owner find
    German -- and must say that choosing it is not the whole story."""
    with mock.patch.object(locales, "_sample", return_value=""):
        items = locales.options("", built=["en_GB.UTF-8"])
    by_name = {i["name"]: i["label"] for i in items}
    assert by_name["en_GB.UTF-8"] == "English (en_GB)"
    assert "not built yet" in by_name["de_DE.UTF-8"]
    assert "picframe3 setup --yes" in by_name["de_DE.UTF-8"]
    assert "en_GB.UTF-8" in [i["name"] for i in items[:2]], "built ones come first"


def test_the_menu_keeps_the_owners_spelling_selected():
    """The config says `de_DE.utf8` (migrated from picframe), `locale -a` says
    `de_DE.UTF-8`.  Offering only the second would silently change the value
    the page shows as selected."""
    with mock.patch.object(locales, "_sample", return_value=""):
        items = locales.options("de_DE.utf8", built=["de_DE.UTF-8", "en_GB.UTF-8"])
    names = [i["name"] for i in items]
    assert names[:3] == ["", "de_DE.utf8", "en_GB.UTF-8"]
    assert names.count("de_DE.utf8") == 1 and "de_DE.UTF-8" not in names
    assert items[1]["label"] == "Deutsch (de_DE)"


def test_a_configured_language_that_is_not_built_is_marked_not_swapped():
    with mock.patch.object(locales, "_sample", return_value=""):
        items = locales.options("sv_SE.UTF-8", built=["en_GB.UTF-8"])
    entry = next(i for i in items if i["name"] == "sv_SE.UTF-8")
    assert "not built yet" in entry["label"]
    assert "picframe3 setup --yes" in entry["label"]


def test_each_entry_carries_a_sample_date_in_the_frames_format():
    with mock.patch.object(locales, "_sample", return_value="16. September 2026") as s:
        text = locales.label("de_DE.UTF-8", "%-d. %B %Y", dt.date(2026, 9, 16))
    assert text == "Deutsch (de_DE) — 16. September 2026"
    s.assert_called_once_with("de_DE.UTF-8", "%-d. %B %Y", "2026-09-16")


def test_the_sample_never_changes_this_processs_locale():
    import locale

    before = locale.setlocale(locale.LC_TIME)
    locales.label("de_DE.UTF-8", "%B", dt.date(2026, 3, 1))
    assert locale.setlocale(locale.LC_TIME) == before


# ---------------------------------------------------------------- settings
def test_the_setting_is_a_menu_of_built_languages():
    config = Config()
    config.viewer.locale = "de_DE.UTF-8"
    with mock.patch.object(locales, "available", return_value=["de_DE.UTF-8"]), \
         mock.patch.object(locales, "_sample", return_value=""):
        built = uischema.schema(config)
    field = next(f for s in built["sections"] for f in s["fields"]
                 if f["key"] == "viewer.locale")
    assert field["kind"] == "select"
    assert field["options"] == "locales"
    assert [o["name"] for o in built["options"]["locales"]][:2] == ["", "de_DE.UTF-8"]


def test_the_web_server_can_hand_the_menu_in():
    handed = [{"name": "", "label": "System default"}]
    with mock.patch.object(locales, "options") as build:
        built = uischema.schema(Config(), extra_options={"locales": handed})
    build.assert_not_called()
    assert built["options"]["locales"] == handed


# ------------------------------------------------------------------- setup
@pytest.fixture
def system(tmp_path, monkeypatch):
    gen = tmp_path / "locale.gen"
    gen.write_text(LOCALE_GEN, encoding="utf-8")
    sup = tmp_path / "SUPPORTED"
    sup.write_text(SUPPORTED, encoding="utf-8")
    monkeypatch.setattr(locales, "LOCALE_GEN", str(gen))
    monkeypatch.setattr(locales, "SUPPORTED", str(sup))
    monkeypatch.setattr(wizard.shutil, "which",
                        lambda name, path=None: "/usr/sbin/locale-gen")
    monkeypatch.delenv(locales.ENV_EXTRA, raising=False)

    state = SimpleNamespace(built=["en_GB.UTF-8"], writes=[], commands=[], gen=gen)

    def write_as_root(text, destination, mode="0644"):
        state.writes.append(destination)
        gen.write_text(text, encoding="utf-8")
        return True

    def run_root(command, **kwargs):
        state.commands.append(command)
        if command == ["locale-gen"]:
            state.built = [line.split()[0] for line in gen.read_text().splitlines()
                           if line and not line.startswith("#")]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(wizard, "write_as_root", write_as_root)
    monkeypatch.setattr(wizard, "run_root", run_root)
    monkeypatch.setattr(locales, "available", lambda: list(state.built))
    monkeypatch.setattr(wizard, "say", lambda *a, **k: None)
    return state


def test_setup_builds_the_configured_language(system):
    config = Config()
    config.viewer.locale = "de_DE.UTF-8"
    assert wizard.install_date_languages(config) is True
    assert system.commands == [["locale-gen"]]
    assert "de_DE.UTF-8" in system.built


def test_setup_builds_nothing_nobody_asked_for(system):
    assert wizard.install_date_languages(Config()) is True
    assert system.writes == [] and system.commands == []
    assert system.built == ["en_GB.UTF-8"]


def test_setup_with_everything_built_touches_nothing(system):
    """An update that changes nothing must not rewrite /etc or spend a minute
    in locale-gen."""
    config = Config()
    config.viewer.locale = "sv_SE.UTF-8"
    wizard.install_date_languages(config)
    system.writes.clear()
    system.commands.clear()
    wizard.install_date_languages(config)
    assert system.writes == [] and system.commands == []


def test_setup_takes_extras_from_the_environment(system, monkeypatch):
    monkeypatch.setenv(locales.ENV_EXTRA, "sv_SE")
    wizard.install_date_languages(Config())
    assert "sv_SE.UTF-8" in system.built


def test_setup_never_touches_the_systems_own_language(system):
    config = Config()
    config.viewer.locale = "de_DE.UTF-8"
    wizard.install_date_languages(config)
    assert set(system.writes) <= {str(system.gen)}
    assert all("update-locale" not in c and "localectl" not in c
               for c in system.commands)


def test_setup_without_locale_gen_is_a_note_not_a_failure(system, monkeypatch):
    monkeypatch.setattr(wizard.shutil, "which", lambda name, path=None: None)
    config = Config()
    config.viewer.locale = "de_DE.UTF-8"
    assert wizard.install_date_languages(config) is False
    assert system.commands == []
