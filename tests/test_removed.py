"""The removal journal.

The thing being protected here is provenance.  A picture leaves the library
and the database row goes with it -- title, date taken, place, tags -- so if
the journal is not written before ``forget()``, or is written without those
fields, what is left is a folder of anonymous JPEGs.  Most of these tests are
about that ordering and that content; the rest are about putting one back.
"""

import json
import os

import pytest
from PIL import Image

from picframe3.library.removed import JOURNAL_NAME, RemovalLog


@pytest.fixture
def record(library, photo_dir):
    """A real indexed picture, with metadata worth losing."""
    path = str(photo_dir / "2024" / "exif.jpg")
    rec = library.by_path(path)
    meta = rec.as_meta()
    meta.title = "Alpine morning"
    meta.tags = ["mountains", "sunrise"]
    stat = os.stat(path)
    library.upsert(meta, mtime=stat.st_mtime, size=stat.st_size)
    return library.by_path(path)


@pytest.fixture
def log(tmp_path):
    return RemovalLog(str(tmp_path / "deleted"))


# -- writing ---------------------------------------------------------------

def test_a_removal_is_written_down(log, record):
    log.record(record, "exif.jpg", source="http")
    entries = log.entries()
    assert len(entries) == 1
    assert entries[0]["original_path"] == record.path
    assert entries[0]["stored_as"] == "exif.jpg"
    assert entries[0]["source"] == "http"


def test_the_journal_keeps_what_the_database_row_was_about_to_lose(log, record):
    """The whole reason this exists: forget() deletes the only copy."""
    entry = log.record(record, "exif.jpg")
    assert entry["title"] == "Alpine morning"
    assert set(entry["tags"]) == {"mountains", "sunrise"}
    assert entry["taken_at"] == record.taken_at
    assert entry["taken_iso"].startswith("2024-07-14")
    assert entry["folder"] == record.folder
    assert entry["width"] and entry["height"]


def test_it_is_one_json_object_per_line(log, record):
    log.record(record, "a.jpg")
    log.record(record, "b.jpg")
    text = open(log.path, encoding="utf-8").read()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert [json.loads(ln)["stored_as"] for ln in lines] == ["a.jpg", "b.jpg"]


def test_the_journal_sits_with_the_files_it_describes(log, record):
    log.record(record, "a.jpg")
    assert os.path.basename(log.path) == JOURNAL_NAME
    assert os.path.dirname(log.path) == log.folder


def test_a_missing_folder_is_created(tmp_path, record):
    log = RemovalLog(str(tmp_path / "nowhere" / "deeper"))
    log.record(record, "a.jpg")
    assert os.path.exists(log.path)


def test_a_journal_that_cannot_be_written_does_not_raise(tmp_path, record):
    """Bookkeeping must never be what stops the frame removing a picture."""
    blocker = tmp_path / "in-the-way"
    blocker.write_text("not a folder")
    log = RemovalLog(str(blocker))       # the folder cannot even be created
    log.record(record, "a.jpg")          # no exception
    assert log.count() == 0


# -- reading ---------------------------------------------------------------

def test_counts_and_latest_ignore_what_has_been_put_back(log, record):
    log.record(record, "a.jpg")
    log.record(record, "b.jpg")
    assert log.count() == 2
    assert log.latest()["stored_as"] == "b.jpg"
    log.mark_restored("b.jpg", "/pictures/b.jpg")
    assert log.count() == 1
    assert log.latest()["stored_as"] == "a.jpg"


def test_a_restored_entry_stays_in_the_journal(log, record):
    """A journal that forgets the undone removals is not a journal."""
    log.record(record, "a.jpg")
    log.mark_restored("a.jpg", "/pictures/a.jpg")
    assert log.entries() == log.entries(include_restored=True)
    assert len(log.entries()) == 1
    assert log.entries(include_restored=False) == []
    entry = log.entries()[0]
    assert entry["restored_to"] == "/pictures/a.jpg"
    assert entry["restored_iso"]


def test_a_torn_line_costs_one_entry_not_the_journal(log, record):
    """A power cut mid-append is the realistic corruption here."""
    log.record(record, "a.jpg")
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write('{"stored_as": "b.jpg", "orig')       # cut off
    log.record(record, "c.jpg")
    assert [e["stored_as"] for e in log.entries()] == ["a.jpg", "c.jpg"]


def test_the_cache_notices_a_new_line(log, record):
    log.record(record, "a.jpg")
    assert log.count() == 1
    other = RemovalLog(log.folder)                # a second reader
    assert other.count() == 1
    log.record(record, "b.jpg")
    assert other.count() == 2


def test_summary_is_what_the_state_document_carries(log, record):
    assert log.summary()["count"] == 0
    log.record(record, "exif.jpg")
    summary = log.summary()
    assert summary["count"] == 1
    assert summary["last_basename"] == record.basename
    assert summary["last_folder"] == record.folder
    assert summary["journal"] == log.path


def test_newest_first_is_available_for_the_page(log, record):
    log.record(record, "a.jpg")
    log.record(record, "b.jpg")
    assert [e["stored_as"] for e in log.entries(newest_first=True)] == ["b.jpg", "a.jpg"]


# -- the app: remove, then put back ----------------------------------------

@pytest.fixture
def frame(tmp_path, photo_dir):
    """A PicFrame with a library and no screen, enough for remove/restore."""
    import asyncio

    from picframe3.app import PicFrame
    from picframe3.config import Config

    config = Config()
    config.library.picture_folders = [str(photo_dir)]
    config.library.database = str(tmp_path / "library.db3")
    config.library.deleted_folder = str(tmp_path / "deleted")
    app = PicFrame(config)

    from picframe3.library.db import Library
    from picframe3.library.playlist import Playlist
    from picframe3.library.removed import RemovalLog
    from picframe3.library.scanner import Scanner

    app.library = Library(config.library.database)
    app.removals = RemovalLog(config.library.deleted_folder)
    app.scanner = Scanner(app.library, [str(photo_dir)])
    app.scanner.scan()
    app.playlist = Playlist(app.library)
    app._advance = _noop                      # no renderer in this test
    yield app
    app.library.close()
    del asyncio


async def _noop(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_removing_writes_the_journal_before_forgetting_the_row(frame, photo_dir):
    path = str(photo_dir / "2024" / "exif.jpg")
    frame.current = [frame.library.by_path(path)]
    await frame._delete_current(source="http")

    assert frame.library.by_path(path) is None          # gone from the index
    assert not os.path.exists(path)                     # gone from the folder
    entry = frame.removals.latest()
    assert entry["original_path"] == path
    assert entry["taken_iso"]                           # the row's metadata survived
    assert os.path.exists(os.path.join(frame.removals.folder, entry["stored_as"]))


@pytest.mark.asyncio
async def test_a_second_removal_of_the_same_name_gets_its_own_entry(frame, photo_dir):
    for folder in ("2023", "2024"):
        target = photo_dir / folder / "same.jpg"
        Image.new("RGB", (60, 40), (9, 9, 9)).save(target)
    frame.scanner.scan()
    for folder in ("2023", "2024"):
        path = str(photo_dir / folder / "same.jpg")
        frame.current = [frame.library.by_path(path)]
        await frame._delete_current()
    stored = [e["stored_as"] for e in frame.removals.entries()]
    assert stored == ["same.jpg", "same-1.jpg"]
    origins = {e["original_path"] for e in frame.removals.entries()}
    assert len(origins) == 2                            # both are traceable


@pytest.mark.asyncio
async def test_putting_one_back_returns_it_to_the_folder_it_came_from(frame, photo_dir):
    path = str(photo_dir / "2023" / "img00.jpg")
    frame.current = [frame.library.by_path(path)]
    await frame._delete_current()
    stored_as = frame.removals.latest()["stored_as"]

    result = await frame._restore_removed(stored_as)
    assert result["ok"] and result["path"] == path
    assert os.path.exists(path)
    assert frame.library.by_path(path) is not None      # indexed again
    assert frame.removals.count() == 0
    assert frame.removals.entries()[0]["restored_to"] == path


@pytest.mark.asyncio
async def test_putting_one_back_recreates_a_folder_that_has_since_gone(frame, photo_dir):
    path = str(photo_dir / "2023" / "img00.jpg")
    frame.current = [frame.library.by_path(path)]
    await frame._delete_current()
    for leftover in os.listdir(photo_dir / "2023"):
        os.unlink(photo_dir / "2023" / leftover)
    os.rmdir(photo_dir / "2023")

    result = await frame._restore_removed(frame.removals.latest()["stored_as"])
    assert result["ok"] and os.path.exists(path)


@pytest.mark.asyncio
async def test_a_taken_name_does_not_overwrite_anything(frame, photo_dir):
    path = str(photo_dir / "2023" / "img00.jpg")
    frame.current = [frame.library.by_path(path)]
    await frame._delete_current()
    Image.new("RGB", (10, 10), (1, 2, 3)).save(path)     # something else took it
    before = open(path, "rb").read()

    result = await frame._restore_removed(frame.removals.latest()["stored_as"])
    assert result["ok"] and result["moved"] is True
    assert result["path"].endswith("img00-restored.jpg")
    assert open(path, "rb").read() == before             # untouched


@pytest.mark.asyncio
async def test_restoring_something_that_is_not_there_says_so(frame):
    result = await frame._restore_removed("nothing.jpg")
    assert result["ok"] is False and "nothing.jpg" in result["error"]


@pytest.mark.asyncio
async def test_restoring_a_file_deleted_from_the_folder_by_hand_says_so(frame, photo_dir):
    path = str(photo_dir / "2023" / "img00.jpg")
    frame.current = [frame.library.by_path(path)]
    await frame._delete_current()
    stored_as = frame.removals.latest()["stored_as"]
    os.unlink(os.path.join(frame.removals.folder, stored_as))

    result = await frame._restore_removed(stored_as)
    assert result["ok"] is False and "no longer" in result["error"]


# -- the web API -----------------------------------------------------------

@pytest.fixture
def client(frame, photo_dir):
    """The real HTTP surface over the frame above."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from picframe3.control.http import HttpServer

    return TestClient(HttpServer(frame, frame.config.http).api)


@pytest.fixture
async def one_removed(frame, photo_dir):
    path = str(photo_dir / "2023" / "img00.jpg")
    frame.current = [frame.library.by_path(path)]
    await frame._delete_current(source="http")
    return frame.removals.latest()


@pytest.mark.asyncio
async def test_the_api_lists_what_was_removed(client, one_removed):
    rows = client.get("/api/removed").json()
    assert len(rows) == 1
    assert rows[0]["original_path"] == one_removed["original_path"]
    assert rows[0]["on_disk"] is True
    assert rows[0]["source"] == "http"


@pytest.mark.asyncio
async def test_restored_entries_are_out_of_the_way_unless_asked_for(
        client, frame, one_removed):
    client.post(f"/api/removed/{one_removed['stored_as']}/restore").raise_for_status()
    assert client.get("/api/removed").json() == []
    kept = client.get("/api/removed?include_restored=true").json()
    assert len(kept) == 1 and kept[0]["restored_to"]


@pytest.mark.asyncio
async def test_the_api_serves_a_thumbnail_of_something_no_longer_indexed(
        client, one_removed):
    response = client.get(f"/api/removed/{one_removed['stored_as']}/thumb")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_the_journal_can_be_downloaded_whole(client, one_removed):
    response = client.get("/api/removed/journal")
    assert response.status_code == 200
    assert one_removed["original_path"] in response.text


@pytest.mark.asyncio
async def test_a_name_from_outside_the_folder_is_refused(client, one_removed):
    """``stored_as`` comes off the URL, so it has to bounce off something."""
    for attempt in ("../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "nope.jpg"):
        assert client.get(f"/api/removed/{attempt}/thumb").status_code in (404, 422)


@pytest.mark.asyncio
async def test_restoring_through_the_api_puts_the_file_back(
        client, frame, one_removed):
    response = client.post(f"/api/removed/{one_removed['stored_as']}/restore")
    assert response.status_code == 200
    assert response.json()["path"] == one_removed["original_path"]
    assert os.path.exists(one_removed["original_path"])


@pytest.mark.asyncio
async def test_restoring_something_unknown_is_a_404(client):
    assert client.post("/api/removed/ghost.jpg/restore").status_code == 404


@pytest.mark.asyncio
async def test_the_state_document_carries_the_count(frame, one_removed):
    assert frame.state().as_dict()["removed"]["count"] == 1


# -- emptying the trash ----------------------------------------------------
# Deleting for good is the one thing on this tab that really deletes, and the
# thing being protected is the same as everywhere else in this file: the file
# goes, the line about it does not.  A journal that forgets what it was asked
# to delete would answer "what happened to that photograph?" with silence,
# which is the failure the journal exists to prevent.

def test_purging_marks_the_line_and_leaves_it_in_the_file(log, record):
    log.record(record, "a.jpg")
    assert log.mark_purged("a.jpg") == ["a.jpg"]

    assert log.count() == 0                  # not in the trash any more
    assert log.entries() == []               # and out of the way by default
    kept = log.entries(include_purged=True)
    assert len(kept) == 1 and kept[0]["purged_at"]
    assert kept[0]["title"] == "Alpine morning"    # the provenance survived
    assert kept[0]["purged_iso"]
    assert log.purged_count() == 1
    assert json.loads(open(log.path, encoding="utf-8").read().strip())["purged_at"]


def test_purging_is_not_something_that_happens_twice(log, record):
    log.record(record, "a.jpg")
    first = log.mark_purged("a.jpg")
    assert log.mark_purged("a.jpg") == []
    stamp = log.entries(include_purged=True)[0]["purged_at"]
    log.mark_purged("a.jpg", when=stamp + 500)
    assert log.entries(include_purged=True)[0]["purged_at"] == stamp
    assert first == ["a.jpg"]


def test_something_put_back_cannot_then_be_purged(log, record):
    """The file is back in the library; purging must not claim otherwise."""
    log.record(record, "a.jpg")
    log.mark_restored("a.jpg", "/pictures/2024/a.jpg")
    assert log.mark_purged("a.jpg") == []
    entry = log.entries(include_restored=True)[0]
    assert entry["restored_at"] and not entry.get("purged_at")


def test_a_purged_picture_is_not_offered_for_restoring(log, record):
    log.record(record, "a.jpg")
    log.mark_purged("a.jpg")
    assert log.find("a.jpg") is None
    assert log.mark_restored("a.jpg", "/pictures/anywhere.jpg") is False


def test_a_whole_batch_is_one_rewrite(log, record):
    """Four hundred pictures must not mean four hundred journal rewrites."""
    names = [f"p{n}.jpg" for n in range(5)]
    for name in names:
        log.record(record, name)
    rewrites = []
    original = log._rewrite
    log._rewrite = lambda entries: (rewrites.append(1), original(entries))[1]
    assert sorted(log.mark_purged(names)) == sorted(names)
    assert len(rewrites) == 1
    assert log.count() == 0 and log.purged_count() == 5


def test_a_rewrite_does_not_drop_the_lines_it_is_not_changing(log, record):
    """entries() hides purged lines by default; a rewrite must not obey that."""
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        log.record(record, name)
    log.mark_purged("a.jpg")
    log.mark_restored("b.jpg", "/pictures/2024/b.jpg")
    log.mark_purged("c.jpg")

    everything = log.entries(include_restored=True, include_purged=True)
    assert len(everything) == 3
    by_name = {e["stored_as"]: e for e in everything}
    assert by_name["a.jpg"]["purged_at"] and by_name["c.jpg"]["purged_at"]
    assert by_name["b.jpg"]["restored_at"]


def test_summary_counts_the_trash_and_what_left_it(log, record):
    log.record(record, "a.jpg")
    log.record(record, "b.jpg")
    log.mark_purged("a.jpg")
    summary = log.summary()
    assert summary["count"] == 1 and summary["purged"] == 1
    assert summary["last_basename"] == record.basename


def test_purging_nothing_is_not_an_error(log, record):
    assert log.mark_purged([]) == []
    assert log.mark_purged("never-existed.jpg") == []


# -- the app ---------------------------------------------------------------

@pytest.fixture
async def two_removed(frame, photo_dir):
    """Two pictures in the trash, from two different folders."""
    out = []
    for folder, name in (("2023", "img00.jpg"), ("2024", "exif.jpg")):
        frame.current = [frame.library.by_path(str(photo_dir / folder / name))]
        await frame._delete_current(source="http")
        out.append(frame.removals.latest())
    return out


@pytest.mark.asyncio
async def test_purging_one_deletes_the_file_and_keeps_the_note(frame, two_removed):
    first = two_removed[0]
    on_disk = os.path.join(frame.removals.folder, first["stored_as"])
    assert os.path.exists(on_disk)

    result = await frame._purge_removed(first["stored_as"], source="http")
    assert result["ok"] and result["deleted"] == 1 and result["freed"] > 0
    assert not os.path.exists(on_disk)
    assert frame.removals.count() == 1                  # the other one is still there

    kept = [e for e in frame.removals.entries(include_purged=True)
            if e["stored_as"] == first["stored_as"]]
    assert len(kept) == 1 and kept[0]["purged_at"]
    assert kept[0]["original_path"] == first["original_path"]

    # And it cannot be put back afterwards, which is the honest answer.
    assert (await frame._restore_removed(first["stored_as"]))["ok"] is False


@pytest.mark.asyncio
async def test_emptying_the_trash_takes_every_file_and_no_line(frame, two_removed):
    result = await frame._empty_trash(source="http")
    assert result["ok"] and result["purged"] == 2 and result["deleted"] == 2
    assert result["freed"] > 0 and result["missing"] == 0 and not result["failed"]

    assert frame.removals.count() == 0
    assert frame.removals.purged_count() == 2
    for entry in two_removed:
        assert not os.path.exists(os.path.join(frame.removals.folder, entry["stored_as"]))
    lines = [ln for ln in open(frame.removals.path, encoding="utf-8") if ln.strip()]
    assert len(lines) == 2                              # nothing was thrown away
    assert all(json.loads(ln)["original_path"] for ln in lines)


@pytest.mark.asyncio
async def test_emptying_an_empty_trash_is_a_no_op(frame):
    result = await frame._empty_trash(source="http")
    assert result["ok"] and result["purged"] == 0 and result["deleted"] == 0


@pytest.mark.asyncio
async def test_a_file_already_deleted_by_hand_still_gets_its_row_tidied(
        frame, two_removed):
    """Somebody emptied the folder with rm.  The ghost row is the mess this
    feature is meant to clear, so purging it must work, not refuse."""
    first = two_removed[0]
    os.unlink(os.path.join(frame.removals.folder, first["stored_as"]))
    result = await frame._purge_removed(first["stored_as"], source="http")
    assert result["ok"] and result["missing"] is True and result["deleted"] == 0
    assert frame.removals.find(first["stored_as"]) is None


@pytest.mark.asyncio
async def test_a_file_the_frame_did_not_put_there_is_counted_not_deleted(
        frame, two_removed):
    """``deleted_folder`` is a setting, and a setting can point at the wrong
    place.  Nothing here unlinks a file the journal cannot account for."""
    stray = os.path.join(frame.removals.folder, "somebody-elses.jpg")
    with open(stray, "w", encoding="utf-8") as fh:
        fh.write("not ours")

    result = await frame._empty_trash(source="http")
    assert result["left_alone"] == 1
    assert os.path.exists(stray)


@pytest.mark.asyncio
async def test_the_delete_switch_covers_deleting_for_good_too(frame, two_removed):
    """Deleting for good cannot be easier than removing."""
    frame.config.http.allow_delete = False
    name = two_removed[0]["stored_as"]

    assert (await frame._purge_removed(name, source="http"))["ok"] is False
    assert (await frame._empty_trash(source="mqtt"))["ok"] is False
    assert os.path.exists(os.path.join(frame.removals.folder, name))

    # Somebody standing at the frame is never refused, here as everywhere.
    assert (await frame._purge_removed(name, source="keyboard"))["ok"] is True


# -- through the API -------------------------------------------------------

@pytest.mark.asyncio
async def test_purging_through_the_api_keeps_the_row_and_marks_it(
        client, frame, one_removed):
    name = one_removed["stored_as"]
    assert client.post(f"/api/removed/{name}/purge").status_code == 200
    assert client.get("/api/removed").json() == []
    kept = client.get("/api/removed?include_purged=true").json()
    assert len(kept) == 1
    assert kept[0]["purged_at"] and kept[0]["purged_iso"]
    assert kept[0]["on_disk"] is False
    assert kept[0]["original_path"] == one_removed["original_path"]


@pytest.mark.asyncio
async def test_emptying_the_trash_through_the_api(client, frame, one_removed):
    response = client.post("/api/removed/empty")
    assert response.status_code == 200
    assert response.json()["deleted"] == 1
    assert frame.removals.count() == 0
    assert "/api/removed/empty" not in str(frame.removals.entries(include_purged=True))
    assert client.get("/api/removed/journal").status_code == 200


@pytest.mark.asyncio
async def test_empty_is_a_route_not_a_file_name(client, frame, one_removed):
    """``/api/removed/empty`` must not be read as a picture called "empty"."""
    assert client.post("/api/removed/empty").status_code == 200
    assert client.post("/api/removed/empty/restore").status_code == 404


@pytest.mark.asyncio
async def test_purging_something_unknown_is_a_404(client, one_removed):
    assert client.post("/api/removed/ghost.jpg/purge").status_code == 404
    for attempt in ("../../etc/passwd", "..%2F..%2Fetc%2Fpasswd"):
        # 405: the path normalises to one with no POST route at all, which is
        # a refusal too -- what matters is that nothing is unlinked.
        assert client.post(
            f"/api/removed/{attempt}/purge").status_code in (404, 405, 422)


@pytest.mark.asyncio
async def test_the_api_refuses_to_delete_for_good_when_removal_is_off(
        client, frame, one_removed):
    frame.config.http.allow_delete = False
    name = one_removed["stored_as"]
    assert client.post(f"/api/removed/{name}/purge").status_code == 403
    assert client.post("/api/removed/empty").status_code == 403
    assert os.path.exists(os.path.join(frame.removals.folder, name))


@pytest.mark.asyncio
async def test_the_state_document_carries_what_was_purged(frame, one_removed):
    await frame._purge_removed(one_removed["stored_as"], source="http")
    removed = frame.state().as_dict()["removed"]
    assert removed["count"] == 0 and removed["purged"] == 1
