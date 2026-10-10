"""FastAPI dashboard. Run: uvicorn --factory app.web.main:create_app"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db.models import Heartbeat
from app.db.session import get_engine, migrate, session_scope
from app.services.state import get_state, set_state
from app.web.deps import WEB_DIR, NotAuthenticated
from app.web.routes import actions, api, auth, pages

log = logging.getLogger("edgecast.web")
HEARTBEAT_STALE = timedelta(minutes=10)


async def heartbeat_watchdog() -> None:
    """If the worker stops beating for 10 minutes, alert via Telegram directly (the worker can't)."""
    from app.alerts import send_direct

    s = get_settings()
    while True:
        await asyncio.sleep(60)
        try:
            with session_scope() as db:
                hb = db.get(Heartbeat, "worker")
                stale = hb is not None and datetime.now(UTC) - hb.ts > HEARTBEAT_STALE
                alerted = get_state(db, "heartbeat_alerted", False)
                if stale and not alerted and s.TELEGRAM_ENABLED and s.TELEGRAM_BOT_TOKEN and s.TELEGRAM_CHAT_ID:
                    ok = await asyncio.to_thread(
                        send_direct,
                        s.TELEGRAM_BOT_TOKEN.get_secret_value(),
                        s.TELEGRAM_CHAT_ID,
                        f"💔 Edgecast worker heartbeat lost (last {hb.ts:%Y-%m-%d %H:%M}Z)",
                    )
                    set_state(db, "heartbeat_alerted", ok)
                elif not stale and alerted:
                    set_state(db, "heartbeat_alerted", False)
        except Exception:
            log.exception("heartbeat watchdog failed")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    migrate()
    task = asyncio.create_task(heartbeat_watchdog())
    yield
    task.cancel()


def create_app() -> FastAPI:
    s = get_settings()
    secret = s.SESSION_SECRET.get_secret_value()
    if len(secret) < 16:
        raise RuntimeError("SESSION_SECRET must be set to at least 16 random characters")
    app = FastAPI(title="Edgecast", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="edgecast_session",
        max_age=7 * 24 * 3600,
        same_site="strict",
        https_only=s.PUBLIC_DOMAIN not in ("localhost", ""),
    )
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(actions.router)
    app.include_router(api.router)
    if s.API_CORS_ORIGINS:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=s.API_CORS_ORIGINS,
            allow_methods=["GET"],
            allow_headers=["Authorization"],
            allow_credentials=False,
        )

    @app.exception_handler(NotAuthenticated)
    async def _login_redirect(request: Request, exc: NotAuthenticated) -> Response:
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    @app.get("/healthz")
    def healthz() -> Response:
        try:
            with get_engine().connect() as c:
                c.execute(text("SELECT 1"))
            with session_scope() as db:
                hb = db.get(Heartbeat, "worker")
                age = (datetime.now(UTC) - hb.ts).total_seconds() if hb else None
        except Exception as e:
            return JSONResponse({"status": "error", "db": str(e)[:200]}, status_code=503)
        return JSONResponse(
            {
                "status": "ok",
                "db": "ok",
                "worker_heartbeat_age_s": age,
                "worker_ok": age is not None and age < HEARTBEAT_STALE.total_seconds(),
            }
        )

    return app
