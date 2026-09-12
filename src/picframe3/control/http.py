"""Web interface: a small REST API, a live event stream, and a browser UI.

Everything the frame can do is reachable here, which makes the web UI, the
phone on the sofa and Home Assistant all first-class clients of the same
surface.  Server-sent events push state changes instead of the browser polling,
so an open tab costs nothing while the frame is idle.
"""

from __future__ import annotations

import asyncio
import io
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

        @api.get("/api/config/schema", dependencies=guard)
        async def config_schema():
            """Every setting, with enough about each one to draw a control.

            Generated from the dataclasses, so the settings page cannot fall
            behind the code: a field added to the config appears here, and on
            the page, without anyone remembering to list it.
            """
            from ..uischema import schema

            return schema(self.app.config)

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
                self.app.config.save()
            return {"ok": True, "applied": applied, "saved": persist}

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

        @api.get("/api/library/photos", dependencies=guard)
        async def photos(q: str = "", limit: int = Query(60, le=500), offset: int = 0):
            if q:
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
    try:
        from PIL import Image, ImageOps

        if is_video:
            from ..media.video import poster_frame

            image = poster_frame(path, THUMB_SIZE)
            if image is None:
                return None
        else:
            image = Image.open(path)
            image.draft("RGB", THUMB_SIZE)        # JPEG DCT scaling: much faster
            image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        image.thumbnail(THUMB_SIZE, Image.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=82, optimize=True)
        return buf.getvalue()
    except Exception as exc:
        _log.debug("thumbnail failed for %s: %s", path, exc)
        return None
