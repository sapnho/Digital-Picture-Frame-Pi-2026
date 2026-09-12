"""Web interface: a small REST API, a live event stream, and a browser UI.

Everything the frame can do is reachable here, which makes the web UI, the
phone on the sofa and Home Assistant all first-class clients of the same
surface.  Server-sent events push state changes instead of the browser polling,
so an open tab costs nothing while the frame is idle.

The interface is open on the LAN by design -- a picture frame that asks for a
password before it will skip a photograph is a picture frame nobody uses.  Open
on the LAN is not the same as open to the web, though, and everything in
:class:`_LocalNetworkGuard` exists to keep that distinction true: a page on the
internet must not be able to reach in through the owner's own browser, and no
unauthenticated request may be allowed to cost the frame more than it costs the
caller.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import json
import logging
import os
import secrets
import socket
import time
from typing import Any
from urllib.parse import urlsplit

from ..config import HttpConfig
from ..events import Action, Command
from ..install import install_hint

_log = logging.getLogger(__name__)

# Imported at module scope on purpose.  This module uses
# ``from __future__ import annotations``, so FastAPI resolves every handler
# annotation as a string against the *module* globals -- a ``Request``
# imported inside a function is invisible to it, and the parameter silently
# becomes a required query argument (a 422 on every call).
try:
    from fastapi import (  # noqa: F401  (resolved lazily by FastAPI's annotations)
        Depends,
        FastAPI,
        HTTPException,
        Query,
        Request,
        Response,
        status,
    )
    from fastapi.responses import FileResponse, StreamingResponse  # noqa: F401
    from fastapi.security import HTTPBasic, HTTPBasicCredentials  # noqa: F401
    from fastapi.staticfiles import StaticFiles  # noqa: F401
    from pydantic import BaseModel, ConfigDict  # noqa: F401

    HAVE_FASTAPI = True
except ImportError:  # pragma: no cover - optional dependency
    HAVE_FASTAPI = False

WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
THUMB_SIZE = (480, 480)

#: The largest request body this interface will read.  Starlette buffers a body
#: in memory before a handler ever sees it, so without a limit an unauthenticated
#: ``POST`` of a gigabyte is enough to get the frame killed by the OOM killer --
#: and with ``Restart=always`` in the unit file that is a restart loop rather
#: than a single crash.  Nothing this API accepts is remotely near a megabyte.
MAX_BODY_BYTES = 1024 * 1024

#: How many requests uvicorn will have in flight at once.  Past this it answers
#: 503 rather than opening another connection, so a burst costs a rejection
#: instead of the memory of a thousand half-read requests -- on a machine whose
#: real job is drawing a picture on time.
LIMIT_CONCURRENCY = 32

#: Screenshots are expensive on both sides of the fence: the render loop has to
#: read the framebuffer back, and the result then has to be PNG-encoded.  Within
#: this window every caller is handed the capture that was just taken.
SCREENSHOT_MIN_INTERVAL = 2.0

#: The shortest gap between two pushes on the event stream.  State is published
#: whenever anything at all changes, which during a transition is several times
#: a second; a browser cannot draw that fast and the frame should not be
#: serialising it.  Updates inside the window are coalesced -- the newest state
#: replaces the one waiting, so nothing is stale and nothing is a backlog.
SSE_MIN_INTERVAL = 0.25

#: Refuse to decode an image larger than this over HTTP.  ``draft()`` makes a
#: huge JPEG cheap, but a PNG or a TIFF has no such shortcut: a 500-megapixel
#: PNG dropped into the guest share is 1.5 GB of RGB the moment anything asks
#: for its thumbnail.  The limit is on the *decode*, so serving the original
#: file is unaffected -- that is a byte-for-byte copy and costs nothing.
MAX_DECODE_PIXELS = 64_000_000


if HAVE_FASTAPI:

    class CommandBody(BaseModel):
        """``{"action": "next"}``, plus whatever that action's payload needs.

        Typed rather than a bare ``dict`` so a body with no ``action`` is a 422
        from the framework instead of a ``KeyError`` inside the handler.
        """

        model_config = ConfigDict(extra="allow")

        action: str

    class GeoPreviewBody(BaseModel):
        """The tiers editor's live preview.

        Every field is destructured by :mod:`~picframe3.media.geocode`, which
        expects strings inside lists inside a list.  ``{"key_order": [[[]]]}``
        used to reach it and come back as a 500; declared this way it is a 422
        naming the field, which is both honest and free.
        """

        model_config = ConfigDict(extra="ignore")

        detail: str | None = None
        key_order: list[list[str]] | None = None
        suppress: list[str] | None = None


# --------------------------------------------------------------------------
# Keeping an open interface local
# --------------------------------------------------------------------------

#: Methods that change something.  ``GET`` and ``HEAD`` are not on the list
#: because the ``Host`` check below is what protects them.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Name endings that can only ever mean a machine on this network.  A dotted
#: name is otherwise refused, which would lock out anyone whose router hands
#: out a domain of its own -- and the commonest router in the houses this frame
#: is built for hands out ``fritz.box``.  None of these can be registered on
#: the public internet, so none of them can be rebound at the frame.
LAN_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal",
                ".localdomain", ".localhost", ".fritz.box")

#: An escape hatch for the frame that is reached under a name this cannot
#: guess -- behind a reverse proxy, say.  Colon- or comma-separated.  It exists
#: because being locked out of your own picture frame by a hostname rule is a
#: worse outcome than the rule itself prevents.
ALLOWED_HOSTS_ENV = "PICFRAME3_ALLOWED_HOSTS"

#: 100.64.0.0/10, the carrier-grade NAT range.  Not `is_private` to Python, but
#: it is what Tailscale uses for every node on a tailnet, and a frame reached
#: over the VPN is being reached by its owner.
CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")


def _header(scope: dict, name: str) -> str:
    """One request header from a raw ASGI scope, lowercased name, "" if absent."""
    wanted = name.encode("latin-1")
    for key, value in scope.get("headers", ()):
        if key == wanted:
            return value.decode("latin-1")
    return ""


def _split_host(value: str) -> tuple[str, str]:
    """``"192.168.1.4:9000"`` -> ``("192.168.1.4", "9000")``; IPv6 brackets kept."""
    text = value.strip().lower()
    if text.startswith("["):                      # [::1]:9000
        host, _, rest = text.partition("]")
        return host.lstrip("["), rest.lstrip(":")
    host, _, port = text.partition(":")
    return host, port


def host_is_local(host: str, allowed: set[str]) -> bool:
    """Whether a ``Host`` header may be the frame's own address.

    This is the DNS-rebinding check.  An attacker's page cannot read a reply
    from ``http://192.168.1.4:9000/api/config`` -- the same-origin policy stops
    it -- so instead they publish ``frame.evil.example`` with a one-second TTL,
    point it at their own server, then re-point it at the frame's LAN address.
    The browser now believes the two are the same origin and hands over every
    reply.  The one thing that does not change through all of that is the
    ``Host`` header, which still says ``frame.evil.example``.

    So: a literal loopback, private or link-local address is fine (that is what
    the owner types, and what a rebinding attack cannot make the browser send);
    an ``.local`` mDNS name is fine; a name with no dot in it is fine, because a
    public resolver cannot answer one; anything else -- which is to say every
    name that can be registered on the internet -- is refused.
    """
    name, _ = _split_host(host)
    if not name:
        # HTTP/1.0 and some scripted clients send none.  A browser always does,
        # so an absent header is never the attack this guards against.
        return True
    if name in allowed:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return name.endswith(LAN_SUFFIXES) or "." not in name
    if address.is_loopback or address.is_private or address.is_link_local:
        return True
    # Python does not count the carrier-grade NAT range as private, but
    # Tailscale hands every machine on a tailnet an address in it -- so without
    # this the owner reaching his own frame over the VPN is refused every
    # request, including the page itself, with no obvious way back in.
    return address in CGNAT_V4


def origin_matches_host(origin: str, host: str) -> bool:
    """Whether a browser's ``Origin``/``Referer`` is this same interface."""
    parts = urlsplit(origin if "//" in origin else f"//{origin}")
    if not parts.netloc:
        return False
    origin_host, origin_port = _split_host(parts.netloc)
    request_host, request_port = _split_host(host)
    # A missing port means the scheme's default, and this interface is plain
    # HTTP on a port the owner chose, so comparing what was written is both
    # sufficient and impossible to get subtly wrong.
    return (origin_host, origin_port) == (request_host, request_port)


class _LocalNetworkGuard:
    """One piece of ASGI middleware holding three unrelated doors shut.

    Written as raw ASGI rather than ``BaseHTTPMiddleware`` because the event
    stream is a long-lived ``StreamingResponse`` that asks the request whether
    the client has gone; the http-middleware wrapper interposes its own
    plumbing there and the stream stops noticing a closed tab.

    What it enforces, in order:

    1. **A body limit.**  ``Content-Length`` over :data:`MAX_BODY_BYTES` is
       refused before a byte is read.
    2. **The ``Host`` header.**  See :func:`host_is_local` -- this is what stops
       a page on the internet from reading ``/api/config`` through the owner's
       own browser.
    3. **``Origin``/``Referer`` on anything that changes state.**  A ``POST``
       with no body and no content type is a "simple request": no preflight, so
       CORS never gets a say, so any site the owner visits could quietly fire
       ``/api/delete`` or ``/api/restart``.  A browser always labels such a
       request with its ``Origin``, and one that does not match this host is
       refused.  A request with neither header is left alone: that is curl,
       Home Assistant or a shell script, none of which a website can forge.
    """

    def __init__(self, app, *, max_body: int = MAX_BODY_BYTES,
                 allowed_hosts: set[str] | None = None,
                 allowed_origins: list[str] | None = None):
        self.app = app
        self.max_body = max_body
        self.allowed_hosts = allowed_hosts or set()
        #: Sites named in `http.cors_origins`.  Without this the CORS policy
        #: was a setting that did nothing for anything that changes state: the
        #: preflight said yes and the request itself was then refused here.
        self.allowed_origins = {
            o.strip().rstrip("/").lower() for o in (allowed_origins or []) if o.strip()
        }

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        refusal = self._refuse(scope)
        if refusal is not None:
            status_code, message = refusal
            response = Response(message, status_code=status_code,
                                media_type="text/plain; charset=utf-8")
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)

    def _refuse(self, scope: dict) -> tuple[int, str] | None:
        length = _header(scope, "content-length")
        if length.isdigit() and int(length) > self.max_body:
            return (413, f"the request body may not exceed {self.max_body} bytes")
        if not length and "chunked" in _header(scope, "transfer-encoding").lower():
            # A chunked body announces no length, so the check above cannot see
            # it -- and Starlette buffers the whole thing before a handler runs.
            # Nothing this API accepts needs chunked encoding.
            return (411, "this interface needs a Content-Length; "
                         "chunked request bodies are not accepted")

        host = _header(scope, "host")
        if not host_is_local(host, self.allowed_hosts):
            return (403, "this interface only answers on the frame's own "
                         "address on your network; the Host header "
                         f"{host!r} is not one of them")

        if scope.get("method", "").upper() in UNSAFE_METHODS:
            source = _header(scope, "origin") or _header(scope, "referer")
            if source and not self._origin_allowed(source, host):
                return (403, f"refusing a request from {source!r}: this "
                             "interface only accepts changes from its own page")
        return None

    def _origin_allowed(self, source: str, host: str) -> bool:
        if origin_matches_host(source, host):
            return True
        parts = urlsplit(source if "//" in source else f"//{source}")
        origin = f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme else ""
        return bool(origin) and origin.rstrip("/") in self.allowed_origins


class HttpServer:
    def __init__(self, app, config: HttpConfig):
        self.app = app
        self.config = config
        if not HAVE_FASTAPI:
            raise RuntimeError(
                f"the web interface needs fastapi and uvicorn ({install_hint('web')})"
            )
        try:
            import uvicorn  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                f"the web interface needs uvicorn ({install_hint('web')})"
            ) from exc
        #: The capture every concurrent ``/api/screenshot`` waits on, and the
        #: last PNG it produced.  See :meth:`_screenshot`.
        self._shot_task: asyncio.Task | None = None
        self._shot_data: bytes | None = None
        self._shot_at = 0.0
        self.api = self._build()

    # ------------------------------------------------------------------
    def allowed_hosts(self) -> set[str]:
        """Names that count as "this frame", besides the addresses it has.

        The frame's own hostname, because ``http://picture-frame.local:9000/``
        is how the owner reaches it, and whatever ``http.host`` was set to when
        that is a name rather than an address.
        """
        names = {"localhost"}
        try:
            hostname = socket.gethostname().lower()
        except OSError:                            # pragma: no cover
            hostname = ""
        if hostname:
            names |= {hostname, hostname.split(".")[0], f"{hostname.split('.')[0]}.local"}
        configured = str(self.config.host or "").strip().lower()
        if configured and configured not in ("0.0.0.0", "::", ""):
            names.add(configured)
        for extra in getattr(self.config, "allowed_hosts", None) or ():
            if str(extra).strip():
                names.add(str(extra).strip().lower())
        for extra in os.environ.get(ALLOWED_HOSTS_ENV, "").replace(",", ":").split(":"):
            if extra.strip():
                names.add(extra.strip().lower())
        return names

    def cors_origins(self) -> list[str]:
        """The CORS origins actually honoured, with ``*`` thrown out.

        ``allow_origins=["*"]`` on an interface with no password means every
        page on the web may drive the whole API -- change the settings, remove
        photographs -- on behalf of anyone who visits it from the house.  That
        is never what someone means by "let my dashboard embed this", so the
        wildcard is refused out loud rather than honoured quietly.
        """
        origins = []
        for entry in self.config.cors_origins or []:
            text = str(entry).strip()
            if text in ("*", "null"):
                _log.warning(
                    "ignoring cors_origins entry %r: a wildcard would let any "
                    "website on the internet drive this frame. List the sites "
                    "you mean, e.g. https://homeassistant.local:8123", text)
                continue
            if text:
                origins.append(text)
        return origins

    def _build(self):
        api = FastAPI(
            title="picframe3",
            version=getattr(self.app, "version", "3"),
            # The interactive documentation is off, deliberately and always.
            # Swagger UI loads its JavaScript and CSS from a CDN, and a frame
            # on a home network -- the one machine here with no browser and
            # possibly no route to the internet -- renders it as a blank page.
            # The schema itself is still served, behind the same guard as
            # everything else, at /api/openapi.json.
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
        origins = self.cors_origins()
        if origins:
            from fastapi.middleware.cors import CORSMiddleware

            api.add_middleware(
                CORSMiddleware,
                allow_origins=origins,
                # Named rather than "*": a wildcard here waves through methods
                # and headers nothing in this API uses, and the browser's
                # preflight is one of the few checks an open interface gets.
                allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
                allow_headers=["Content-Type", "Authorization"],
            )
        # Added last, so it wraps everything including CORS and the static
        # files: a request that should not be answered is not answered by
        # anything.
        api.add_middleware(_LocalNetworkGuard, max_body=MAX_BODY_BYTES,
                           allowed_hosts=self.allowed_hosts(),
                           allowed_origins=origins)

        auth = self._auth_dependency()
        guard = [Depends(auth)] if auth else []

        # -- state -----------------------------------------------------
        @api.get("/api/state", dependencies=guard)
        async def get_state():
            return self.app.state().as_dict()

        @api.get("/api/events", dependencies=guard)
        async def events(request: Request):
            # One slot rather than a queue: what a browser wants is the newest
            # state, not every state.  A listener that arrives while the last
            # one is still being written simply replaces it, so a burst of
            # updates during a transition costs one push instead of eight, and
            # a slow client can never build a backlog to be dropped later.
            latest: dict[str, Any] | None = None
            wakeup = asyncio.Event()

            def listener(state):
                nonlocal latest
                latest = state.as_dict()
                wakeup.set()

            unsubscribe = self.app.bus.subscribe(listener)

            async def stream():
                nonlocal latest
                try:
                    yield _sse(self.app.state().as_dict())
                    while True:
                        if await request.is_disconnected():
                            break
                        try:
                            await asyncio.wait_for(wakeup.wait(), timeout=20.0)
                        except TimeoutError:
                            yield ": keep-alive\n\n"
                            continue
                        # Let whatever else is about to change land in the same
                        # push before sending: the frame publishes several
                        # times a second while a picture is changing.
                        await asyncio.sleep(SSE_MIN_INTERVAL)
                        wakeup.clear()
                        payload, latest = latest, None
                        if payload is not None:
                            yield _sse(payload)
                finally:
                    unsubscribe()

            return StreamingResponse(stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no"})

        # -- which pictures are in the running -------------------------
        @api.get("/api/filters", dependencies=guard)
        async def get_filters():
            """The filter now in force, and everything a filter panel offers."""
            # Counting the library, every folder and every tag is several full
            # table scans.  On the event loop that is the renderer's loop, so a
            # filter panel left open made the cross-fade stutter; in a worker
            # thread it is somebody else's problem.  Each Library connection
            # belongs to one thread, which is what makes this safe.
            return await _in_thread(self._filters_payload)

        @api.post("/api/filters", dependencies=guard)
        async def set_filters(body: dict | None = None):
            """Change one or more filters.  Absent keys are left alone.

            A patch rather than a whole filter set because that is what a
            control surface actually has: one text box, one dropdown, one
            switch.  ``{"reset": true}`` clears everything.
            """
            self.app.bus.submit(
                Command(Action.SET_FILTERS, dict(body or {}), source="http"))
            return {"ok": True}

        @api.post("/api/filters/preview", dependencies=guard)
        async def preview_filters(body: dict | None = None):
            """How many pictures a filter *would* select, without applying it.

            What lets the panel count down as you type instead of making you
            apply a filter to find out it matches nothing.  Counted in a worker
            thread: this runs on every keystroke.
            """
            trial = self.app.playlist.filters.merged(dict(body or {}))
            matching = await _in_thread(self.app.playlist.count_for, trial)
            return {"matching": matching, "filters": trial.as_dict()}

        # -- commands --------------------------------------------------
        @api.post("/api/command", dependencies=guard)
        async def post_command(body: CommandBody):
            command = Command.parse(body.model_dump(), source="http")
            if command is None:
                raise HTTPException(400, f"unknown action {body.action!r}")
            self._vet(command)
            self.app.bus.submit(command)
            return {"ok": True, "action": command.action.value}

        # -- configuration ---------------------------------------------
        @api.get("/api/config", dependencies=guard)
        async def get_config():
            from ..uischema import redact

            return redact(self.app.config.as_dict())

        @api.post("/api/geo/preview", dependencies=guard)
        async def geo_preview(body: GeoPreviewBody):
            """What a set of address tiers would write under a photograph.

            Previewed against a picture the frame has actually shown whenever
            its address is in the geocache, because seeing your own caption
            change as you edit teaches the rule in one go; otherwise against a
            stored example reply. Never makes a network request.
            """
            from ..media.geocode import (
                EXAMPLE_ADDRESS,
                format_address,
                key_order_for,
            )

            address, source = EXAMPLE_ADDRESS, "an example"
            record = self.app.current[0] if self.app.current else None
            if record is not None and self.app.geocoder is not None:
                found = self.app.geocoder.cached_address(record.latitude, record.longitude)
                if found:
                    address, source = found, record.basename

            detail = body.detail or "full"
            tiers = body.key_order or self.app.config.geo.key_order
            order = key_order_for(detail, tiers)
            suppress = body.suppress
            if suppress is None:
                suppress = self.app.config.geo.suppress
            return {
                "text": format_address(address, order, suppress) or "",
                "source": source,
                "available": sorted(k for k, v in address.items() if v),
            }

        @api.get("/api/config/schema", dependencies=guard)
        async def config_schema():
            """Every setting, with enough about each one to draw a control.

            Generated from the dataclasses, so the settings page cannot fall
            behind the code: a field added to the config appears here, and on
            the page, without anyone remembering to list it.
            """
            from ..uischema import schema

            # Option lists that can only come from the running frame: the
            # folders that are actually in the library, and the address keys
            # this photograph's own reply happens to carry.
            folders = await _in_thread(self.app.library.folders)
            options = [{"name": path, "label": f"{path}  ({count})"}
                       for path, count in folders]
            return schema(self.app.config, extra_options={"folders": options})

        @api.patch("/api/config", dependencies=guard)
        async def patch_config(body: dict, persist: bool = Query(False)):
            applied: dict[str, Any] = {}
            for key, value in body.items():
                self._vet_setting(str(key), value)
            for key, value in body.items():
                self.app.bus.submit(
                    Command(Action.SET_CONFIG, {"key": key, "value": value}, source="http")
                )
                applied[key] = value
            if persist:
                await asyncio.sleep(0.2)     # let the settings land first
                self.app.save_config()
            return {"ok": True, "applied": applied, "saved": persist}

        @api.post("/api/restart", dependencies=guard)
        async def restart(save: bool = Query(True)):
            """Stop cleanly and come back up.

            Several settings -- the MQTT broker, the HTTP port, which folders
            are indexed -- can only be picked up by a fresh process.  Saving
            first is the default because a restart would otherwise throw away
            the very changes it is being asked to apply.
            """
            saved = False
            if save and self.app.state().unsaved_changes:
                self.app.save_config()
                saved = True
            self.app.bus.submit(Command(Action.RESTART, source="http"))
            return {"ok": True, "saved": saved,
                    "supervised": self.app.under_systemd()}

        @api.post("/api/shutdown", dependencies=guard)
        async def shutdown(save: bool = Query(True)):
            """Power the Pi off, so the plug can be pulled safely.

            Not a restart: nothing brings the frame back until somebody
            switches the power on again.  Saving first is the default for the
            same reason it is on a restart -- the settings would otherwise be
            lost -- and it has a handler of its own rather than going through
            the shorthand route so that ``save`` is actually read.

            Whether the frame can do it at all is in the state document
            (``can_shutdown``), so the answer here and the button in the web
            interface cannot disagree.
            """
            if not self.app.state().can_shutdown:
                raise HTTPException(
                    503,
                    "this frame has no systemd to ask for a power-off; "
                    "shut the Pi down at the command line instead.",
                )
            saved = False
            if save and self.app.state().unsaved_changes:
                self.app.save_config()
                saved = True
            self.app.bus.submit(Command(Action.SHUTDOWN, source="http"))
            return {"ok": True, "saved": saved}

        # -- syncthing -------------------------------------------------
        # Syncthing is another program with its own web interface, and this is
        # not an attempt to reimplement it: it is the four things somebody
        # setting up a frame needs before Syncthing's own page is any use --
        # is it there, turn it on, keep this folder, pair with that phone.
        @api.get("/api/sync", dependencies=guard)
        async def sync_status():
            from .. import sync as sync_module

            return await _in_thread(sync_module.status, self.app.config)

        @api.post("/api/sync/switch", dependencies=guard)
        async def sync_switch(body: dict | None = None):
            """Turn Syncthing on -- installing it if it is missing -- or off.

            Deliberately the same path as the switch on the settings page:
            this writes ``sync.enabled`` through the command bus, and the
            applier does the rest on a thread.  Saved at once, because
            installing a package and enabling a service is not a change that
            should be lost to a restart.
            """
            on = bool((body or {}).get("on", True))
            self.app.bus.submit(Command(Action.SET_CONFIG,
                                        {"key": "sync.enabled", "value": on},
                                        source="http"))
            await asyncio.sleep(0.2)         # let the setting land first
            self.app.save_config()
            return {"ok": True, "enabled": on}

        @api.post("/api/sync/folder", dependencies=guard)
        async def sync_folder():
            """Create or re-align the frame's folder, and its own web page."""
            from .. import sync as sync_module

            if not sync_module.installed():
                raise HTTPException(503, "Syncthing is not installed on this "
                                         "frame yet.")
            if not sync_module.is_active(sync_module.unit()):
                raise HTTPException(503, "Syncthing is installed but not "
                                         "running.")
            try:
                await _in_thread(sync_module.configure, self.app.config)
            except sync_module.SyncError as exc:
                raise HTTPException(503, str(exc)) from exc
            return await _in_thread(sync_module.status, self.app.config)

        @api.post("/api/sync/device", dependencies=guard)
        async def sync_add_device(body: dict | None = None):
            """Pair with another machine and share the pictures with it."""
            from .. import sync as sync_module

            payload = body or {}
            device_id = str(payload.get("device_id") or "")
            name = str(payload.get("name") or "")[:64]
            try:
                await _in_thread(sync_module.add_device, device_id, name,
                                 self.app.config)
            except sync_module.SyncError as exc:
                raise HTTPException(400, str(exc)) from exc
            return await _in_thread(sync_module.status, self.app.config)

        @api.post("/api/sync/device/remove", dependencies=guard)
        async def sync_remove_device(body: dict | None = None):
            from .. import sync as sync_module

            device_id = str((body or {}).get("device_id") or "")
            try:
                await _in_thread(sync_module.remove_device, device_id)
            except sync_module.SyncError as exc:
                raise HTTPException(400, str(exc)) from exc
            return await _in_thread(sync_module.status, self.app.config)

        # -- library ---------------------------------------------------
        # Everything below reads SQLite, so everything below reads it in a
        # worker thread: a library of forty thousand photographs is several
        # hundred milliseconds of COUNT(*), and the event loop these handlers
        # share is the one drawing the cross-fade.
        @api.get("/api/library", dependencies=guard)
        async def library_stats():
            return await _in_thread(self.app.library.stats)

        @api.get("/api/library/folders", dependencies=guard)
        async def folders():
            rows = await _in_thread(self.app.library.folders)
            return [{"path": p, "count": n} for p, n in rows]

        @api.get("/api/library/tags", dependencies=guard)
        async def tags():
            rows = await _in_thread(self.app.library.all_tags)
            return [{"name": t, "count": n} for t, n in rows]

        @api.get("/api/library/locations", dependencies=guard)
        async def locations():
            rows = await _in_thread(self.app.library.locations)
            return [{"name": p, "count": n} for p, n in rows]

        @api.get("/api/library/photos", dependencies=guard)
        async def photos(q: str = "", limit: int = Query(60, le=500), offset: int = 0,
                         selected: bool = False):
            records = await _in_thread(self._photos, q, limit, offset, selected)
            return [_summary(r) for r in records]

        @api.get("/api/library/photo/{file_id}", dependencies=guard)
        async def photo(file_id: int):
            record = await _in_thread(self.app.library.get, file_id)
            if record is None:
                raise HTTPException(404, "not found")
            return record.as_dict()

        @api.get("/api/library/photo/{file_id}/thumb", dependencies=guard)
        async def thumb(file_id: int, v: str = Query("")):
            record = await _in_thread(self.app.library.get, file_id)
            if record is None:
                raise HTTPException(404, "not found")
            return await _thumbnail_response(record.path, bool(record.is_video),
                                             versioned=bool(v))

        @api.get("/api/library/photo/{file_id}/file", dependencies=guard)
        async def original(file_id: int):
            record = await _in_thread(self.app.library.get, file_id)
            if record is None or not os.path.exists(record.path):
                raise HTTPException(404, "not found")
            # No pixel budget here on purpose: this copies bytes off the disk
            # and never decodes them, so a 500-megapixel TIFF costs the frame
            # nothing but the read.
            return FileResponse(record.path)

        # -- removed pictures ------------------------------------------
        # The journal, not the folder listing: the folder only knows there is
        # a file called IMG_4312.jpg, while the journal knows it was taken in
        # Lisbon in 2019, removed on Tuesday from the web UI, and which folder
        # it belongs back in.
        @api.get("/api/removed", dependencies=guard)
        async def removed(include_restored: bool = Query(False),
                          include_purged: bool = Query(False),
                          limit: int = Query(200, le=2000)):
            log = self.app.removals
            if log is None:
                return []
            entries = log.entries(include_restored=include_restored,
                                  include_purged=include_purged, newest_first=True)
            # Which of these are still being held out, and which have turned up
            # on the disk again.  One query for the page rather than one per
            # row: the holds table is small, and the page draws two hundred.
            holds = ({h["stored_as"]: h for h in self.app.library.holds()}
                     if self.app.library is not None else {})
            return [_removal(e, log.folder, holds.get(e.get("stored_as") or ""))
                    for e in entries[:limit]]

        @api.get("/api/removed/summary", dependencies=guard)
        async def removed_summary():
            log = self.app.removals
            out = log.summary() if log is not None else {}
            if self.app.library is not None:
                out.update(self.app.library.hold_summary())
            return out

        @api.post("/api/removed/empty", dependencies=guard)
        async def empty_trash():
            """Delete everything in the trash for good.  The journal stays.

            Declared above the ``{stored_as}`` routes on purpose: FastAPI
            matches in order, and a literal segment registered afterwards can
            be swallowed by the placeholder in front of it.
            """
            result = await self.app._empty_trash(source="http")
            if not result.get("ok"):
                raise HTTPException(403, result.get("error", "cannot empty the trash"))
            return result

        @api.post("/api/removed/{stored_as}/purge", dependencies=guard)
        async def purge(stored_as: str):
            """Delete one removed picture for good.  Its journal line stays."""
            entry, _ = _removed_file(self.app, stored_as)
            if entry is None:
                raise HTTPException(404, "not found")
            result = await self.app._purge_removed(entry["stored_as"], source="http")
            if not result.get("ok"):
                raise HTTPException(403, result.get("error", "cannot delete it"))
            return result

        @api.get("/api/removed/{stored_as}/thumb", dependencies=guard)
        async def removed_thumb(stored_as: str, v: str = Query("")):
            entry, path = _removed_file(self.app, stored_as)
            if entry is None or not os.path.exists(path):
                raise HTTPException(404, "not found")
            return await _thumbnail_response(path, bool(entry.get("is_video")),
                                             versioned=bool(v))

        @api.get("/api/removed/{stored_as}/file", dependencies=guard)
        async def removed_file(stored_as: str):
            entry, path = _removed_file(self.app, stored_as)
            if entry is None or not os.path.exists(path):
                raise HTTPException(404, "not found")
            return FileResponse(path)

        @api.post("/api/removed/{stored_as}/allow", dependencies=guard)
        async def allow(stored_as: str):
            """Say this picture may be shown again -- the only thing that does.

            Not behind ``allow_delete``: that switch is there to stop the
            network removing pictures, and this puts one back.
            """
            name = os.path.basename(str(stored_as or ""))
            if not name or name != stored_as:
                raise HTTPException(404, "not found")
            result = await self.app._release_removed(name, source="http")
            if not result.get("ok"):
                raise HTTPException(404, result.get("error", "cannot release it"))
            return result

        @api.post("/api/removed/{stored_as}/restore", dependencies=guard)
        async def restore(stored_as: str):
            """Put it back where it came from, and say where that was."""
            result = await self.app._restore_removed(stored_as)
            if not result.get("ok"):
                raise HTTPException(404, result.get("error", "cannot restore"))
            return result

        @api.get("/api/removed/journal", dependencies=guard)
        async def removed_journal():
            """The raw journal file, for keeping or reading elsewhere."""
            log = self.app.removals
            if log is None or not os.path.exists(log.path):
                raise HTTPException(404, "nothing has been removed yet")
            return FileResponse(log.path, media_type="application/x-ndjson",
                                filename="removals.jsonl")

        @api.get("/api/current", dependencies=guard)
        async def current_picture(size: int = Query(1280, ge=64, le=3840)):
            """A JPEG of the photograph that is on the frame right now.

            The picture itself, not the screen -- so it is there while the
            display is off, and a Generic Camera in Home Assistant can point at
            it without the frame having to draw anything.  ``/api/screenshot``
            is the other question: what the panel actually looks like.
            """
            from ..media.preview import render_preview

            current = self.app.state().current or {}
            path = current.get("path")
            if not path or not os.path.exists(path):
                raise HTTPException(404, "no picture is on the frame")
            is_video = bool(current.get("is_video"))
            too_big = await _in_thread(_too_many_pixels, path, is_video)
            if too_big:
                raise HTTPException(413, too_big)
            data = await _in_thread(render_preview, path, is_video, (size, size), 82)
            if data is None:
                raise HTTPException(415, "cannot render this file as a picture")
            return Response(data, media_type="image/jpeg", headers={
                # The picture behind one URL changes every few minutes, so a
                # cached copy would be the wrong one within the hour.
                "Cache-Control": "no-store",
            })

        @api.get("/api/screenshot", dependencies=guard)
        async def screenshot():
            """A PNG of what is actually on the frame's screen."""
            try:
                data, age = await self._screenshot()
            except TimeoutError:
                raise HTTPException(
                    503,
                    "the frame did not draw in time — it is probably busy "
                    "preparing a slide or scanning; try again in a moment",
                ) from None
            except Exception as exc:
                raise HTTPException(500, f"could not capture the screen: {exc}") from exc
            return Response(data, media_type="image/png", headers={
                "Cache-Control": "no-store",
                "Content-Disposition": 'inline; filename="picframe.png"',
                # So a caller can tell a fresh capture from the one it was
                # handed because it asked again too soon.
                "X-Capture-Age": f"{age:.1f}",
            })

        @api.get("/api/transitions", dependencies=guard)
        async def transition_names():
            from ..gfx import transitions

            return transitions.names()

        @api.get("/api/caption-fields", dependencies=guard)
        async def caption_fields():
            """What can be written over a picture, and what to call it."""
            from ..gfx.overlays import CAPTION_FIELDS

            return [{"name": name, "label": label} for name, label in CAPTION_FIELDS]

        @api.get("/api/mat-styles", dependencies=guard)
        async def mat_styles():
            """The mat styles, with the wording the settings page uses."""
            from ..media.mat import STYLE_LABELS, STYLES

            return [{"name": name, "label": STYLE_LABELS.get(name, name)}
                    for name in STYLES]

        @api.get("/api/fits", dependencies=guard)
        async def fit_modes():
            """What `fit: auto` may do with a picture that needs help."""
            labels = {
                "mat": "In a mat (passepartout)",
                "blur": "Whole, on a blurred copy of itself",
                "contain": "Whole, on the background colour",
                "cover": "Cropped to fill the screen",
            }
            from ..media.prepare import AUTO_FITS

            return [{"name": name, "label": labels[name]} for name in AUTO_FITS]

        @api.get("/api/date-windows", dependencies=guard)
        async def date_windows():
            """The rolling date filters, in the order they should be offered.

            A rule rather than a pair of dates: picking "Last 7 days" stores
            the rule, and the frame works the dates out again every time it
            asks the library, so the filter still means the last seven days
            next month.
            """
            from ..library.playlist import DATE_WINDOWS

            current = (self.app.playlist.filters.date_window
                       if self.app.playlist else "") or "all"
            return [{"name": name, "label": label, "selected": name == current}
                    for name, label in DATE_WINDOWS.items()]

        @api.get("/api/geo-detail", dependencies=guard)
        async def geo_detail():
            """How much of an address a caption may show."""
            from ..media.geocode import DETAIL_LABELS

            return [{"name": name, "label": label}
                    for name, label in DETAIL_LABELS.items()]

        @api.get("/api/openapi.json", dependencies=guard)
        async def openapi_schema():
            """The machine-readable description, behind the same door as the rest."""
            return api.openapi()

        @api.get("/api/docs", dependencies=guard)
        async def docs_notice():
            """Why there is no Swagger UI here, and where the schema is."""
            return Response(
                "picframe3 does not serve interactive API documentation: "
                "Swagger UI fetches its assets from a CDN, and the frame is "
                "often on a network with no route to one.\n\n"
                "The schema is at /api/openapi.json; point any OpenAPI client "
                "at it.\n",
                media_type="text/plain; charset=utf-8",
            )

        @api.get("/api/health")
        async def health():
            return {"ok": True, "uptime": round(time.monotonic() - self.app.started, 1)}

        # -- the catch-all, and it has to be last ----------------------
        # Starlette matches routes in registration order, so this one swallows
        # every single-segment /api/... POST declared after it.  It used to sit
        # in the middle of the file, which is how POST /api/restart?save=true
        # ended up here instead of in the restart handler: the query parameter
        # was never read, and "Save & restart" silently threw away the very
        # changes it was asked to apply.  Nothing may be registered below this
        # line except the static files.
        @api.post("/api/{action}", dependencies=guard)
        async def shortcut(action: str, body: dict | None = None):
            payload = dict(body or {})
            payload["action"] = action
            command = Command.parse(payload, source="http")
            if command is None:
                raise HTTPException(404, f"no such action {action!r}")
            self._vet(command)
            self.app.bus.submit(command)
            return {"ok": True, "action": command.action.value}

        if os.path.isdir(WEB_ROOT):
            api.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")
        else:  # pragma: no cover
            _log.warning("web assets missing at %s", WEB_ROOT)
        return api

    # ------------------------------------------------------------------
    # What this surface is allowed to ask for
    # ------------------------------------------------------------------
    def _vet(self, command: Command) -> None:
        """Refuse a command the network is not permitted to give.

        The frame's own buttons are a different matter: somebody standing in
        front of it has already proved more than any header can.
        """
        if command.action is Action.DELETE and not self.app.config.http.allow_delete:
            raise HTTPException(
                403,
                "removing pictures over the network is switched off "
                "(http.allow_delete). The frame's own buttons can still do it.",
            )
        if command.action is Action.SET_CONFIG:
            self._vet_setting(str(command.payload.get("key") or ""),
                              command.payload.get("value"))

    def _vet_setting(self, key: str, value: Any) -> None:
        """Refuse a setting that would make the frame write somewhere it should not."""
        from ..uischema import check_path_setting

        problem = check_path_setting(key, value, self.app.config)
        if problem:
            raise HTTPException(400, problem)

    # ------------------------------------------------------------------
    # Work that is too slow for the render loop
    # ------------------------------------------------------------------
    def _filters_payload(self) -> dict[str, Any]:
        """Everything the filter panel draws itself from.  Called in a thread."""
        return {
            "filters": self.app.playlist.filters.as_dict(),
            "matching": self.app.playlist.size,
            "total": self.app.library.stats().get("files", 0),
            "folders": [{"name": name, "count": count}
                        for name, count in self._folder_counts()],
            "tags": [{"name": t, "count": n}
                     for t, n in self.app.library.all_tags()],
            "locations": [{"name": p, "count": n}
                          for p, n in self.app.library.locations()[:200]],
        }

    def _photos(self, q: str, limit: int, offset: int, selected: bool) -> list:
        """The library grid's page of records.  Called in a thread."""
        if selected:
            # Exactly what the slideshow is drawing from, in the same order
            # the grid shows everything else: newest first.  A search inside
            # the selection reads further down it before trimming, so
            # searching does not silently look at one page.
            reach = min(limit * 10, 2000) if q else limit
            ids = self.app.playlist.selection(limit=reach, offset=offset)
            records = [r for r in (self.app.library.get(i) for i in ids)
                       if r is not None]
            if q:
                needle = q.casefold()
                records = [r for r in records if needle in " ".join(
                    str(x) for x in (r.basename, r.title, r.caption, r.location,
                                     *(r.tags or ()))).casefold()][:limit]
            return records
        if q:
            return self.app.library.search(q, limit=limit)
        return self.app.library.query(
            "SELECT * FROM files WHERE hidden=0 "
            "ORDER BY COALESCE(taken_at, mtime) DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    async def _screenshot(self) -> tuple[bytes, float]:
        """One capture, however many callers are asking for it.

        There is a single capture slot in the render loop, so two requests
        arriving together used to overwrite each other's future: the first
        waited out its fifteen seconds and got a 503, the second got the
        picture.  Here every caller in flight awaits the *same* task, and a
        caller arriving within :data:`SCREENSHOT_MIN_INTERVAL` of the last one
        is handed that result rather than costing the renderer another
        framebuffer read-back and another PNG encode.

        Returns the PNG and how old it is, in seconds.
        """
        now = time.monotonic()
        if self._shot_data is not None and now - self._shot_at < SCREENSHOT_MIN_INTERVAL:
            return self._shot_data, now - self._shot_at

        task = self._shot_task
        if task is None or task.done():
            task = asyncio.ensure_future(self.app.screenshot())
            self._shot_task = task
        # Shielded, so a browser that closes the tab mid-capture cancels its own
        # request and not the capture everybody else is waiting for.
        data = await asyncio.shield(task)
        self._shot_data = data
        self._shot_at = time.monotonic()
        return data, 0.0

    # ------------------------------------------------------------------
    def _folder_counts(self) -> list[tuple[str, int]]:
        """The folder dropdown's options: short names, with a count each."""
        counts: dict[str, int] = {}
        wanted = set(self.app.folder_choices())
        for path, count in self.app.library.folders():
            for name in wanted:
                if path == name or path.endswith("/" + name):
                    counts[name] = counts.get(name, 0) + count
        return sorted(counts.items())

    def _auth_dependency(self):
        if not self.config.auth_user:
            return None
        scheme = HTTPBasic()
        user = self.config.auth_user
        password = self.config.auth_password

        def check(credentials: HTTPBasicCredentials = Depends(scheme)):
            # Encoded first: compare_digest raises TypeError on a str holding
            # anything outside ASCII, so a user name with an umlaut in it used
            # to come back as a 500 rather than as "wrong password".
            ok_user = secrets.compare_digest(
                credentials.username.encode("utf-8"), user.encode("utf-8"))
            ok_pass = secrets.compare_digest(
                credentials.password.encode("utf-8"), password.encode("utf-8"))
            if not (ok_user and ok_pass):
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, "invalid credentials",
                    headers={"WWW-Authenticate": "Basic"},
                )
            return credentials.username

        return check

    async def run(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            self.api,
            host=self.config.host,
            port=self.config.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
            # Past this many requests in flight uvicorn answers 503 instead of
            # accepting more work.  The frame has one job that must not miss a
            # deadline, and being unable to say "busy" is how an open interface
            # turns a burst of requests into a stutter on the wall.
            limit_concurrency=LIMIT_CONCURRENCY,
        )
        server = uvicorn.Server(config)
        _log.info("web interface on http://%s:%d/", self.config.host, self.config.port)
        if self.config.host in ("0.0.0.0", "::") and not self.config.auth_user:
            # Deliberate, and the right default for a picture frame -- but it
            # should be a thing the owner knows rather than a thing he finds
            # out.  Anyone on the network can change every setting the page can.
            _log.warning(
                "the web interface answers on every network with no password: "
                "anyone who can reach %s:%d can change any setting. Set "
                "http.auth_user and http.auth_password to require a login, or "
                "http.host to 127.0.0.1 to keep it on the frame itself",
                self.config.host, self.config.port,
            )
        try:
            await server.serve()
        except asyncio.CancelledError:
            server.should_exit = True
            raise


async def _in_thread(func, *args):
    """Run something blocking off the event loop the renderer shares."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args))


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def _summary(record) -> dict:
    return {
        "id": record.id,
        "path": record.path,
        "basename": record.basename,
        "mtime": record.mtime,
        "folder": record.folder,
        "is_video": bool(record.is_video),
        "is_portrait": bool(record.is_portrait),
        "taken_at": record.taken_at,
        "title": record.title,
        "caption": record.caption,
        "location": record.location,
        "tags": record.tags,
        "rating": record.rating,
    }


def _too_many_pixels(path: str, is_video: bool = False) -> str | None:
    """Why this file will not be decoded here, or ``None`` if it will.

    Reads the header only -- Pillow does not touch the pixels until something
    asks for them -- so the cost of saying no is a few hundred bytes off the
    disk, against the 1.5 GB a 500-megapixel PNG would otherwise become.  A
    video is left to the poster-frame code, which decodes one frame at the
    size it was asked for.
    """
    if is_video:
        return None
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
    except Exception:
        # Not an image, or one Pillow cannot even open: not this check's
        # problem, and the renderer below says so more usefully.
        return None
    if width * height > MAX_DECODE_PIXELS:
        return (f"{os.path.basename(path)} is {width}×{height} pixels, past the "
                f"{MAX_DECODE_PIXELS // 1_000_000} megapixel limit this frame "
                "will decode over the network. The original file is still "
                "served unchanged.")
    return None


def _thumbnail(path: str, is_video: bool) -> bytes | None:
    from ..media.preview import render_preview

    return render_preview(path, is_video, THUMB_SIZE)


async def _thumbnail_response(path: str, is_video: bool, versioned: bool = False):
    """One thumbnail, refused rather than decoded when it is absurdly large.

    ``versioned`` says the caller put the file's mtime in the query string, so
    the URL changes whenever the picture behind it does and the answer can be
    kept for a day.  Without it the URL is only ``/photo/<id>/thumb`` -- and an
    id is a SQLite rowid that is handed to a different file after a rescan, so
    a day-old copy would be shown beside the caption of a different picture.
    """
    too_big = await _in_thread(_too_many_pixels, path, is_video)
    if too_big:
        raise HTTPException(413, too_big)
    data = await _in_thread(_thumbnail, path, is_video)
    if data is None:
        raise HTTPException(415, "cannot render a thumbnail for this file")
    cache = ("public, max-age=86400, immutable" if versioned
             else "no-cache, max-age=0, must-revalidate")
    return Response(data, media_type="image/jpeg",
                    headers={"Cache-Control": cache})


def _removal(entry: dict, folder: str, hold: dict | None = None) -> dict:
    """One journal line, shaped for the page that draws it.

    ``hold`` is the row from the held-out list, when there is one.  It is what
    lets the page say the difference between "removed, and that was that" and
    "removed, and something keeps putting it back".
    """
    stored_as = entry.get("stored_as") or ""
    return {
        "stored_as": stored_as,
        "held": hold is not None,
        "came_back": bool(hold and (hold.get("seen_count") or 0)),
        "seen_count": (hold or {}).get("seen_count") or 0,
        "seen_path": (hold or {}).get("seen_path") or "",
        "seen_at": (hold or {}).get("seen_at"),
        "basename": entry.get("basename") or stored_as,
        "folder": entry.get("folder") or "",
        "original_path": entry.get("original_path") or "",
        "removed_at": entry.get("removed_at"),
        "removed_iso": entry.get("removed_iso") or "",
        "source": entry.get("source") or "",
        "taken_at": entry.get("taken_at"),
        "taken_iso": entry.get("taken_iso") or "",
        "title": entry.get("title") or "",
        "caption": entry.get("caption") or "",
        "location": entry.get("location") or "",
        "tags": entry.get("tags") or [],
        "size": entry.get("size"),
        "is_video": bool(entry.get("is_video")),
        "play_count": entry.get("play_count"),
        "restored_at": entry.get("restored_at"),
        "restored_iso": entry.get("restored_iso") or "",
        "restored_to": entry.get("restored_to") or "",
        "purged_at": entry.get("purged_at"),
        "purged_iso": entry.get("purged_iso") or "",
        "on_disk": os.path.exists(os.path.join(folder, stored_as)) if stored_as else False,
    }


def _removed_file(app, stored_as: str):
    """Resolve a journal id to a file, refusing anything outside the folder.

    ``stored_as`` arrives from the URL, so ``../../etc/passwd`` has to bounce
    off something.  basename() plus a journal lookup is that something: only
    names the frame itself wrote can be served.
    """
    log = getattr(app, "removals", None)
    if log is None:
        return None, ""
    name = os.path.basename(str(stored_as or ""))
    if not name or name != stored_as:
        return None, ""
    entry = next(
        (e for e in reversed(log.entries()) if e.get("stored_as") == name), None
    )
    if entry is None:
        return None, ""
    return entry, os.path.join(log.folder, name)
