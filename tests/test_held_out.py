"""A removed picture stays removed -- until somebody says otherwise.

The failure being pinned here is the one the old pi3d frame actually had: a
picture was taken off the wall, and some time later it was back on it.  There
are three separate ways that happens, and each of them has tests below.

1. **The removal is only a database row.**  Delete the row and the file, and
   anything that puts a copy of the file back -- a two-way Syncthing folder, a
   restored backup, the same photograph arriving again from a phone under a
   different name -- puts the picture back in the playlist.  So a removal is
   remembered by the *content*, and a returning copy is held out however it is
   named and wherever it lands.

2. **A stale id in the playlist.**  A picture that leaves the library between
   two refreshes leaves an id behind that resolves to nothing, and "nothing"
   used to be indistinguishable from "the library is empty".

3. **The file is read after it has gone.**  The next picture is decoded up to
   a whole interval before it is shown, so a photograph deleted inside that
   window went up once more from a copy already in memory.

And the other half of the promise, which is just as important: *unless I say
so*.  Releasing is one click, putting a picture back from the trash releases it
by itself, and nothing else in the program clears a hold.
"""

import os
import shutil

import pytest

from picframe3.library.digest import file_digest
from picframe3.library.playlist import Playlist
from picframe3.library.scanner import Scanner

# -- the held-out list itself ----------------------------------------------

def test_a_hold_is_kept_by_content_not_by_name(library, photo_dir, tmp_path):
    """The same bytes under another name are the same photograph."""
    original = str(photo_dir / "2023" / "img00.jpg")
    digest = file_digest(original)
    library.hold(digest, size=os.path.getsize(original), path=original,
                 stored_as="img00.jpg")

    copy = photo_dir / "2024" / "quite-a-different-name.jpg"
    shutil.copy2(original, copy)
    os.unlink(original)
    Scanner(library, [str(photo_dir)]).scan()

    record = library.by_path(str(copy))
    assert record is not None, "it should be indexed -- the Removed tab shows it"
    assert record.held, "but held out of the playlist"
    assert str(copy) not in [r.path for r in _playlist_records(library)]


def test_only_files_of_a_held_size_are_ever_hashed(library, photo_dir, monkeypatch):
    """The size test in front of the hash is what makes this affordable.

    Without it, every scan reads every photograph in the library from end to
    end -- minutes of disk on a Pi, every hour, for ever.
    """
    from picframe3.library import scanner as scanner_module

    hashed: list[str] = []

    def counting_digest(path):
        hashed.append(path)
        return file_digest(path)

    monkeypatch.setattr(scanner_module, "file_digest", counting_digest)

    original = str(photo_dir / "2023" / "img00.jpg")
    library.hold("no-such-digest", size=os.path.getsize(original),
                 path="/gone/x.jpg", stored_as="x.jpg")
    # Everything is already indexed and unchanged, so force a re-read of all
    # of it: this is the worst case the size test has to survive.
    library.connect().execute("UPDATE files SET mtime = 0")
    Scanner(library, [str(photo_dir)]).scan()

    assert hashed, "the one file of that size should have been hashed"
    assert all(os.path.getsize(p) == os.path.getsize(original) for p in hashed)
    assert len(hashed) < len(_playlist_records(library))


def test_a_size_match_that_is_a_different_picture_is_left_alone(library, photo_dir):
    """Same weight, different photograph: the hash is what decides."""
    victim = str(photo_dir / "2023" / "img00.jpg")
    library.hold("a-digest-belonging-to-something-else",
                 size=os.path.getsize(victim), path="/gone/x.jpg", stored_as="x.jpg")
    library.connect().execute("UPDATE files SET mtime = 0")
    Scanner(library, [str(photo_dir)]).scan()
    assert not library.by_path(victim).held


def test_a_copy_already_in_the_index_goes_out_at_once(library, photo_dir):
    """Two copies of one photograph, and you remove one of them.

    The other is already indexed and nothing about it has changed, so waiting
    for the next scan to notice would leave the picture you just took off the
    wall on the wall for an hour.
    """
    original = str(photo_dir / "2023" / "img00.jpg")
    twin = photo_dir / "2024" / "twin.jpg"
    shutil.copy2(original, twin)
    Scanner(library, [str(photo_dir)]).scan()

    digest = file_digest(original)
    library.hold(digest, size=os.path.getsize(original), path=original,
                 stored_as="img00.jpg")
    # What the frame does in the executor as part of a removal.
    for file_id, path in library.files_sized(os.path.getsize(original)):
        if file_digest(path) == digest:
            library.mark_held(file_id, digest)
    assert library.by_path(str(twin)).held


def test_releasing_is_the_only_thing_that_clears_a_hold(library, photo_dir):
    original = str(photo_dir / "2023" / "img00.jpg")
    digest = file_digest(original)
    library.hold(digest, size=os.path.getsize(original), path=original,
                 stored_as="img00.jpg")
    # The scan is what recognises a file already on the disk...
    library.connect().execute("UPDATE files SET mtime = 0")
    Scanner(library, [str(photo_dir)]).scan()
    assert library.by_path(original).held

    # ...and every scan after it holds it out again, for ever, by itself.
    library.connect().execute("UPDATE files SET mtime = 0")
    Scanner(library, [str(photo_dir)]).scan()
    assert library.by_path(original).held

    assert library.release(digest) is True
    assert not library.by_path(original).held
    assert library.held_sizes() == set()


def test_removing_the_same_picture_twice_keeps_the_sightings(library, photo_dir):
    """A second removal must not wipe the record of what keeps coming back."""
    original = str(photo_dir / "2023" / "img00.jpg")
    digest = file_digest(original)
    library.hold(digest, size=1, path=original, stored_as="a.jpg")
    library.note_seen(digest, "/pictures/again.jpg")
    library.hold(digest, size=1, path=original, stored_as="a-1.jpg")
    hold = library.hold_for(digest)
    assert hold["seen_count"] == 1
    assert hold["seen_path"] == "/pictures/again.jpg"


def test_the_summary_counts_what_has_come_back(library):
    library.hold("one", size=1, path="/a.jpg", stored_as="a.jpg")
    library.hold("two", size=2, path="/b.jpg", stored_as="b.jpg")
    library.note_seen("two", "/pictures/b.jpg")
    summary = library.hold_summary()
    assert summary["held"] == 2
    assert summary["came_back"] == 1
    assert summary["last_came_back_path"] == "/pictures/b.jpg"


def test_a_held_picture_is_out_of_the_browsing_view_too(library, photo_dir):
    """Held out means held out -- not "hidden from the wall but in the grid"."""
    original = str(photo_dir / "2024" / "exif.jpg")
    before = library.stats()["files"]
    digest = file_digest(original)
    library.connect().execute("UPDATE files SET digest=? WHERE path=?",
                              (digest, original))
    library.hold(digest, size=os.path.getsize(original), path=original,
                 stored_as="exif.jpg")
    assert library.stats()["held"] == 1
    assert library.stats()["files"] == before        # still indexed, still counted
    assert library.count() == before - 1             # but not in the running


# -- the whole round trip through the frame --------------------------------

@pytest.fixture
def frame(tmp_path, photo_dir):
    """A PicFrame with a library and no screen."""
    from picframe3.app import PicFrame
    from picframe3.config import Config
    from picframe3.library.db import Library
    from picframe3.library.removed import RemovalLog

    config = Config()
    config.library.picture_folders = [str(photo_dir)]
    config.library.database = str(tmp_path / "library.db3")
    config.library.deleted_folder = str(tmp_path / "deleted")
    app = PicFrame(config)
    app.library = Library(config.library.database)
    app.removals = RemovalLog(config.library.deleted_folder)
    app.scanner = Scanner(app.library, [str(photo_dir)])
    app.scanner.scan()
    app.playlist = Playlist(app.library)
    app._advance = _noop
    yield app
    app.library.close()


async def _noop(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_a_picture_that_comes_back_does_not_come_back(frame, photo_dir):
    """The test this whole feature exists for.

    Remove a picture, then have something put the file back -- which is what a
    folder that syncs both ways does, minutes later, every time.
    """
    path = str(photo_dir / "2024" / "exif.jpg")
    frame.current = [frame.library.by_path(path)]
    await frame._delete_current(source="http")
    assert not os.path.exists(path)

    stored = os.path.join(frame.removals.folder, frame.removals.latest()["stored_as"])
    shutil.copy2(stored, path)                   # Syncthing puts it back
    frame.scanner.scan()
    frame.playlist.refresh()

    record = frame.library.by_path(path)
    assert record.held
    assert path not in [r.path for r in _playlist_records(frame.library)]
    assert frame.library.hold_summary()["came_back"] == 1


@pytest.mark.asyncio
async def test_removing_one_of_two_copies_takes_both_off_the_wall(frame, photo_dir):
    """Same photograph, two files.  Removing one and leaving the other up is
    not what anybody means by removing it."""
    path = str(photo_dir / "2024" / "exif.jpg")
    twin = photo_dir / "2023" / "same-picture.jpg"
    shutil.copy2(path, twin)
    frame.scanner.scan()
    frame.playlist.refresh()

    frame.current = [frame.library.by_path(path)]
    await frame._delete_current(source="http")
    frame.playlist.refresh()

    assert frame.library.by_path(str(twin)).held
    assert str(twin) not in [r.path for r in _playlist_records(frame.library)]


@pytest.mark.asyncio
async def test_the_journal_says_what_the_picture_was(frame, photo_dir):
    """The digest is in the journal too, which is the copy that survives
    losing the database."""
    path = str(photo_dir / "2024" / "exif.jpg")
    digest = file_digest(path)
    frame.current = [frame.library.by_path(path)]
    await frame._delete_current(source="http")
    assert frame.removals.latest()["digest"] == digest


@pytest.mark.asyncio
async def test_saying_so_puts_it_back_in_the_playlist(frame, photo_dir):
    path = str(photo_dir / "2024" / "exif.jpg")
    frame.current = [frame.library.by_path(path)]
    await frame._delete_current(source="http")
    stored_as = frame.removals.latest()["stored_as"]
    shutil.copy2(os.path.join(frame.removals.folder, stored_as), path)
    frame.scanner.scan()

    result = await frame._release_removed(stored_as, source="http")
    assert result["ok"]
    assert not frame.library.by_path(path).held
    frame.playlist.refresh()
    assert path in [r.path for r in _playlist_records(frame.library)]


@pytest.mark.asyncio
async def test_putting_one_back_from_the_trash_releases_it_by_itself(frame, photo_dir):
    """Otherwise the picture goes back to its folder and still never shows --
    restored, and silently held out by the next scan."""
    path = str(photo_dir / "2024" / "exif.jpg")
    frame.current = [frame.library.by_path(path)]
    await frame._delete_current(source="http")
    stored_as = frame.removals.latest()["stored_as"]

    assert (await frame._restore_removed(stored_as))["ok"]
    frame.scanner.scan()
    assert frame.library.hold_summary()["held"] == 0
    assert not frame.library.by_path(path).held


@pytest.mark.asyncio
async def test_deleting_it_for_good_keeps_holding_it_out(frame, photo_dir):
    """If anything, a picture deleted for good has a better claim to stay out
    of the playlist than one still sitting in the trash."""
    path = str(photo_dir / "2024" / "exif.jpg")
    frame.current = [frame.library.by_path(path)]
    await frame._delete_current(source="http")
    stored_as = frame.removals.latest()["stored_as"]
    await frame._purge_removed(stored_as, source="http")
    assert frame.library.hold_summary()["held"] == 1


@pytest.mark.asyncio
async def test_releasing_something_nobody_removed_says_so(frame):
    result = await frame._release_removed("never-existed.jpg", source="http")
    assert not result["ok"]


@pytest.mark.asyncio
async def test_a_removal_that_cannot_be_hashed_still_removes(frame, photo_dir,
                                                             monkeypatch):
    """Bookkeeping must never be what stops the frame removing a picture."""
    import picframe3.app as app_module

    monkeypatch.setattr(app_module, "file_digest", lambda path: None)
    path = str(photo_dir / "2024" / "exif.jpg")
    frame.current = [frame.library.by_path(path)]
    await frame._delete_current(source="http")
    assert frame.library.by_path(path) is None
    assert not os.path.exists(path)
    assert frame.library.hold_summary()["held"] == 0


# -- the two ways a deleted picture used to reach the screen ----------------

def test_the_playlist_steps_over_an_id_that_has_gone(library, photo_dir):
    """One deleted photograph used to put the "nothing to show" screen up.

    ``next()`` resolved the stale id to an empty list, and an empty list is
    what "the library is empty" looks like to the caller.
    """
    playlist = Playlist(library, persist=False)
    size = playlist.size
    assert size > 2
    # Delete the rows the playlist is about to reach, behind its back.
    upcoming = playlist.peek()
    library.forget([r.path for r in upcoming])

    group = playlist.next()
    assert group, "a picture that has gone must be stepped over, not give up"
    assert all(r.id not in [u.id for u in upcoming] for r in group)


def test_the_playlist_gives_up_only_when_everything_has_gone(library):
    playlist = Playlist(library, persist=False)
    library.connect().execute("DELETE FROM files")
    assert playlist.next() == []


@pytest.mark.asyncio
async def test_a_picture_deleted_while_it_was_prefetched_is_not_shown(frame,
                                                                     photo_dir):
    """The narrow window: the next slide is decoded up to an interval early.

    The existence check sits in front of the loader for exactly this -- taking
    the prefetched copy would put a deleted photograph on the wall once more.
    """
    shown: list[str] = []
    loaded: list[str] = []

    class _Loader:
        def prefetched_for(self, metas):
            return True

        async def take_prefetched(self):
            loaded.append("prefetched")
            return _Prepared()

        def cancel_prefetch(self):
            pass

        async def load(self, metas):
            loaded.append("loaded")
            return _Prepared()

        def prefetch(self, metas):
            pass

    class _Prepared:
        image = None
        is_video = False
        fit = "contain"

    frame.loader = _Loader()
    frame.slideshow._show = lambda group, prepared, initial=False: shown.extend(
        r.path for r in group)

    doomed = frame.playlist.peek()[0].path
    os.unlink(doomed)

    await frame.slideshow.advance()
    assert doomed not in shown
    assert frame.library.by_path(doomed) is None, "and it is out of the index"


@pytest.mark.asyncio
async def test_a_file_that_is_there_but_will_not_decode_is_only_hidden(frame,
                                                                      photo_dir):
    """Gone and unreadable need opposite answers: a corrupt file may become
    readable again (the HEIC plugin arrives), a deleted one will not."""
    class _Loader:
        def prefetched_for(self, metas):
            return False

        def cancel_prefetch(self):
            pass

        async def load(self, metas):
            return None

        def prefetch(self, metas):
            pass

    broken = frame.playlist.peek()[0].path
    with open(broken, "wb") as fh:
        fh.write(b"still here, still not a picture")
    frame.loader = _Loader()
    frame.slideshow._show_placeholder = lambda: None

    await frame.slideshow.advance()
    record = frame.library.by_path(broken)
    assert record is not None, "it is still on the disk, so it stays in the index"
    assert record.hidden


# -- the web surface -------------------------------------------------------

@pytest.fixture
def client(frame):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from picframe3.control.http import HttpServer

    return TestClient(HttpServer(frame, frame.config.http).api)


@pytest.mark.asyncio
async def test_the_removed_tab_can_tell_you_it_came_back(client, frame, photo_dir):
    path = str(photo_dir / "2024" / "exif.jpg")
    frame.current = [frame.library.by_path(path)]
    await frame._delete_current(source="http")
    row = client.get("/api/removed").json()[0]
    assert row["held"] is True
    assert row["came_back"] is False

    shutil.copy2(os.path.join(frame.removals.folder, row["stored_as"]), path)
    frame.scanner.scan()
    row = client.get("/api/removed").json()[0]
    assert row["came_back"] is True
    assert row["seen_path"] == path
    assert client.get("/api/removed/summary").json()["came_back"] == 1


@pytest.mark.asyncio
async def test_the_page_can_release_one(client, frame, photo_dir):
    path = str(photo_dir / "2024" / "exif.jpg")
    frame.current = [frame.library.by_path(path)]
    await frame._delete_current(source="http")
    stored_as = frame.removals.latest()["stored_as"]

    assert client.post(f"/api/removed/{stored_as}/allow").status_code == 200
    assert frame.library.hold_summary()["held"] == 0
    # Twice is not an error worth pretending about: there is nothing held out.
    assert client.post(f"/api/removed/{stored_as}/allow").status_code == 404


def test_releasing_refuses_a_name_that_is_not_one_of_its_own(client):
    """``stored_as`` comes off the URL, so it bounces off basename() and the
    journal before anything is looked up."""
    assert client.post("/api/removed/%2e%2e/allow").status_code == 404
    assert client.post("/api/removed/passwd/allow").status_code == 404


def test_home_assistant_can_release_one_too():
    """Every control surface is a peer here, as everywhere else."""
    from picframe3.control.mqtt import MQTT_REFUSED
    from picframe3.events import Action, Command

    command = Command.parse('{"action": "release", "payload": {"stored_as": "a.jpg"}}')
    assert command.action is Action.RELEASE
    assert Action.RELEASE not in MQTT_REFUSED


def _playlist_records(library):
    playlist = Playlist(library, persist=False)
    return [r for r in (library.get(i) for i in playlist._ids) if r is not None]

