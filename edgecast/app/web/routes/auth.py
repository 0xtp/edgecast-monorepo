from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.config import get_settings
from app.db.session import session_scope
from app.services.state import audit
from app.web.deps import (
    check_password,
    client_ip,
    csrf_token,
    login_allowed,
    record_failed_login,
    reset_login,
    templates,
)

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> Response:
    configured = bool(get_settings().UI_PASSWORD_HASH.get_secret_value())
    return templates.TemplateResponse(
        request, "login.html", {"error": "", "configured": configured, "csrf": csrf_token(request)}
    )


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)) -> Response:
    ip = client_ip(request)
    configured = bool(get_settings().UI_PASSWORD_HASH.get_secret_value())
    if not login_allowed(ip):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Too many attempts. Try again in 15 minutes.",
                "configured": configured,
                "csrf": csrf_token(request),
            },
            status_code=429,
        )
    if not check_password(username, password):
        record_failed_login(ip)
        with session_scope() as s:
            audit(s, "UI", "LOGIN_FAILED", f"user={username[:40]} ip={ip}")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid credentials.", "configured": configured, "csrf": csrf_token(request)},
            status_code=401,
        )
    reset_login(ip)
    request.session.clear()
    request.session["user"] = username
    csrf_token(request)
    with session_scope() as s:
        audit(s, "UI", "LOGIN", f"ip={ip}", username)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
