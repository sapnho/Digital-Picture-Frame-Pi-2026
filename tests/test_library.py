import os

from picframe3.library.playlist import Filters, Playlist
from picframe3.library.scanner import Scanner


def test_scan_indexes_everything(library, photo_dir):
    assert library.count() == 7
    assert library.count("is_portrait=1") >= 2


def test_rescan_skips_unchanged(library, photo_dir, tmp_path):
    scanner = Scanner(library, [str(photo_dir)])
    result = scanner.scan()
    assert result.added == 0 and result.skipped == 7


def test_changed_file_is_reindexed(library, photo_dir):
    from PIL import Image

    target = photo_dir / "2023" / "img00.jpg"
    Image.new("RGB", (400, 400), (1, 2, 3)).save(target)
    os.utime(target, (0, 0))
    scanner = Scanner(library, [str(photo_dir)])
    assert scanner.scan().updated == 1
    assert library.by_path(str(target)).width == 400


def test_prune_removes_vanished_files(library, photo_dir):
    victim = photo_dir / "2024" / "img01.jpg"
    victim.unlink()
    assert Scanner(library, [str(photo_dir)]).scan().removed == 1
    assert library.by_path(str(victim)) is None


def test_full_text_search(library, photo_dir):
    record = library.by_path(str(photo_dir / "2024" / "exif.jpg"))
    meta = record.as_meta()
    meta.title = "Alpine morning"
    meta.tags = ["mountains", "sunrise"]
    st = os.stat(meta.path)
    library.upsert(meta, mtime=st.st_mtime, size=st.st_size, location="Munich, Germany")
    assert [r.basename for r in library.search("alpin")] == ["exif.jpg"]
    assert [r.basename for r in library.search("munich")] == ["exif.jpg"]
    assert ("mountains", 1) in library.all_tags()


def test_playlist_covers_every_picture_once_per_round(library):
    playlist = Playlist(library, order="shuffle", persist=False)
    seen = []
    for _ in range(playlist.size):
        seen.extend(r.id for r in playlist.next())
    assert sorted(seen) == sorted(playlist._ids)


def test_playlist_reshuffles_between_rounds(library):
    a = Playlist(library, order="shuffle", persist=False)
    a._round = 1
    a.refresh()
    b = Playlist(library, order="shuffle", persist=False)
    b._round = 2
    b.refresh()
    assert a._ids != b._ids or library.count() < 3


def test_portrait_pairing(library):
    playlist = Playlist(library, order="name", portrait_pairs=True, persist=False)
    groups = [playlist.next() for _ in range(playlist.size)]
    paired = [g for g in groups if len(g) == 2]
    assert paired, "expected at least one portrait pair"
    assert all(r.is_portrait for g in paired for r in g)


def test_previous_returns_the_previous_group(library):
    playlist = Playlist(library, order="name", persist=False)
    first = playlist.next()
    second = playlist.next()
    assert [r.id for r in playlist.previous()] == [r.id for r in first]
    assert first != second


def test_filters_narrow_the_playlist(library, photo_dir):
    full = Playlist(library, order="name", persist=False).size
    narrowed = Playlist(library, order="name",
                        filters=Filters(subfolder="2023"), persist=False).size
    assert 0 < narrowed < full


def test_video_filter(library):
    playlist = Playlist(library, order="name",
                        filters=Filters(include_videos=False), persist=False)
    assert playlist.size == library.count("hidden=0 AND is_video=0")


# -- fair distribution -----------------------------------------------------
# The property that matters on a frame that runs for years is not "looks
# random" but "every picture gets the same number of turns".  These three
# tests pin the three ways that guarantee used to leak.

def test_new_pictures_mid_round_do_not_rob_the_ones_still_waiting(library, photo_dir):
    """Copying a holiday over Samba must not restart the round."""
    from PIL import Image

    playlist = Playlist(library, order="shuffle")
    total = playlist.size
    shown = [r.id for _ in range(total // 2) for r in playlist.next()]

    newcomer = photo_dir / "2024" / "added-later.jpg"
    Image.new("RGB", (800, 600), (9, 9, 9)).save(newcomer)
    Scanner(library, [str(photo_dir)]).scan()
    playlist.refresh()

    while playlist.remaining:
        shown.extend(r.id for r in playlist.next())
    assert len(shown) == len(set(shown)), "a picture came up twice in one round"
    assert set(shown) == {r["id"] for r in
                          library.connect().execute("SELECT id FROM files WHERE hidden=0")}


def test_a_restart_resumes_the_round_instead_of_starting_over(library):
    first = Playlist(library, order="shuffle")
    seen = [r.id for _ in range(3) for r in first.next()]

    resumed = Playlist(library, order="shuffle")      # as if the Pi rebooted
    assert resumed.round == first.round
    rest = []
    while resumed.remaining:
        rest.extend(r.id for r in resumed.next())
    assert not (set(seen) & set(rest)), "a reboot replayed pictures already shown"


def test_play_counts_stay_within_one_of_each_other(library):
    playlist = Playlist(library, order="shuffle")
    for _ in range(playlist.size * 3 + 2):
        for record in playlist.next():
            library.mark_played(record.id, playlist.round)
    stats = library.play_stats()
    assert stats["max"] - stats["min"] <= 1, stats
    assert stats["never_shown"] == 0


def test_each_round_gets_a_different_order(library):
    playlist = Playlist(library, order="shuffle")
    rounds = []
    for _ in range(3):
        order = []
        while playlist.remaining:
            order.extend(r.id for r in playlist.next())
        playlist.next()                     # crosses into the next round
        rounds.append(tuple(order))
    assert len(set(rounds)) > 1, "the same order every round is not a shuffle"


def test_an_old_index_upgrades_in_place(tmp_path):
    """A frame that has been running since 3.2 must not lose its library."""
    import re
    import sqlite3

    from picframe3.library.db import _SCHEMA, Library

    path = tmp_path / "old.db"
    v1 = re.sub(r"    -- The shuffle round.*?play_round    INTEGER NOT NULL DEFAULT 0,\n",
                "", _SCHEMA, flags=re.S)
    v1 = re.sub(r"    -- Deliberately kept out.*?digest        TEXT,\n",
                "", v1, flags=re.S)
    # ...and no holds table at all, which is what an index written before the
    # held-out list existed actually looks like.
    v1 = re.sub(r"-- Pictures that were removed.*?CREATE INDEX IF NOT EXISTS holds_size[^;]*;\n",
                "", v1, flags=re.S)
    files_table = v1.split("CREATE TABLE IF NOT EXISTS files")[1].split(");")[0]
    for column in ("play_round", "held", "digest"):
        assert column not in files_table, f"the v1 stand-in still has files.{column}"
    assert "holds" not in v1, "the v1 schema stand-in still has the holds table"
    conn = sqlite3.connect(path)
    conn.executescript(v1)
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
    conn.commit()
    conn.close()

    upgraded = Library(str(path))
    columns = {r[1] for r in upgraded.connect().execute("PRAGMA table_info(files)")}
    assert {"play_round", "held", "digest"} <= columns
    assert upgraded.connect().execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
    # The held-out list is part of the upgrade, not only its two columns.
    upgraded.hold("abc", size=7, path="/pics/a.jpg", stored_as="a.jpg")
    assert upgraded.held_sizes() == {7}


# -- the ways the library used to break the frame ---------------------------
# Each of these pins one failure that reached a frame on a wall: a filter that
# made it unbootable, a share that emptied the index, a deleted folder that
# stayed in it, and four smaller ones that quietly showed the wrong pictures.

def _library_of(tmp_path, folders, name="lib.db3"):
    """A library over ``{folder: count}``, built the way a frame builds one."""
    from PIL import Image

    from picframe3.library.db import Library

    root = tmp_path / "pics"
    for folder, count in folders.items():
        (root / folder).mkdir(parents=True, exist_ok=True)
        for i in range(count):
            Image.new("RGB", (80, 60), (i * 7 % 255, 40, 90)).save(
                root / folder / f"img{i:02d}.jpg")
    db = Library(str(tmp_path / name))
    Scanner(db, [str(root)]).scan()
    return db, root


def test_a_double_quote_in_the_search_box_does_not_brick_the_frame(library):
    """The one that made a frame unbootable until the database was edited.

    ``dog "`` left FTS5 with an unterminated string, and because the filter was
    persisted before it was tried, every later start raised the same error.
    """
    from picframe3.library.db import fts_query

    assert fts_query('dog "') == '"dog"*'

    playlist = Playlist(library, order="name")
    playlist.set_filters(Filters(search='dog "'))
    assert playlist.size == 0                      # no match, but no exception
    assert library.search('dog "') == []

    # And a frame restarting with that filter in the database comes up --
    # rebuilding the playlist from the stored filter is what app.py does.
    stored = Filters.from_dict(library.get_state("playlist_filters") or {})
    assert stored.search == 'dog "'
    assert Playlist(library, order="name", filters=stored).size == 0


def test_a_filter_is_only_remembered_once_it_has_worked(library, monkeypatch):
    playlist = Playlist(library, order="name")
    playlist.set_filters(Filters(subfolder="2024"))

    def explode(*_args, **_kwargs):
        raise RuntimeError("query failed")

    monkeypatch.setattr(playlist, "refresh", explode)
    try:
        playlist.set_filters(Filters(search="whatever"))
    except RuntimeError:
        pass
    stored = library.get_state("playlist_filters") or {}
    assert stored.get("subfolder") == "2024", "a filter that failed was persisted"


def test_an_unmounted_share_does_not_wipe_the_index(tmp_path, library, photo_dir):
    """The failure that costs play counts, rounds and place names."""
    before = library.count()
    scanner = Scanner(library, [str(photo_dir), str(tmp_path / "not-mounted")])
    result = scanner.scan()
    assert result.missing_roots == [str(tmp_path / "not-mounted")]
    assert result.removed == 0
    assert library.count() == before


def test_a_mass_disappearance_is_treated_as_a_mount_problem(library, photo_dir):
    for victim in sorted(photo_dir.glob("2023/*.jpg")):
        victim.unlink()
    before = library.count()
    assert Scanner(library, [str(photo_dir)]).scan().removed == 0
    assert library.count() == before
    # Deliberately deleting them is still possible, by saying so.
    scanner = Scanner(library, [str(photo_dir)], prune_max_fraction=1.0)
    assert scanner.scan().removed > 0
    assert library.count() < before


def test_a_deleted_folder_takes_its_pictures_out_of_the_index(tmp_path):
    import shutil

    db, root = _library_of(tmp_path, {"trip": 3, "keep": 2})
    scanner = Scanner(db, [str(root)])
    shutil.rmtree(root / "trip")

    result = scanner.rescan_paths([str(root / "trip")])
    assert result.removed == 3
    assert db.count() == 2
    db.close()


def test_inotify_does_not_re_add_what_the_settings_exclude(tmp_path):
    from PIL import Image

    db, root = _library_of(tmp_path, {"keep": 1})
    scanner = Scanner(db, [str(root)], include_videos=False,
                      exclude=["@eaDir"], ignore_hidden=True)
    (root / "@eaDir").mkdir()
    Image.new("RGB", (40, 30), (1, 2, 3)).save(root / "@eaDir" / "thumb.jpg")
    Image.new("RGB", (40, 30), (1, 2, 3)).save(root / "keep" / ".hidden.jpg")
    (root / "keep" / "clip.mp4").write_bytes(b"not really a video")

    result = scanner.rescan_paths([
        str(root / "@eaDir" / "thumb.jpg"),
        str(root / "keep" / ".hidden.jpg"),
        str(root / "keep" / "clip.mp4"),
    ])
    assert (result.added, result.scanned) == (0, 0)
    assert db.count() == 1
    db.close()


def test_previous_walks_past_a_deleted_picture(library):
    """Pressing back onto a removed photograph must not blank the frame."""
    playlist = Playlist(library, order="name", persist=False)
    first = playlist.next()
    second = playlist.next()
    playlist.next()
    library.forget([second[0].path])

    back = playlist.previous()                     # onto the deleted one
    assert back, "the empty-library placeholder came up instead of a picture"
    assert [r.id for r in back] == [r.id for r in first]


def test_an_empty_selection_does_not_spin_the_round_counter(library):
    """A filter that matches nothing used to write to the card every tick."""
    playlist = Playlist(library, order="shuffle")
    playlist.set_filters(Filters(subfolder="no-such-folder"))
    assert playlist.size == 0
    round_before = playlist.round
    writes = library.get_state("playlist_round")

    for _ in range(20):
        assert playlist.next() == []
    assert playlist.round == round_before
    assert library.get_state("playlist_round") == writes


def test_neither_images_nor_videos_falls_back_to_both():
    merged = Filters().merged({"include_videos": False, "include_images": False})
    assert merged.include_videos and merged.include_images


def test_peek_changes_nothing(library):
    playlist = Playlist(library, order="name")
    while playlist.remaining:
        playlist.next()
    playlist.previous()                            # fills the redo queue
    round_before, future_before = playlist.round, list(playlist._future)

    peeked = playlist.peek()

    assert playlist.round == round_before, "peek() opened a new round"
    assert list(playlist._future) == future_before, "peek() destroyed the redo queue"
    assert [r.id for r in peeked] == [r.id for r in playlist.next()]


def test_a_folder_filter_does_not_treat_underscores_as_wildcards(tmp_path):
    db, _root = _library_of(tmp_path, {"2024_Italien": 2, "2024xItalien": 3})
    playlist = Playlist(db, order="name",
                        filters=Filters(subfolder="2024_Italien"), persist=False)
    assert playlist.size == 2
    db.close()


def test_a_place_filter_does_not_treat_percent_as_a_wildcard(tmp_path):
    db, root = _library_of(tmp_path, {"a": 2})
    rows = db.query("SELECT * FROM files ORDER BY path")
    db.set_location(rows[0].id, "100% Arctic")
    db.set_location(rows[1].id, "100 Arctic")
    playlist = Playlist(db, order="name",
                        filters=Filters(location_contains="100%"), persist=False)
    assert playlist.size == 1
    assert root.exists()
    db.close()


def test_the_exif_date_format_is_understood():
    """What the picframe MQTT automations people already have publish."""
    from datetime import datetime

    from picframe3.library.playlist import parse_date

    assert parse_date("2026:09:12") == datetime(2026, 9, 12).timestamp()
    assert parse_date("2026:09:12", end_of_day=True) == datetime(
        2026, 9, 12, 23, 59, 59).timestamp()
    assert parse_date("2026:09:12 08:30:00") == datetime(
        2026, 9, 12, 8, 30).timestamp()


# -- the order a frame starts in -------------------------------------------

def test_with_nothing_set_the_frame_shuffles():
    from picframe3.config import Config
    from picframe3.library.playlist import starting_order

    assert starting_order(Config().slideshow.order, None) == "shuffle"
    assert starting_order("nonsense", None) == "shuffle"


def test_an_order_picked_on_the_frame_survives_a_restart():
    from picframe3.library.playlist import starting_order

    remembered = {"order": "date_desc", "configured": "shuffle"}
    assert starting_order("shuffle", remembered) == "date_desc"


def test_an_edited_config_file_beats_the_remembered_order():
    from picframe3.library.playlist import starting_order

    remembered = {"order": "date_desc", "configured": "shuffle"}
    assert starting_order("name", remembered) == "name"
    assert starting_order("shuffle", {"order": "bogus", "configured": "shuffle"}) == "shuffle"
