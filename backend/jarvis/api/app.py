"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from jarvis.agents.deps import Deps
from jarvis.api.auth import RateLimiter, TokenVerifier
from jarvis.api.routes import campaigns, conversation, ops, records
from jarvis.api.routes import integrations as integrations_routes
from jarvis.config import Settings, get_settings
from jarvis.core import integrations as integrations_core
from jarvis.core.errors import JarvisError
from jarvis.db.pool import Database
from jarvis.integrations.livekit import LiveKitClient
from jarvis.observability.logging import configure_logging

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None, deps: Deps | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.jarvis_log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await app.state.db.connect()
            res = await integrations_core.refresh(app.state, settings, app.state.livekit)
            app.state.credentials_error = res["error"]
            if res["error"]:
                log.error("credentials.load_failed", detail=res["error"])
        except Exception as exc:  # API still starts; /ready reports the outage
            log.warning("db.initial_connect_failed", error=str(exc))
        yield
        await app.state.db.close()

    app = FastAPI(title="JARVIS API", version="0.1.0", lifespan=lifespan)
    db = deps.db if deps else Database(settings.database_url, pool_size=settings.database_pool_size,
                                       max_backoff_s=settings.db_reconnect_max_backoff_s)
    app.state.settings = settings
    app.state.base_settings = settings          # environment only; dashboard credentials are merged over it
    app.state.credentials_error = None
    app.state.db = db
    app.state.deps = deps or Deps.build(settings, db)
    app.state.verifier = TokenVerifier(settings)
    app.state.limiter = RateLimiter(db)
    app.state.livekit = LiveKitClient(settings)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type",
                                                                                     "X-Workspace-Id", "Idempotency-Key", "Last-Event-ID"])

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_req: Request, exc: RequestValidationError):
        # Never echo submitted values (they may be credentials): report field locations and messages only.
        errs = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_FAILED", "message": "invalid request",
                                                                "details": {"fields": errs}}})

    @app.exception_handler(JarvisError)
    async def _jarvis_error(_req: Request, exc: JarvisError):
        return JSONResponse(status_code=exc.http_status,
                            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    app.include_router(ops.public)
    app.include_router(campaigns.router)
    app.include_router(records.router)
    app.include_router(ops.router)
    app.include_router(integrations_routes.router)
    app.include_router(conversation.router)
    return app


app = None


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app
