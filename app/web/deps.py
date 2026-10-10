"""Shared web plumbing: templates, auth, CSRF, login rate limiting."""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import bcrypt
from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from app.config import get_settings

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

LOGIN_WINDOW_S = 15 * 60
LOGIN_MAX_ATTEMPTS = 5
_attempts: dict[str, deque[float]] = defaultdict(deque)


class NotAuthenticated(Exception):
    pass


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def login_allowed(ip: str) -> bool:
    q = _attempts[ip]
    now = time.monotonic()
    while q and now - q[0] > LOGIN_WINDOW_S:
        q.popleft()
    return len(q) < LOGIN_MAX_ATTEMPTS


def record_failed_login(ip: str) -> None:
    _attempts[ip].append(time.monotonic())


def reset_login(ip: str) -> None:
    _attempts.pop(ip, None)


def check_password(username: str, password: str) -> bool:
    s = get_settings()
    stored = s.UI_PASSWORD_HASH.get_secret_value()
    if not stored or not secrets.compare_digest(username, s.UI_USERNAME):
        # constant-ish time even for unknown users
        bcrypt.checkpw(b"x", bcrypt.hashpw(b"y", bcrypt.gensalt(4)))
        return False
    try:
        return bcrypt.checkpw(password.encode(), stored.encode())
    except ValueError:
        return False


def require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise NotAuthenticated()
    return str(user)


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return str(tok)


async def verify_csrf(request: Request) -> None:
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token")
    if sent is None:
        form = await request.form()
        sent = str(form.get("csrf", ""))
    if not expected or not secrets.compare_digest(str(expected), sent or ""):
        raise HTTPException(status_code=403, detail="CSRF check failed")


# ------------------------------------------------------------------ template filters
def fmt_usd(v: Any, signed: bool = False) -> str:
    if v is None:
        return "—"
    v = float(v)
    s = f"{abs(v):,.2f}"
    sign = "-" if v < 0 else ("+" if signed and v > 0 else "")
    return f"{sign}${s}"


def fmt_pct(v: Any, digits: int = 1, signed: bool = False) -> str:
    if v is None:
        return "—"
    v = 100 * float(v)
    return f"{v:+.{digits}f}%" if signed else f"{v:.{digits}f}%"


def fmt_dt(v: Any, fmt: str = "%b %d %H:%M") -> str:
    if not v:
        return "—"
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return v
    if v.tzinfo is None:
        v = v.replace(tzinfo=UTC)
    return v.astimezone(ZoneInfo(get_settings().TZ)).strftime(fmt)


def fmt_ago(v: Any) -> str:
    if not v:
        return "never"
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    if v.tzinfo is None:
        v = v.replace(tzinfo=UTC)
    secs = (datetime.now(UTC) - v).total_seconds()
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs // 60)}m ago"
    if secs < 172800:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def fmt_hours(h: Any) -> str:
    from app.services.engine import fmt_hours as fh

    return "—" if h is None else fh(float(h))


def pnl_class(v: Any) -> str:
    if v is None or abs(float(v)) < 0.005:
        return "text-zinc-300"
    return "text-emerald-400" if float(v) > 0 else "text-rose-400"


def fmt_city(c: Any) -> str:
    c = str(c or "")
    return c.upper() if len(c) <= 3 else c.title()


templates.env.filters.update(
    city=fmt_city, usd=fmt_usd, pct=fmt_pct, dt=fmt_dt, ago=fmt_ago, hours=fmt_hours, pnl=pnl_class
)
