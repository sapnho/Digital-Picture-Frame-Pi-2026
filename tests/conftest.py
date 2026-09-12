import os
import sys

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational as R

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _has_egl() -> bool:
    try:
        from picframe3.gfx import create_backend

        backend = create_backend("headless", width=16, height=16)
        backend.close()
        return True
    except Exception:
        return False


HAS_EGL = _has_egl()
needs_gl = pytest.mark.skipif(not HAS_EGL, reason="no EGL/GLES available")


@pytest.fixture
def photo_dir(tmp_path):
    """A small library: landscape, portrait, and one with full EXIF."""
    root = tmp_path / "pics"
    (root / "2024").mkdir(parents=True)
    (root / "2023").mkdir(parents=True)
    for i in range(6):
        size = (900, 1200) if i % 3 == 0 else (1600, 1000)
        folder = root / ("2024" if i % 2 else "2023")
        Image.new("RGB", size, (20 * i % 255, 90, 160)).save(folder / f"img{i:02d}.jpg")

    exif = Image.Exif()
    exif[271], exif[272], exif[274] = "Canon", "EOS R6", 6
    ex = exif.get_ifd(0x8769)
    ex[36867] = "2024:07:14 18:22:05"
    ex[36881] = "+02:00"
    ex[33437], ex[33434], ex[34855], ex[37386] = R(28, 10), R(1, 250), 400, R(50, 1)
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (R(48, 1), R(8, 1), R(30, 1))
    gps[3], gps[4] = "E", (R(11, 1), R(34, 1), R(12, 1))
    Image.new("RGB", (1200, 800), (120, 140, 90)).save(root / "2024" / "exif.jpg", exif=exif)
    return root


@pytest.fixture
def library(tmp_path, photo_dir):
    from picframe3.library.db import Library
    from picframe3.library.scanner import Scanner

    db = Library(str(tmp_path / "library.db3"))
    Scanner(db, [str(photo_dir)]).scan()
    yield db
    db.close()
