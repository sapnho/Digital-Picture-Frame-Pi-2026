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
    assert "play_round" not in v1, "the v1 schema stand-in still has the new column"
    conn = sqlite3.connect(path)
    conn.executescript(v1)
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
    conn.commit()
    conn.close()

    upgraded = Library(str(path))
    columns = {r[1] for r in upgraded.connect().execute("PRAGMA table_info(files)")}
    assert "play_round" in columns
    assert upgraded.connect().execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
