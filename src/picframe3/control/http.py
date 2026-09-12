"""Web interface: a small REST API, a live event stream, and a browser UI.

Everything the frame can do is reachable here, which makes the web UI, the
phone on the sofa and Home Assistant all first-class clients of the same
surface.  Server-sent events push state changes instead of the browser polling,
so an open tab costs nothing while the frame is idle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from typing import Any

from ..config import HttpConfig
from ..events import Action, Command

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

    HAVE_FASTAPI = True
except ImportError:  # pragma: no cover - optional dependency
    HAVE_FASTAPI = False

WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
THUMB_SIZE = (480, 480)


class HttpServer:
    def __init__(self, app, config: HttpConfig):
        self.app = app
        self.config = config
        if not HAVE_FASTAPI:
            raise RuntimeError(
                "the web interface needs fastapi and uvicorn "
                "(pip install 'picframe3[web]')"
            )
        try:
            import uvicorn  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "the web interface needs uvicorn (pip install 'picframe3[web]')"
            ) from exc
        self.api = self._build()

    # ------------------------------------------------------------------
    def _build(self):
        api = FastAPI(
            title="picframe3",
            version=getattr(self.app, "version", "3"),
            docs_url="/api/docs",
            openapi_url="/api/openapi.json",
        )
        if self.config.cors_origins:
            from fastapi.middleware.cors import CORSMiddleware

            api.add_middleware(
                CORSMiddleware,
                allow_origins=self.config.cors_origins,
                allow_methods=["*"],
                allow_headers=["*"],
            )

        auth = self._auth_dependency()
        guard = [Depends(auth)] if auth else []

        # -- state -----------------------------------------------------
        @api.get("/api/state", dependencies=guard)
        async def get_state():
            return self.app.state().as_dict()

        @api.get("/api/events", dependencies=guard)
        async def events(request: Request):
            queue: asyncio.Queue = asyncio.Queue(maxsize=8)

            def listener(state):
                try:
                    queue.put_nowait(state.as_dict())
                except asyncio.QueueFull:
                    pass

            unsubscribe = self.app.bus.subscribe(listener)

            async def stream():
                try:
                    yield _sse(self.app.state().as_dict())
                    while True:
                        if await request.is_disconnected():
                            break
                        try:
                            payload = await asyncio.wait_for(queue.get(), timeout=20.0)
                            yield _sse(payload)
                        except TimeoutError:
                            yield ": keep-alive\n\n"
                finally:
                    unsubscribe()

            return StreamingResponse(stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no"})

        # -- which pictures are in the running -------------------------
        # Declared before the /api/{action} shortcut below, which would
        # otherwise swallow POST /api/filters and turn it into an unknown
        # action.  FastAPI matches routes in the order they are added.
        @api.get("/api/filters", dependencies=guard)
        async def get_filters():
            """The filter now in force, and everything a filter panel offers."""
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
            apply a filter to find out it matches nothing.
            """
            trial = self.app.playlist.filters.merged(dict(body or {}))
            return {"matching": self.app.playlist.count_for(trial),
                    "filters": trial.as_dict()}

        # -- commands --------------------------------------------------
        @api.post("/api/command", dependencies=guard)
        async def post_command(body: dict):
            command = Command.parse(body, source="http")
            if command is None:
                raise HTTPException(400, f"unknown action {body.get('action')!r}")
            self.app.bus.submit(command)
            return {"ok": True, "action": command.action.value}

        @api.post("/api/{action}", dependencies=guard)
        async def shortcut(action: str, body: dict | None = None):
            payload = dict(body or {})
            payload["action"] = action
            command = Command.parse(payload, source="http")
            if command is None:
                raise HTTPException(404, f"no such action {action!r}")
            self.app.bus.submit(command)
            return {"ok": True, "action": command.action.value}

        # -- configuration ---------------------------------------------
        @api.get("/api/config", dependencies=guard)
        async def get_config():
            from ..uischema import redact

            return redact(self.app.config.as_dict())

        @api.post("/api/geo/preview", dependencies=guard)
        async def geo_preview(body: dict):
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

            detail = str(body.get("detail") or "full")
            tiers = body.get("key_order") or self.app.config.geo.key_order
            order = key_order_for(detail, tiers)
            suppress = body.get("suppress")
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
            folders = [{"name": path, "label": f"{path}  ({count})"}
                       for path, count in self.app.library.folders()]
            return schema(self.app.config, extra_options={"folders": folders})

        @api.patch("/api/config", dependencies=guard)
        async def patch_config(body: dict, persist: bool = Query(False)):
            applied: dict[str, Any] = {}
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

        # -- library ---------------------------------------------------
        @api.get("/api/library", dependencies=guard)
        async def library_stats():
            return self.app.library.stats()

        @api.get("/api/library/folders", dependencies=guard)
        async def folders():
            return [{"path": p, "count": n} for p, n in self.app.library.folders()]

        @api.get("/api/library/tags", dependencies=guard)
        async def tags():
            return [{"name": t, "count": n} for t, n in self.app.library.all_tags()]

        @api.get("/api/library/locations", dependencies=guard)
        async def locations():
            return [{"name": p, "count": n} for p, n in self.app.library.locations()]

        @api.get("/api/library/photos", dependencies=guard)
        async def photos(q: str = "", limit: int = Query(60, le=500), offset: int = 0,
                         selected: bool = False):
            if selected:
                # Exactly what the slideshow is drawing from, in the same
                # order the grid shows everything else: newest first.  A
                # search inside the selection reads further down it before
                # trimming, so searching does not silently look at one page.
                reach = min(limit * 10, 2000) if q else limit
                ids = self.app.playlist.selection(limit=reach, offset=offset)
                records = [r for r in (self.app.library.get(i) for i in ids)
                           if r is not None]
                if q:
                    needle = q.casefold()
                    records = [r for r in records if needle in " ".join(
                        str(x) for x in (r.basename, r.title, r.caption, r.location,
                                         *(r.tags or ()))).casefold()][:limit]
            elif q:
                records = self.app.library.search(q, limit=limit)
            else:
                records = self.app.library.query(
                    "SELECT * FROM files WHERE hidden=0 "
                    "ORDER BY COALESCE(taken_at, mtime) DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            return [_summary(r) for r in records]

        @api.get("/api/library/photo/{file_id}", dependencies=guard)
        async def photo(file_id: int):
            record = self.app.library.get(file_id)
            if record is None:
                raise HTTPException(404, "not found")
            return record.as_dict()

        @api.get("/api/library/photo/{file_id}/thumb", dependencies=guard)
        async def thumb(file_id: int):
            record = self.app.library.get(file_id)
            if record is None:
                raise HTTPException(404, "not found")
            data = await asyncio.get_running_loop().run_in_executor(
                None, _thumbnail, record.path, record.is_video
            )
            if data is None:
                raise HTTPException(415, "cannot render a thumbnail for this file")
            return Response(data, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})

        @api.get("/api/library/photo/{file_id}/file", dependencies=guard)
        async def original(file_id: int):
            record = self.app.library.get(file_id)
            if record is None or not os.path.exists(record.path):
                raise HTTPException(404, "not found")
            return FileResponse(record.path)

        # -- removed pictures ------------------------------------------
        # The journal, not the folder listing: the folder only knows there is
        # a file called IMG_4312.jpg, while the journal knows it was taken in
        # Lisbon in 2019, removed on Tuesday from the web UI, and which folder
        # it belongs back in.
        @api.get("/api/removed", dependencies=guard)
        async def removed(include_restored: bool = Query(False),
                          limit: int = Query(200, le=2000)):
            log = self.app.removals
            if log is None:
                return []
            entries = log.entries(include_restored=include_restored, newest_first=True)
            return [_removal(e, log.folder) for e in entries[:limit]]

        @api.get("/api/removed/summary", dependencies=guard)
        async def removed_summary():
            log = self.app.removals
            return log.summary() if log is not None else {}

        @api.get("/api/removed/{stored_as}/thumb", dependencies=guard)
        async def removed_thumb(stored_as: str):
            entry, path = _removed_file(self.app, stored_as)
            if entry is None or not os.path.exists(path):
                raise HTTPException(404, "not found")
            data = await asyncio.get_running_loop().run_in_executor(
                None, _thumbnail, path, bool(entry.get("is_video"))
            )
            if data is None:
                raise HTTPException(415, "cannot render a thumbnail for this file")
            return Response(data, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})

        @api.get("/api/removed/{stored_as}/file", dependencies=guard)
        async def removed_file(stored_as: str):
            entry, path = _removed_file(self.app, stored_as)
            if entry is None or not os.path.exists(path):
                raise HTTPException(404, "not found")
            return FileResponse(path)

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
            data = await asyncio.get_running_loop().run_in_executor(
                None, render_preview, path, bool(current.get("is_video")),
                (size, size), 82,
            )
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
                data = await self.app.screenshot()
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

        @api.get("/api/geo-detail", dependencies=guard)
        async def geo_detail():
            """How much of an address a caption may show."""
            from ..media.geocode import DETAIL_LABELS

            return [{"name": name, "label": label}
                    for name, label in DETAIL_LABELS.items()]

        @api.get("/api/health")
        async def health():
            return {"ok": True, "uptime": round(time.monotonic() - self.app.started, 1)}

        if os.path.isdir(WEB_ROOT):
            api.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")
        else:  # pragma: no cover
            _log.warning("web assets missing at %s", WEB_ROOT)
        return api

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
            ok_user = secrets.compare_digest(credentials.username, user)
            ok_pass = secrets.compare_digest(credentials.password, password)
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
        )
        server = uvicorn.Server(config)
        _log.info("web interface on http://%s:%d/", self.config.host, self.config.port)
        try:
            await server.serve()
        except asyncio.CancelledError:
            server.should_exit = True
            raise


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def _summary(record) -> dict:
    return {
        "id": record.id,
        "path": record.path,
        "basename": record.basename,
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


def _thumbnail(path: str, is_video: bool) -> bytes | None:
    from ..media.preview import render_preview

    return render_preview(path, is_video, THUMB_SIZE)


def _removal(entry: dict, folder: str) -> dict:
    """One journal line, shaped for the page that draws it."""
    stored_as = entry.get("stored_as") or ""
    return {
        "stored_as": stored_as,
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
