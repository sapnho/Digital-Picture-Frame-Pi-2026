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


# ==========================================================================
# The control surface itself
# ==========================================================================
# The web interface is open on the LAN by design. That is a decision about
# passwords, not about safety, and these tests hold the difference: the route
# table has to be unambiguous, a website must not be able to reach in through
# the owner's browser, an unauthenticated body has to be bounded, and the one
# destructive action has to be switchable.

class _StubBus:
    """Records what the handlers ask the frame to do."""

    def __init__(self):
        self.commands = []

    def submit(self, command):
        self.commands.append(command)
        return True

    def subscribe(self, listener):
        return lambda: None


class _StubApp:
    """Enough of PictureFrameApp for the control routes to answer."""

    def __init__(self, **overrides):
        from picframe3.config import Config
        from picframe3.events import State

        self.config = Config()
        for dotted, value in overrides.items():
            section, _, name = dotted.partition(".")
            setattr(getattr(self.config, section), name, value)
        self.bus = _StubBus()
        self.started = 0.0
        self.current = ()
        self.geocoder = None
        self.removals = None
        self.saves = 0
        self._state = State(unsaved_changes=True)

    def state(self):
        return self._state

    def save_config(self):
        self.saves += 1

    def under_systemd(self):
        return True


def _server(**overrides):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from picframe3.control.http import HttpServer

    frame = _StubApp(**overrides)
    server = HttpServer(frame, frame.config.http)
    return frame, TestClient(server.api, raise_server_exceptions=False)


# -- the route table --------------------------------------------------------

def test_restart_reaches_the_restart_handler_and_saves_first():
    """The regression that cost real settings: `POST /api/{action}` was
    registered above `/api/restart`, Starlette matches in registration order,
    so "Save & restart" landed in the shortcut handler, `save` was never read,
    and the restart threw away the changes it was asked to apply."""
    frame, client = _server()
    reply = client.post("/api/restart?save=true")
    assert reply.status_code == 200
    body = reply.json()
    assert body["saved"] is True, "the shortcut handler cannot answer this"
    assert body["supervised"] is True
    assert frame.saves == 1
    assert [c.action.value for c in frame.bus.commands] == ["restart"]


def test_shutdown_reaches_its_own_handler_and_saves_first():
    """Same trap as the restart route: registered below `POST /api/{action}`
    it would land in the shortcut handler, `save` would never be read, and
    powering the frame off would take the unsaved settings with it."""
    frame, client = _server()
    reply = client.post("/api/shutdown?save=true")
    assert reply.status_code == 200
    assert reply.json()["saved"] is True, "the shortcut handler cannot answer this"
    assert frame.saves == 1
    assert [c.action.value for c in frame.bus.commands] == ["shutdown"]


def test_shutdown_can_still_be_asked_not_to_save():
    frame, client = _server()
    assert client.post("/api/shutdown?save=false").json()["saved"] is False
    assert frame.saves == 0


def test_a_frame_that_cannot_power_itself_off_says_so_rather_than_pretending():
    frame, client = _server()
    frame._state.can_shutdown = False
    reply = client.post("/api/shutdown")
    assert reply.status_code == 503
    assert "systemd" in reply.json()["detail"]
    assert frame.bus.commands == []


def test_restart_can_still_be_asked_not_to_save():
    frame, client = _server()
    assert client.post("/api/restart?save=false").json()["saved"] is False
    assert frame.saves == 0


def test_the_shortcut_route_still_serves_the_actions_that_have_no_handler():
    frame, client = _server()
    assert client.post("/api/next").json()["action"] == "next"
    assert client.post("/api/nonsense").status_code == 404


def test_no_explicit_api_route_is_shadowed_by_the_catch_all():
    """Held structurally as well, so the next route added below the catch-all
    fails here rather than in six months on somebody's wall."""
    from picframe3.control.http import HttpServer

    pytest.importorskip("fastapi")
    frame = _StubApp()
    routes = [r for r in HttpServer(frame, frame.config.http).api.routes
              if getattr(r, "path", "").startswith("/api/")]
    catch_all = next(i for i, r in enumerate(routes) if r.path == "/api/{action}")
    below = [r.path for r in routes[catch_all + 1:]
             if "POST" in getattr(r, "methods", set())]
    assert below == [], f"registered after the catch-all and unreachable: {below}"


# -- removing a picture -----------------------------------------------------

def test_delete_over_http_is_refused_when_the_setting_is_off():
    frame, client = _server(**{"http.allow_delete": False})
    reply = client.post("/api/command", json={"action": "delete"})
    assert reply.status_code == 403
    assert "allow_delete" in reply.text
    assert frame.bus.commands == [], "nothing may reach the frame"


def test_the_shortcut_route_cannot_sidestep_the_delete_switch():
    frame, client = _server(**{"http.allow_delete": False})
    assert client.post("/api/delete").status_code == 403
    assert frame.bus.commands == []


def test_delete_is_allowed_when_the_setting_is_on():
    frame, client = _server(**{"http.allow_delete": True})
    assert client.post("/api/command", json={"action": "delete"}).status_code == 200
    assert [c.action.value for c in frame.bus.commands] == ["delete"]


# -- a website must not be able to drive the frame --------------------------

def test_a_post_from_another_site_is_refused():
    """A POST with no body and no content type needs no preflight, so CORS
    never sees it: any page the owner visits could fire /api/delete."""
    frame, client = _server()
    reply = client.post("/api/restart",
                        headers={"Origin": "https://holiday-photos.example"})
    assert reply.status_code == 403
    assert frame.saves == 0 and frame.bus.commands == []


def test_a_referer_from_another_site_is_refused_too():
    frame, client = _server()
    reply = client.post("/api/next",
                        headers={"Referer": "https://holiday-photos.example/x"})
    assert reply.status_code == 403


def test_the_frames_own_page_is_left_alone():
    frame, client = _server()
    reply = client.post("/api/next", headers={"Origin": "http://testserver"})
    assert reply.status_code == 200
    assert [c.action.value for c in frame.bus.commands] == ["next"]


def test_a_request_with_no_origin_at_all_still_works():
    """curl, Home Assistant, a shell script. None of them is something a
    website can forge, and requiring a header they do not send would break
    every automation the frame has."""
    _, client = _server()
    assert client.post("/api/next").status_code == 200


@pytest.mark.parametrize("host", ["localhost:9000", "127.0.0.1:9000",
                                  "192.168.1.4:9000", "10.0.0.9", "[::1]:9000",
                                  "picture-frame.local:9000", "picframe",
                                  "picframe.fritz.box:9000", "picframe.lan",
                                  "169.254.3.7"])
def test_the_frames_own_addresses_are_accepted(host):
    _, client = _server()
    assert client.get("/api/state", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["frame.evil.example", "8.8.8.8",
                                  "photos.example.com:9000"])
def test_a_rebound_dns_name_is_refused(host):
    """DNS rebinding: the attacker's name is re-pointed at the frame's LAN
    address, the browser then treats the two as one origin and hands over
    every reply. The one thing that does not change is the Host header."""
    _, client = _server()
    assert client.get("/api/state", headers={"Host": host}).status_code == 403


def test_the_ui_itself_is_behind_the_same_check():
    _, client = _server()
    assert client.get("/", headers={"Host": "frame.evil.example"}).status_code == 403


# -- an unauthenticated request must not be able to cost much ---------------

def test_a_huge_body_is_refused_before_it_is_read():
    """Starlette buffers the whole body in memory, and the unit file restarts
    the frame on exit: one 1 GB POST is a reboot loop, not a crash."""
    from picframe3.control.http import MAX_BODY_BYTES

    _, client = _server()
    reply = client.post("/api/command", content=b"x" * (MAX_BODY_BYTES + 1024),
                        headers={"Content-Type": "application/json"})
    assert reply.status_code == 413


def test_a_normal_body_is_not_refused():
    _, client = _server()
    assert client.post("/api/command", json={"action": "next"}).status_code == 200


def test_the_server_limits_how_much_it_runs_at_once():
    """Without this uvicorn accepts every connection offered and the renderer
    shares the loop with all of them."""
    from picframe3.control.http import LIMIT_CONCURRENCY

    assert 0 < LIMIT_CONCURRENCY <= 128


# -- logging in, when the owner has asked for a password --------------------

def test_a_non_ascii_user_name_is_a_401_and_not_a_crash():
    """compare_digest refuses a str holding anything outside ASCII, so a
    configured user name with an umlaut turned every wrong password into a
    500 — and told the caller the frame was broken rather than that they were."""
    _, client = _server(**{"http.auth_user": "jörg", "http.auth_password": "seepferd"})
    reply = client.get("/api/state", auth=("someone", "wrong"))
    assert reply.status_code == 401


def test_the_right_password_still_gets_in():
    _, client = _server(**{"http.auth_user": "pi", "http.auth_password": "seepferd"})
    assert client.get("/api/state", auth=("pi", "seepferd")).status_code == 200
    assert client.get("/api/state", auth=("pi", "nope")).status_code == 401
    assert client.get("/api/state").status_code == 401


# -- the rest of the hardening ---------------------------------------------

def test_a_cors_wildcard_is_refused_rather_than_honoured():
    from picframe3.control.http import HttpServer

    pytest.importorskip("fastapi")
    frame = _StubApp(**{"http.cors_origins": ["*", "https://ha.example"]})
    assert HttpServer(frame, frame.config.http).cors_origins() == ["https://ha.example"]


def test_the_swagger_ui_is_not_served_and_says_so():
    """It loads its assets from a CDN the frame may have no route to; the
    schema itself is still there, behind the same guard."""
    _, client = _server()
    notice = client.get("/api/docs")
    assert notice.status_code == 200
    assert "openapi.json" in notice.text
    assert client.get("/api/openapi.json").json()["openapi"]


def test_a_setting_may_not_point_the_frame_at_a_shell_startup_file():
    frame, client = _server()
    reply = client.patch("/api/config", json={"logging.file": "~/.bashrc"})
    assert reply.status_code == 400
    assert frame.bus.commands == []
    assert client.post("/api/command", json={
        "action": "set_config", "key": "logging.file",
        "value": "/etc/profile.d/x.sh"}).status_code == 400


def test_a_setting_inside_an_allowed_root_is_still_accepted():
    frame, client = _server()
    assert client.patch("/api/config",
                        json={"logging.file": "/tmp/picframe.log"}).status_code == 200
    assert frame.bus.commands[0].payload["key"] == "logging.file"


def test_a_malformed_geo_preview_is_a_422_and_not_a_crash():
    """`{"key_order": [[[]]]}` reached format_address and came back a 500."""
    _, client = _server()
    assert client.post("/api/geo/preview", json={"key_order": [[[]]]}).status_code == 422
    assert client.post("/api/geo/preview", json={"detail": {"a": 1}}).status_code == 422
    assert client.post("/api/geo/preview", json={"detail": "town"}).status_code == 200


# -- one capture, however many people ask for it ---------------------------

def test_parallel_screenshots_share_one_capture():
    """There is a single capture slot in the render loop, so two requests
    arriving together used to overwrite each other's future: the first waited
    out its fifteen seconds and got a 503, the second got the picture. And
    every request costs a framebuffer read-back plus a PNG encode, which is
    the renderer's time."""
    import asyncio

    pytest.importorskip("fastapi")
    from picframe3.control.http import HttpServer

    frame = _StubApp()
    captures = []

    async def capture():
        captures.append(1)
        await asyncio.sleep(0.05)
        return b"\x89PNG-pretend"

    frame.screenshot = capture
    server = HttpServer(frame, frame.config.http)

    async def five_at_once():
        return await asyncio.gather(*(server._screenshot() for _ in range(5)))

    results = asyncio.run(five_at_once())
    assert {data for data, _ in results} == {b"\x89PNG-pretend"}
    assert len(captures) == 1, "five requests, one read-back"

    # And a sixth one arriving straight afterwards is handed the same PNG
    # rather than costing the renderer another frame.
    data, age = asyncio.run(server._screenshot())
    assert data == b"\x89PNG-pretend" and age > 0
    assert len(captures) == 1


# -- the guard, after a second look -----------------------------------------

def test_the_frame_answers_on_a_tailscale_address():
    """100.64.0.0/10 is not `is_private` to Python, and Tailscale uses all of it.

    Without this the owner reaching his own frame over the VPN is refused every
    single request -- including the page itself, which leaves no obvious way
    back in.
    """
    _, client = _server()
    reply = client.get("/api/state", headers={"Host": "100.101.102.103:9000"})
    assert reply.status_code == 200


def test_a_site_allowed_through_cors_may_also_change_something():
    """Otherwise `cors_origins` is a setting that does nothing.

    The preflight said yes and the guard then refused the request itself, so a
    dashboard listed in the setting could read the frame and change nothing.
    """
    origin = "http://homeassistant.local:8123"
    _, client = _server(**{"http.cors_origins": [origin]})
    reply = client.post("/api/next", headers={"Origin": origin})
    assert reply.status_code == 200

    _, strict = _server()
    refused = strict.post("/api/next", headers={"Origin": origin})
    assert refused.status_code == 403


def test_a_chunked_body_cannot_walk_past_the_size_limit():
    """The limit only looked at Content-Length, which a chunked body omits."""
    _, client = _server()
    reply = client.post("/api/command", content=iter([b'{"action":"next"}']),
                        headers={"content-type": "application/json"})
    assert reply.status_code == 411
