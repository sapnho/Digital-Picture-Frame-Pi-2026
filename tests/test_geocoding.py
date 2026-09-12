"""Place names must survive being switched on after the library was built."""

import pytest

from picframe3.library.scanner import Scanner


class FakeGeocoder:
    """Stands in for Nominatim, and counts requests."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.requests = 0

    def lookup(self, lat, lon, *, cached_only=False):
        if cached_only:
            return None
        self.requests += 1
        return self.answers.get(round(lat, 2))


@pytest.fixture
def gps_library(tmp_path):
    from PIL import Image
    from PIL.TiffImagePlugin import IFDRational as R

    from picframe3.library.db import Library

    root = tmp_path / "pics"
    root.mkdir()
    for name, lat, lon in (("munich", (48, 8, 30), (11, 34, 12)),
                           ("rome", (41, 53, 25), (12, 29, 32))):
        exif = Image.Exif()
        gps = exif.get_ifd(0x8825)
        gps[1], gps[2] = "N", tuple(R(v, 1) for v in lat)
        gps[3], gps[4] = "E", tuple(R(v, 1) for v in lon)
        Image.new("RGB", (800, 600), (90, 120, 150)).save(root / f"{name}.jpg", exif=exif)
    db = Library(str(tmp_path / "lib.db3"))
    Scanner(db, [str(root)], geocoder=None).scan()
    return db, root


def test_coordinates_are_indexed_without_a_geocoder(gps_library):
    db, _ = gps_library
    assert db.count() == 2
    assert db.count_locations_missing() == 2


def test_enabling_geocoding_later_fills_the_library_in(gps_library):
    """The regression this guards: a rescan skips unchanged files, so indexing
    was the only chance a photo ever got at a place name."""
    db, root = gps_library
    geo = FakeGeocoder({48.14: "Munich, Germany", 41.89: "Rome, Italy"})
    scanner = Scanner(db, [str(root)], geocoder=geo)

    assert scanner.scan().skipped == 2          # nothing changed on disk
    assert scanner.backfill_locations(limit=10) == 2
    assert db.count_locations_missing() == 0
    assert {r.location for r in db.query("SELECT * FROM files")} == {
        "Munich, Germany", "Rome, Italy"
    }


def test_backfill_respects_its_batch_size(gps_library):
    db, root = gps_library
    geo = FakeGeocoder({48.14: "Munich, Germany", 41.89: "Rome, Italy"})
    scanner = Scanner(db, [str(root)], geocoder=geo)
    assert scanner.backfill_locations(limit=1) == 1
    assert db.count_locations_missing() == 1


def test_scanning_never_makes_a_cold_request(gps_library):
    """Indexing ten thousand photos must not become ten thousand lookups."""
    db, root = gps_library
    geo = FakeGeocoder({48.14: "Munich, Germany"})
    Scanner(db, [str(root)], geocoder=geo).scan()
    assert geo.requests == 0


def test_a_single_lookup_for_the_picture_on_screen(gps_library):
    db, root = gps_library
    geo = FakeGeocoder({48.14: "Munich, Germany"})
    scanner = Scanner(db, [str(root)], geocoder=geo)
    assert scanner.resolve_location(48.1416, 11.57) == "Munich, Germany"
    assert scanner.resolve_location(None, None) is None


def test_clearing_forces_a_fresh_lookup(gps_library):
    db, root = gps_library
    geo = FakeGeocoder({48.14: "Munich, Germany", 41.89: "Rome, Italy"})
    scanner = Scanner(db, [str(root)], geocoder=geo)
    scanner.backfill_locations(limit=10)
    assert db.clear_locations() == 2
    assert db.count_locations_missing() == 2
