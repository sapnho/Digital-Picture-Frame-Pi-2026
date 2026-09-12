"""The JPEG the frame hands to something else.

One renderer serves the web interface's thumbnails, ``/api/current`` and the
picture published to Home Assistant, so its quirks -- orientation above all --
are worth pinning down once.
"""

import io

import pytest

from picframe3.media.preview import render_preview

Image = pytest.importorskip("PIL.Image")


def _write(path, size=(2400, 1600), colour=(180, 40, 40), exif=None):
    image = Image.new("RGB", size, colour)
    extra = {"exif": exif} if exif else {}
    image.save(path, "JPEG", **extra)
    return str(path)


def test_a_photograph_comes_back_as_a_jpeg_within_the_size(tmp_path):
    data = render_preview(_write(tmp_path / "a.jpg"), size=(480, 480))
    out = Image.open(io.BytesIO(data))
    assert out.format == "JPEG"
    assert max(out.size) == 480
    assert out.size == (480, 320), "the shape of the photograph is kept"


def test_a_rotated_photograph_is_turned_the_right_way_up(tmp_path):
    """EXIF orientation 6 is a portrait held sideways -- the case a dashboard
    shows on its side if this step is forgotten."""
    exif = Image.Exif()
    exif[274] = 6
    path = _write(tmp_path / "portrait.jpg", size=(1200, 900), exif=exif.tobytes())
    out = Image.open(io.BytesIO(render_preview(path, size=(400, 400))))
    assert out.height > out.width


def test_a_file_that_is_not_a_photograph_is_no_picture_rather_than_a_crash(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"nothing of the kind")
    assert render_preview(str(broken)) is None
    assert render_preview(str(tmp_path / "absent.jpg")) is None


# -- the endpoint ----------------------------------------------------------

class _StubFrame:
    """Enough of the app for the picture endpoints to answer."""

    def __init__(self, current):
        from picframe3.config import Config
        from picframe3.events import State

        self.config = Config()
        self._state = State(current=current)

    def state(self):
        return self._state


def _client(current):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from picframe3.control.http import HttpServer

    frame = _StubFrame(current)
    return TestClient(HttpServer(frame, frame.config.http).api, raise_server_exceptions=False)


def test_the_api_serves_the_picture_on_the_frame(tmp_path):
    path = _write(tmp_path / "wall.jpg")
    client = _client({"path": path, "is_video": False})

    reply = client.get("/api/current?size=600")
    assert reply.status_code == 200
    assert reply.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(reply.content)).size) == 600
    # The picture behind the URL changes every few minutes.
    assert "no-store" in reply.headers["cache-control"]


def test_the_api_says_so_when_the_frame_is_showing_nothing(tmp_path):
    assert _client({}).get("/api/current").status_code == 404
    missing = {"path": str(tmp_path / "gone.jpg"), "is_video": False}
    assert _client(missing).get("/api/current").status_code == 404
