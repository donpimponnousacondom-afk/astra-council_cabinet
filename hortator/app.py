from __future__ import annotations

import asyncio
import fcntl
import hmac
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .concurrency import BackgroundTasks
from . import __version__
from .discord_gateway import COMMANDS, DiscordManager
from .models import ControlError, OWNER_ID, SCHEMAS
from .plugins import Registry
from .provider import ProviderError, ProviderPool
from .publishing import PublishingWorker
from .runtime import Engine
from .security import Actor, Auth, Vault
from .service import Service
from .store import Store
from .snapshot_runtime import SnapshotGate, SnapshotRuntime


class Kernel:
    def __init__(self, directory):
        self.started = False
        self.closed = False
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
        from .discord_dispatch import DiscordDispatch

        self.registry.discord_dispatch = DiscordDispatch(self.service)
        self.publishing = PublishingWorker(self.store, self.vault, self.directory, self.registry.documents)
        self.background = BackgroundTasks(self.store)
        self.service.background = self.background
        for component in (self.connector, self.engine, self.publishing):
            component.background = self.background

    def start(self):
        if self.background.group is None or self.background.closing or self.closed:
            raise RuntimeError("Open Kernel.lifetime() before starting runtime services")
        self.started = True
        self.store.recover()
        self.connector.start()
        self.engine.start()
        self.publishing.start()
        self.background.spawn(self.watch_loop(), name="event-loop-monitor")
        self.store.emit(
            "runtime.started", {"version": __version__, "build": self.service.version(), "pid": os.getpid()}
        )

    @asynccontextmanager
    async def lifetime(self):
        # The group drains before database/client resources are closed, even
        # when startup, a caller, or cleanup raises.
        try:
            async with self.background.lifetime():
                try:
                    yield self
                finally:
                    await self.stop()
        finally:
            await self.close_resources()

    async def watch_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            expected = loop.time() + 1
            await asyncio.sleep(1)
            delay = loop.time() - expected
            if delay > 1:
                self.store.emit(
                    "runtime.loop_delayed",
                    {
                        "delay_ms": round(delay * 1000),
                        "active_bots": sorted(self.engine.tasks),
                        "note": "Event loop was delayed; active bots are correlation, not attribution",
                    },
                    level="warning",
                )

    async def stop(self):
        if self.closed:
            return
        errors = []
        for component in (self.publishing, self.engine, self.registry.agentic, self.connector):
            try:
                await component.close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("Runtime shutdown failures", errors)

    async def close_resources(self):
        if self.closed:
            return
        self.closed = True
        try:
            await self.pool.close()
            if self.started:
                self.store.emit("runtime.stopped")
        finally:
            self.store.close()

    async def close(self):
        from .concurrency import cancel_and_wait

        try:
            await self.stop()
        finally:
            try:
                await cancel_and_wait(*self.background.pending)
            finally:
                await self.close_resources()


class LoginBody(BaseModel):
    password: str = Field(max_length=1024)


class CredentialBody(BaseModel):
    value: str = Field(max_length=16000)


class SnapshotBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=2000)


class SnapshotRestoreBody(BaseModel):
    confirmation: str
    scope: str = Field(default="bot", pattern="^(full|bot)$")
    bot_id: str | None = Field(default=None, max_length=80)
    channel_id: str | None = Field(default=None, max_length=100)
    include_context: bool = False
    include_global_memory: bool = False


class ControlBody(BaseModel):
    action: str
    kind: str | None = None
    id: str | None = None
    data: dict = Field(default_factory=dict)


def create_app(directory=None, start_runtime=True, *, stopping=None, console=None):
    directory = Path(directory or os.getenv("HORTATOR_DATA_DIR", "data")).resolve()
    stopping = stopping if stopping is not None else asyncio.Event()
    gate = SnapshotGate()

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
        controller = SnapshotRuntime(app, directory, Kernel, start_runtime=start_runtime, console=console)
        app.state.snapshots = controller
        try:
            async with asyncio.TaskGroup() as group:
                task = group.create_task(controller.run(), name="snapshot-runtime-owner")
                await controller.ready.wait()
                try:
                    yield
                finally:
                    task.cancel()
        finally:
            if console:
                console.stop_keys()
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
        current = request.app.state.kernel
        if current is None or current.closed:
            raise ControlError("Runtime is restarting for snapshot maintenance", 503)
        return current

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
        snapshot_mutation = request.method == "POST" and request.url.path.startswith("/api/snapshots")
        try:
            async with gate.enter(exclusive=snapshot_mutation):
                controller = getattr(request.app.state, "snapshots", None)
                if controller and controller.busy:
                    return JSONResponse(
                        {"error": "Snapshot maintenance is still in progress; retry after it completes"},
                        status_code=503,
                    )
                if (
                    controller
                    and controller.paused
                    and request.method not in ("GET", "HEAD", "OPTIONS")
                    and not request.url.path.startswith(("/api/auth/", "/api/snapshots"))
                ):
                    return JSONResponse(
                        {
                            "error": "Runtime is paused after snapshot restoration. Inspect the restored state, then use Snapshots → Resume runtime before changing it."
                        },
                        status_code=409,
                    )
                response = await call_next(request)
        except ControlError as error:
            response = JSONResponse({"error": str(error)}, status_code=error.status)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/sites/"):
            # Published documents are a separate opaque sandbox even though files use
            # this loopback server. Never inherit dashboard script/API capabilities.
            parts = request.url.path.split("/")
            source = ""
            if len(parts) >= 4 and all(re.fullmatch(r"[A-Za-z0-9_-]+", item) for item in parts[2:4]):
                source = quote(str(request.base_url), safe=":/[]") + "sites/" + "/".join(parts[2:4]) + "/"
            restricted = source or "'none'"
            response.headers["Content-Security-Policy"] = (
                "sandbox allow-scripts; default-src 'none'; "
                f"script-src 'unsafe-inline' {restricted}; style-src 'unsafe-inline' {restricted}; "
                f"img-src data: {restricted}; font-src {restricted}; media-src {restricted}; "
                f"connect-src {restricted}; object-src 'none'; frame-src 'none'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            )
            response.headers["Cache-Control"] = "no-store"
            response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
            response.headers["Permissions-Policy"] = (
                "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
            )
            if 200 <= response.status_code < 300:
                # Site resources are deliberately public. Opaque-origin sandbox JS
                # may read only these resources, without credentials.
                response.headers["Access-Control-Allow-Origin"] = "*"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ControlError)
    async def control_error(request, exc):
        k = getattr(request.app.state, "kernel", None)
        body = {"error": str(exc)}
        if isinstance(exc, ProviderError):
            body.update(
                source="provider", api_status=exc.status, upstream_status=exc.http_status, details=exc.details
            )
        return JSONResponse(k.vault.redact(body) if k else body, status_code=exc.status)

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

    @app.get("/api/version")
    async def version(actor=Depends(authenticated), k=Depends(kernel)):
        return k.service.version()

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
            max_age=432000000,
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
        return k.service.status() | {
            "maintenance_pause": app.state.snapshots.paused,
            "snapshot_operation": app.state.snapshots.operation,
        }

    @app.get("/api/snapshots")
    async def snapshots(actor=Depends(authenticated)):
        return await app.state.snapshots.catalog()

    @app.post("/api/snapshots")
    async def capture_snapshot(body: SnapshotBody, actor=Depends(authenticated)):
        return await app.state.snapshots.request("capture", body.model_dump())

    @app.post("/api/snapshots/resume")
    async def resume_snapshot(actor=Depends(authenticated)):
        if not app.state.snapshots.paused:
            return {"paused": False}
        return await app.state.snapshots.request("resume", {})

    @app.post("/api/snapshots/{identifier}/restore")
    async def restore_snapshot(identifier: str, body: SnapshotRestoreBody, actor=Depends(authenticated)):
        if body.confirmation != identifier:
            raise ControlError("Type the complete snapshot identifier to confirm restoration", 409)
        return await app.state.snapshots.request(
            "restore", {"identifier": identifier, **body.model_dump(exclude={"confirmation"})}
        )

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
        return k.service.turn(turn_id, include_diagnostics=True)

    @app.get("/api/trajectory/{turn_id}/export")
    async def trajectory_export(turn_id: str, actor=Depends(authenticated), k=Depends(kernel)):
        return JSONResponse(
            k.service.turn(turn_id, include_diagnostics=True),
            headers={"Content-Disposition": f'attachment; filename="{turn_id}.json"'},
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
            while not stopping.is_set() and not await request.is_disconnected():
                if gate.maintenance or k.closed:
                    return
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
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=1)
                except TimeoutError:
                    pass

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

    @app.get("/api/global-memory/{bot_id}")
    async def global_memory(bot_id: str, actor=Depends(authenticated), k=Depends(kernel)):
        actor.require_owner()
        return k.service.global_memory_view(bot_id)

    @app.post("/api/global-memory/{bot_id}")
    async def change_global_memory(bot_id: str, body: dict, actor=Depends(authenticated), k=Depends(kernel)):
        return await k.service.global_memory_change(actor, bot_id, body)

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

    @app.get("/api/documents")
    async def documents(actor=Depends(authenticated), k=Depends(kernel)):
        state = k.publishing.status()
        return {
            "sites": k.registry.documents.list_sites(),
            "remote_status": state["status"],
            "publishing": state,
        }

    @app.get("/api/agentic-tools")
    async def agentic_tools(
        bot_id: str | None = Query(None, max_length=80),
        limit: int = Query(30, ge=1, le=100),
        actor=Depends(authenticated),
        k=Depends(kernel),
    ):
        return await k.registry.agentic.catalog(bot_id=bot_id, limit=limit)

    @app.get("/api/agentic-tools/inspect")
    async def agentic_inspect(
        resource: str = Query(pattern="^(files|file|document|job|output)$"),
        bot_id: str = Query(max_length=80),
        channel_id: str = Query(max_length=100),
        task: str | None = Query(None, max_length=64),
        path: str = Query(".", max_length=240),
        id: str | None = Query(None, max_length=100),
        offset: int = Query(0, ge=0),
        length: int = Query(8000, ge=1, le=18000),
        stream: str = Query("stdout", pattern="^(stdout|stderr)$"),
        actor=Depends(authenticated),
        k=Depends(kernel),
    ):
        return k.vault.redact(
            k.registry.agentic.inspect(
                resource,
                bot_id,
                channel_id,
                task=task,
                path=path,
                identifier=id,
                offset=offset,
                length=length,
                stream=stream,
            )
        )

    @app.get("/api/documents/{bot_id}/{slug}/files/{filename:path}")
    async def document_draft(
        bot_id: str, slug: str, filename: str, actor=Depends(authenticated), k=Depends(kernel)
    ):
        data, mime = k.registry.documents.read_file(bot_id, slug, filename)
        return Response(
            data,
            media_type=mime,
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(Path(filename).name, safe=""),
            },
        )

    @app.api_route("/sites/{bot_id}/{slug}/", methods=["GET", "HEAD"])
    @app.api_route("/sites/{bot_id}/{slug}/{filename:path}", methods=["GET", "HEAD"])
    async def published_document(
        request: Request, bot_id: str, slug: str, filename: str = "index.html", k=Depends(kernel)
    ):
        data, mime = k.registry.documents.resolve_published(bot_id, slug, filename)
        return Response(
            data if request.method == "GET" else b"",
            media_type=mime,
            headers={"Content-Length": str(len(data))},
        )

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
