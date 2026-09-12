"""Syncthing: the parts that can be tested without installing Syncthing.

Everything here is about the frame's *side* of the arrangement -- the folder
object it asks for, the address it insists on, what it will accept as a device
id, and the shape of the answer the settings page draws.  The act of installing
a package and enabling a service is deliberately not tested: it needs root, a
real systemd and a real network, and pretending otherwise would only test the
mock.

The permission files are tested, though, because they are the part that would
turn a mistake into a security hole rather than a bug report.
"""

from __future__ import annotations

import pathlib

import pytest

from picframe3 import sync
from picframe3.config import Config
from picframe3.wizard import _rule_for, copying_choice

PACKAGING = pathlib.Path(__file__).resolve().parents[1] / "packaging"


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.library.picture_folders = [str(tmp_path / "Pictures")]
    cfg.sync.enabled = True
    return cfg


# -- which folder, and which way -------------------------------------------

def test_the_folder_defaults_to_the_first_picture_folder(config, tmp_path):
    assert sync.folder_path(config) == str(tmp_path / "Pictures")


def test_a_named_folder_wins_and_is_expanded(config):
    config.sync.folder_path = "~/Bilder"
    assert sync.folder_path(config).startswith("/")
    assert sync.folder_path(config).endswith("/Bilder")


def test_the_folder_is_offered_with_the_direction_that_was_asked_for(config):
    config.sync.folder_type = "receiveonly"
    assert sync.folder_payload(config)["type"] == "receiveonly"


def test_an_impossible_direction_falls_back_to_two_way(config):
    """A value from a hand-edited config file must not reach Syncthing."""
    config.sync.folder_type = "sideways"
    assert sync.folder_payload(config)["type"] == "sendreceive"


def test_a_two_way_folder_keeps_a_trash_can(config):
    """The safety net under Remove: a deletion the frame sends is recoverable."""
    versioning = sync.folder_payload(config)["versioning"]
    assert versioning["type"] == "trashcan"
    assert versioning["params"]["cleanoutDays"] == "30"


def test_the_trash_can_can_be_switched_off(config):
    config.sync.versioning_days = 0
    assert sync.folder_payload(config)["versioning"]["type"] == ""


def test_the_devices_already_sharing_it_are_carried_over(config):
    """Re-applying the folder must not unshare it from the phone feeding it."""
    payload = sync.folder_payload(config, ["AAAAAAA-BBBBBBB"])
    assert payload["devices"] == [{"deviceID": "AAAAAAA-BBBBBBB"}]


# -- where Syncthing's own page listens -------------------------------------

def test_the_gui_is_offered_to_the_network_by_default(config):
    assert sync.listen_address(config) == "0.0.0.0:8384"


def test_the_gui_can_be_kept_on_the_frame_itself(config):
    config.sync.gui_lan = False
    assert sync.listen_address(config) == "127.0.0.1:8384"


def test_a_different_port_is_honoured(config):
    config.sync.gui_port = 9384
    assert sync.listen_address(config).endswith(":9384")


# -- reading Syncthing's own configuration ----------------------------------

CONFIG_XML = """<configuration version="37">
  <gui enabled="true" tls="false" debugging="false">
    <address>{address}</address>
    <apikey>SEKRIT</apikey>
  </gui>
</configuration>
"""


def _write_config(tmp_path, monkeypatch, address="0.0.0.0:8384"):
    home = tmp_path / "home"
    (home / ".local/state/syncthing").mkdir(parents=True)
    (home / ".local/state/syncthing/config.xml").write_text(
        CONFIG_XML.format(address=address), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SYNCTHING_HOME", raising=False)
    return home


def test_the_api_key_is_read_from_syncthings_own_config(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    base, key = sync.local_api()
    assert key == "SEKRIT"
    assert base == "http://127.0.0.1:8384", "0.0.0.0 is listened on, not connected to"


def test_the_older_config_location_still_works(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config/syncthing").mkdir(parents=True)
    (home / ".config/syncthing/config.xml").write_text(
        CONFIG_XML.format(address="127.0.0.1:8384"), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SYNCTHING_HOME", raising=False)
    assert sync.local_api() == ("http://127.0.0.1:8384", "SEKRIT")


def test_no_config_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("SYNCTHING_HOME", raising=False)
    assert sync.local_api() == ("", "")
    assert not sync.Client("", "")


def test_a_client_with_nothing_to_talk_to_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("SYNCTHING_HOME", raising=False)
    with pytest.raises(sync.SyncError):
        sync.Client().get("/rest/system/status")


# -- what may be written into another program's configuration ---------------

def test_a_device_id_is_accepted_in_either_spelling():
    dashed = "P56IOI7-MZJNU2Y-IQGDREY-DM2MGTI-MGL3BXN-PQ6W5BM-TBBZ4TJ-XZWICQ2"
    assert sync.normalise_device_id(dashed) == dashed
    assert sync.normalise_device_id(dashed.replace("-", "").lower()) == \
        dashed.replace("-", "")


@pytest.mark.parametrize("bad", [
    "", "hello", "../../etc/passwd", "P56IOI7", "1" * 56,
    "P56IOI7-MZJNU2Y-IQGDREY-DM2MGTI-MGL3BXN-PQ6W5BM-TBBZ4TJ",   # seven groups
])
def test_anything_else_is_refused(bad):
    with pytest.raises(sync.SyncError):
        sync.normalise_device_id(bad)


# -- the answer the settings page draws -------------------------------------

def test_status_is_a_complete_answer_even_with_nothing_installed(config, monkeypatch):
    monkeypatch.setattr(sync.shutil, "which", lambda name: None)
    state = sync.status(config)
    assert state["installed"] is False
    assert state["running"] is False
    assert state["configured"] is True
    assert state["folder"] is None
    assert state["devices"] == [] and state["pending"] == []
    assert state["error"] == ""
    assert state["folder_path"].endswith("Pictures")


def test_status_survives_a_syncthing_that_will_not_answer(config, monkeypatch, tmp_path):
    """Installed, running, and its API refusing: a sentence, not a traceback."""
    _write_config(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "installed", lambda: True)
    monkeypatch.setattr(sync, "is_active", lambda name: True)
    monkeypatch.setattr(sync, "is_enabled", lambda name: True)

    def refuse(self, path, method="GET", body=None):
        raise sync.SyncError("Syncthing is not answering on http://127.0.0.1:8384")

    monkeypatch.setattr(sync.Client, "request", refuse)
    state = sync.status(config)
    assert state["running"] is True
    assert "not answering" in state["error"]


# -- the permission files ---------------------------------------------------

def test_the_polkit_rule_names_only_the_three_units_it_should():
    rule = _rule_for("pi", "60-picframe3-syncthing.rules")
    assert '"syncthing@pi.service"' in rule
    assert '"picframe3-syncthing-on@pi.service"' in rule
    assert '"picframe3-syncthing-off@pi.service"' in rule
    assert 'subject.user !== "pi"' in rule
    body = "\n".join(line for line in rule.splitlines()
                     if not line.lstrip().startswith("//"))
    assert "@USER@" not in body, "a placeholder was left in the rule itself"
    # manage-unit-files would be "enable anything at boot", which is a much
    # larger permission than this feature needs.
    assert "manage-unit-files" not in rule


def test_the_helper_only_knows_on_and_off():
    """The whole safety of the arrangement: the unit takes a user name, and
    the script it runs has no third thing it can be asked to do."""
    script = (PACKAGING / "syncthing-helper.sh").read_text(encoding="utf-8")
    assert "syncthing-helper on|off <user>" in script
    assert "apt-get install -y -q --no-install-recommends syncthing" in script
    assert "STNODEFAULTFOLDER=1" in script, "or Syncthing invents a ~/Sync folder"


def test_the_units_run_the_helper_and_nothing_else():
    for name, verb in (("picframe3-syncthing-on@.service", "on"),
                       ("picframe3-syncthing-off@.service", "off")):
        unit = (PACKAGING / name).read_text(encoding="utf-8")
        assert f"ExecStart={sync.HELPER} {verb} %i" in unit
        assert unit.count("ExecStart=") == 1


def test_the_unit_names_match_what_the_frame_asks_systemd_to_start():
    assert sync.on_unit("pi") == "picframe3-syncthing-on@pi.service"
    assert sync.off_unit("pi") == "picframe3-syncthing-off@pi.service"
    assert sync.unit("pi") == "syncthing@pi.service"


# -- the installer's one question -------------------------------------------

@pytest.mark.parametrize("answer,expected", [
    ("1", (True, False)), ("2", (False, True)), ("3", (True, True)),
    ("4", (False, False)), ("Both", (True, True)), ("syncthing", (True, False)),
    ("", (True, False)), ("what?", (True, False)),
])
def test_the_setup_question_reads_every_sensible_answer(answer, expected):
    assert copying_choice(answer) == expected


# -- and the frame's own housekeeping ---------------------------------------

def test_syncthings_own_folders_are_kept_out_of_the_library():
    """.stversions holds every photograph ever removed; indexing it would put
    them all straight back on the wall."""
    from picframe3.app import PicFrame

    frame = PicFrame.__new__(PicFrame)
    frame.config = Config()
    exclusions = frame._scan_exclusions()
    assert ".stversions" in exclusions
    assert ".stfolder" in exclusions


# -- the web API ------------------------------------------------------------
# The settings page is the reason this feature exists, so the endpoints behind
# it get the same treatment as the filter panel's: a real FastAPI client over
# a real frame, with only Syncthing itself stood in for.

@pytest.fixture
def web(tmp_path, monkeypatch):
    """A real frame, a real FastAPI client, and no Syncthing anywhere."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from picframe3.app import PicFrame
    from picframe3.control.http import HttpServer
    from picframe3.library.db import Library

    cfg = Config()
    cfg.library.picture_folders = [str(tmp_path / "Pictures")]
    cfg.library.database = str(tmp_path / "library.db3")
    frame = PicFrame(cfg)
    frame.library = Library(cfg.library.database)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SYNCTHING_HOME", raising=False)
    yield TestClient(HttpServer(frame, cfg.http).api), frame
    frame.library.close()


def test_the_page_can_ask_where_syncthing_stands(web, monkeypatch):
    client, _ = web
    monkeypatch.setattr(sync.shutil, "which", lambda name: None)
    body = client.get("/api/sync").json()
    assert body["installed"] is False and body["configured"] is False
    assert body["folder_path"].endswith("Pictures")
    assert body["error"] == ""


def test_switching_it_on_writes_the_setting_and_saves_it(web, monkeypatch):
    """Installing a package and enabling a service must survive a restart, so
    the switch writes the config file rather than only the running frame."""
    client, frame = web
    saved = []
    monkeypatch.setattr(frame, "save_config", lambda *a, **k: saved.append(True))
    sent = []
    monkeypatch.setattr(frame.bus, "submit", lambda command: sent.append(command))

    body = client.post("/api/sync/switch", json={"on": True}).json()
    assert body == {"ok": True, "enabled": True}
    assert saved == [True]
    assert sent and sent[0].payload == {"key": "sync.enabled", "value": True}


def test_switching_it_off_goes_through_the_same_setting(web, monkeypatch):
    client, frame = web
    monkeypatch.setattr(frame, "save_config", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(frame.bus, "submit", lambda command: sent.append(command))
    assert client.post("/api/sync/switch", json={"on": False}).json()["enabled"] is False
    assert sent[0].payload == {"key": "sync.enabled", "value": False}


def test_a_device_id_that_is_not_one_is_refused(web):
    """The settings page is open on the LAN and this string is written into
    another program's configuration."""
    client, _ = web
    answer = client.post("/api/sync/device", json={"device_id": "../etc/passwd"})
    assert answer.status_code == 400
    assert "device ID" in answer.json()["detail"]


def test_a_syncthing_that_is_not_there_is_a_503_not_a_traceback(web, monkeypatch):
    """“Keep this folder in step” with nothing to say it to says so."""
    client, _ = web
    monkeypatch.setattr(sync, "installed", lambda: False)
    answer = client.post("/api/sync/folder", json={})
    assert answer.status_code == 503
    assert "not installed" in answer.json()["detail"]


def test_the_folder_button_says_so_when_syncthing_is_not_running(web, monkeypatch):
    client, _ = web
    monkeypatch.setattr(sync, "installed", lambda: True)
    monkeypatch.setattr(sync, "is_active", lambda name: False)
    answer = client.post("/api/sync/folder", json={})
    assert answer.status_code == 503
    assert "not running" in answer.json()["detail"]
