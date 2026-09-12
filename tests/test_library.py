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
