from __future__ import annotations

import asyncio
import fcntl
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .discord_gateway import COMMANDS, DiscordManager
from .models import ControlError, OWNER_ID, SCHEMAS
from .plugins import Registry
from .provider import ProviderPool
from .runtime import Engine
from .security import Actor, Auth, Vault
from .service import Service
from .store import Store


class Kernel:
    def __init__(self, directory):
        self.started = False
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.directory / "council.sqlite3")
        self.vault = Vault(self.store, self.directory)
        self.auth = Auth(self.store, self.vault, self.directory)
        self.pool = ProviderPool(self.store, self.vault)
        self.service = Service(self.store, self.vault, self.pool)
        self.service.seed()
        self.registry = Registry(self.store, self.vault, self.directory, self.service.inspect)
        self.service.registry = self.registry
        self.service.seed_plugins()
        self.engine = Engine(self.store, self.vault, self.pool, self.registry)
        self.service.engine = self.engine
        self.connector = DiscordManager(self.service)
        self.service.connector = self.connector
        self.engine.transport = self.connector

    def start(self):
        self.started = True
        self.store.recover()
        self.connector.start()
        self.engine.start()
        self.store.emit("runtime.started", {"version": __version__, "pid": os.getpid()})

    async def close(self):
        await self.engine.close()
        await self.connector.close()
        await self.pool.close()
        if self.started:
            self.store.emit("runtime.stopped")
        self.store.close()


class LoginBody(BaseModel):
    password: str = Field(max_length=1024)


class CredentialBody(BaseModel):
    value: str = Field(max_length=16000)


class ControlBody(BaseModel):
    action: str
    kind: str | None = None
    id: str | None = None
    data: dict = Field(default_factory=dict)


def create_app(directory=None, start_runtime=True):
    directory = Path(directory or os.getenv("HORTATOR_DATA_DIR", "data")).resolve()

    @asynccontextmanager
    async def lifespan(app):
        directory.mkdir(parents=True, exist_ok=True)
        lock = (directory / "runtime.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.close()
            raise RuntimeError(
                "Another Hortator process owns this database. Run exactly one worker."
            ) from exc
        kernel = None
        try:
            kernel = Kernel(directory)
            app.state.kernel = kernel
            if start_runtime:
                kernel.start()
            yield
        finally:
            if kernel:
                await kernel.close()
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    app = FastAPI(
        title="Hortator Council",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    async def kernel(request: Request):
        return request.app.state.kernel

    def check_origin(request):
        origin = request.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            allowed = {v.rstrip("/") for v in os.getenv("HORTATOR_ALLOWED_ORIGINS", "").split(",") if v}
            if (
                parsed.scheme not in ("http", "https")
                or parsed.netloc != request.headers.get("host")
                and origin.rstrip("/") not in allowed
            ):
                raise ControlError("Cross-origin control requests are forbidden", 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise ControlError("Cross-site control requests are forbidden", 403)

    async def authenticated(request: Request, k=Depends(kernel)):
        session = k.auth.session(request.cookies.get("hortator_session"))
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            check_origin(request)
            if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
                raise ControlError("CSRF token is missing or invalid", 403)
        return Actor("dashboard", OWNER_ID)

    @app.middleware("http")
    async def boundaries(request, call_next):
        if (
            request.headers.get("content-length", "0").isdigit()
            and int(request.headers.get("content-length", "0")) > 1_000_000
        ):
            return JSONResponse({"error": "Request body exceeds 1 MB"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ControlError)
    async def control_error(request, exc):
        k = getattr(request.app.state, "kernel", None)
        return JSONResponse({"error": k.vault.redact(str(exc)) if k else str(exc)}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            {
                "error": "; ".join(
                    ".".join(map(str, item["loc"])) + ": " + item["msg"] for item in exc.errors()
                )
            },
            status_code=422,
        )

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "version": __version__}

    @app.post("/api/auth/login")
    async def login(body: LoginBody, request: Request, k=Depends(kernel)):
        check_origin(request)
        # Rate-limited scrypt verification is bounded; auth state stays on the event-loop writer.
        token, csrf = k.auth.login(body.password, request.client.host if request.client else "unknown")
        response = JSONResponse({"csrf": csrf, "owner_id": OWNER_ID})
        response.set_cookie(
            "hortator_session",
            token,
            httponly=True,
            samesite="strict",
            secure=os.getenv("HORTATOR_SECURE_COOKIES") == "1",
            max_age=43200,
            path="/",
        )
        return response

    @app.get("/api/auth/session")
    async def session(request: Request, actor=Depends(authenticated), k=Depends(kernel)):
        data = k.auth.session(request.cookies.get("hortator_session"))
        return {"csrf": data["csrf"], "owner_id": OWNER_ID}

    @app.post("/api/auth/logout")
    async def logout(request: Request, actor=Depends(authenticated), k=Depends(kernel)):
        k.auth.logout(request.cookies["hortator_session"])
        response = JSONResponse({"signed_out": True})
        response.delete_cookie("hortator_session")
        return response

    @app.get("/api/status")
    async def status(actor=Depends(authenticated), k=Depends(kernel)):
        return k.service.status()

    @app.get("/api/stats")
    async def stats(hours: int = Query(24, ge=1, le=8760), actor=Depends(authenticated), k=Depends(kernel)):
        return k.service.stats(hours)

    @app.get("/api/config-schemas")
    async def config_schemas(actor=Depends(authenticated)):
        return {name: value.model_json_schema() for name, value in SCHEMAS.items()}

    @app.get("/api/config/{kind}")
    async def entities(kind: str, actor=Depends(authenticated), k=Depends(kernel)):
        return k.service.inspect(kind)

    @app.get("/api/config/{kind}/{entity_id}")
    async def entity(kind: str, entity_id: str, actor=Depends(authenticated), k=Depends(kernel)):
        return k.service.inspect(kind, entity_id)

    @app.post("/api/control")
    async def control(body: ControlBody, actor=Depends(authenticated), k=Depends(kernel)):
        return await k.service.control(actor, body.model_dump())

    @app.put("/api/credentials/{kind}/{entity_id}/{field}")
    async def credential(
        kind: str,
        entity_id: str,
        field: str,
        body: CredentialBody,
        actor=Depends(authenticated),
        k=Depends(kernel),
    ):
        return await k.service.credential(actor, kind, entity_id, field, body.value)

    @app.get("/api/trajectory")
    async def trajectories(
        bot_id: str | None = None,
        before: float | None = None,
        status: str | None = None,
        limit: int = Query(60, ge=1, le=200),
        actor=Depends(authenticated),
        k=Depends(kernel),
    ):
        return k.service.turns(bot_id, before, status, limit)

    @app.get("/api/trajectory/{turn_id}")
    async def trajectory(turn_id: str, actor=Depends(authenticated), k=Depends(kernel)):
        return k.service.turn(turn_id)

    @app.get("/api/trajectory/{turn_id}/export")
    async def trajectory_export(turn_id: str, actor=Depends(authenticated), k=Depends(kernel)):
        return JSONResponse(
            k.service.turn(turn_id), headers={"Content-Disposition": f'attachment; filename="{turn_id}.json"'}
        )

    @app.get("/api/events/stream")
    async def event_stream(request: Request, after: int = 0, actor=Depends(authenticated), k=Depends(kernel)):
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise ControlError("Invalid event cursor") from None

        async def generate():
            nonlocal cursor
            if not cursor:
                cursor = k.store.one("SELECT coalesce(max(seq),0) AS seq FROM events")["seq"]
            yield f"event: connected\ndata: {json.dumps({'cursor': cursor})}\n\n"
            while not await request.is_disconnected():
                try:
                    k.auth.session(request.cookies.get("hortator_session"))
                except ControlError:
                    return
                rows = k.store.events(after=cursor, limit=200)
                for event in rows:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(event)}\n\n"
                if not rows:
                    yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.get("/api/events")
    async def events(
        after: int = Query(0, ge=0),
        before: int | None = None,
        bot_id: str | None = None,
        turn_id: str | None = None,
        level: str | None = None,
        limit: int = Query(100, ge=1, le=500),
        actor=Depends(authenticated),
        k=Depends(kernel),
    ):
        return k.store.events(
            after=after, before=before, bot_id=bot_id, turn_id=turn_id, level=level, limit=limit
        )

    @app.get("/api/context/{bot_id}/{channel_id}")
    async def context(bot_id: str, channel_id: str, actor=Depends(authenticated), k=Depends(kernel)):
        return k.service.context(bot_id, channel_id)

    @app.get("/api/messages/{channel_id}")
    async def messages(
        channel_id: str,
        after: int = 0,
        through: int | None = None,
        limit: int = Query(100, ge=1, le=1000),
        actor=Depends(authenticated),
        k=Depends(kernel),
    ):
        return k.store.transcript(channel_id, after=after, through=through, limit=limit)

    @app.get("/api/artifacts/{artifact_id}")
    async def artifact(artifact_id: str, actor=Depends(authenticated), k=Depends(kernel)):
        value = k.store.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        if not value:
            raise ControlError("Artifact not found", 404)
        path = k.registry.directory / value["filename"]
        if not path.is_file():
            raise ControlError("Artifact file is missing from storage", 404)
        return FileResponse(path, media_type=value["mime"], filename=value["filename"])

    @app.get("/api/export/config")
    async def export_config(actor=Depends(authenticated), k=Depends(kernel)):
        return JSONResponse(
            {kind: k.store.list(kind) for kind in SCHEMAS},
            headers={"Content-Disposition": 'attachment; filename="council-config.json"'},
        )

    @app.get("/api/commands")
    async def commands(actor=Depends(authenticated)):
        return [{"command": cmd, "description": desc} for cmd, desc in COMMANDS]

    @app.get("/api/openapi.json")
    async def openapi(actor=Depends(authenticated)):
        return app.openapi()

    assets = Path(os.getenv("HORTATOR_WEB_DIR", str(Path(__file__).resolve().parent.parent / "web" / "dist")))
    if assets.is_dir():
        app.mount("/", StaticFiles(directory=assets, html=True), name="dashboard")
    else:

        @app.get("/")
        async def development():
            return {
                "message": "Dashboard assets are not built. Run npm ci && npm run build in web/, or use the Vite development server on port 5173."
            }

    return app


app = create_app()
