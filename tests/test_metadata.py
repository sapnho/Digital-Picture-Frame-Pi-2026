from datetime import UTC, datetime

from picframe3.media import metadata


def test_reads_exif(photo_dir):
    meta = metadata.read(str(photo_dir / "2024" / "exif.jpg"))
    assert meta is not None
    assert meta.make == "Canon" and meta.model == "EOS R6"
    assert meta.f_number == 2.8
    assert meta.exposure_time == "1/250s"
    assert meta.iso == 400
    assert meta.focal_length == 50.0


def test_timezone_offset_is_applied(photo_dir):
    meta = metadata.read(str(photo_dir / "2024" / "exif.jpg"))
    taken = datetime.fromtimestamp(meta.taken_at, tz=UTC)
    assert (taken.hour, taken.minute) == (16, 22)      # 18:22 at +02:00


def test_gps_is_signed_correctly(photo_dir):
    meta = metadata.read(str(photo_dir / "2024" / "exif.jpg"))
    assert round(meta.latitude, 3) == 48.142
    assert round(meta.longitude, 3) == 11.570


def test_orientation_swaps_display_size(photo_dir):
    meta = metadata.read(str(photo_dir / "2024" / "exif.jpg"))
    assert meta.orientation == 6
    assert meta.display_size == (800, 1200)
    assert meta.is_portrait


def test_missing_file_returns_none(tmp_path):
    assert metadata.read(str(tmp_path / "nope.jpg")) is None


def test_falls_back_to_mtime(photo_dir):
    meta = metadata.read(str(photo_dir / "2023" / "img00.jpg"))
    assert meta.taken_at is not None


def test_extension_classification():
    assert metadata.is_supported("/x/a.HEIC")
    assert metadata.is_video("/x/a.MP4")
    assert not metadata.is_supported("/x/notes.txt")
