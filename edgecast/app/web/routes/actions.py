"""State-changing endpoints (POST + CSRF). Each returns a small HTML toast for HTMX, or redirects."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from markupsafe import escape

from app.db.session import session_scope
from app.services import control
from app.web.deps import require_user, verify_csrf

router = APIRouter(prefix="/action", dependencies=[Depends(require_user), Depends(verify_csrf)])


def toast(ok: bool, msg: str, *, refresh: bool = False, needs: str = "") -> Response:
    accent = "border-l-emerald-500" if ok else "border-l-amber-500"
    icon = "✓" if ok else "!"
    tone = "bg-emerald-50 text-emerald-700" if ok else "bg-amber-50 text-amber-700"
    html = (
        f'<div class="flex items-start gap-3 rounded-xl border border-gray-200 border-l-4 {accent} bg-white px-4 py-3 '
        f'text-sm text-gray-800 shadow-pop" data-needs="{needs}">'
        f'<span class="grid h-6 w-6 shrink-0 place-items-center rounded-full text-xs font-bold {tone}">{icon}</span>'
        f"<span>{escape(msg)}</span></div>"
    )
    headers = {"HX-Refresh": "true"} if refresh and ok else {}
    return HTMLResponse(html, headers=headers)


def actor(request: Request) -> str:
    return str(request.session.get("user", ""))


@router.post("/mode")
def mode(
    request: Request, target: str = Form(...), confirm_text: str = Form(""), live_exit: str = Form("")
) -> Response:
    cancel = close = None
    if live_exit:  # "<cancel orders 0|1>:<close positions 0|1>"
        a, _, b = live_exit.partition(":")
        cancel, close = a == "1", b == "1"
    with session_scope() as s:
        r = control.switch_mode(
            s,
            target,
            source="UI",
            actor=actor(request),
            confirm_text=confirm_text or None,
            cancel_orders=cancel,
            close_positions=close,
        )
    return toast(r.ok, r.message, refresh=True, needs=r.needs)


@router.post("/scan")
def scan_now(request: Request) -> Response:
    from app.services.state import audit, enqueue

    with session_scope() as s:
        enqueue(s, "SCAN_NOW", {}, source="UI", actor=actor(request))
        audit(s, "UI", "SCAN_NOW", "", actor(request))
    return toast(True, "Scan queued: forecasts and signals refresh within a minute")


@router.post("/pause")
def pause(request: Request) -> Response:
    with session_scope() as s:
        r = control.pause(s, source="UI", actor=actor(request))
    return toast(r.ok, r.message, refresh=True)


@router.post("/resume")
def resume(request: Request) -> Response:
    with session_scope() as s:
        r = control.resume(s, source="UI", actor=actor(request))
    return toast(r.ok, r.message, refresh=True)


@router.post("/kill")
def kill(request: Request) -> Response:
    with session_scope() as s:
        r = control.kill(s, source="UI", actor=actor(request))
    return toast(r.ok, r.message, refresh=True)


@router.post("/close/{position_id}")
def close(request: Request, position_id: int) -> Response:
    with session_scope() as s:
        r = control.close_position(s, position_id, source="UI", actor=actor(request))
    return toast(r.ok, r.message)


@router.post("/took/{signal_id}")
def took(request: Request, signal_id: int, price: float = Form(...), usd: float = Form(...)) -> Response:
    with session_scope() as s:
        r = control.took(s, signal_id, price, usd, source="UI", actor=actor(request))
    return toast(r.ok, r.message)


@router.post("/ignore/{signal_id}")
def ignore(request: Request, signal_id: int) -> Response:
    with session_scope() as s:
        r = control.ignore_signal(s, signal_id, source="UI", actor=actor(request))
    return toast(r.ok, r.message)


@router.post("/set")
def set_setting(request: Request, key: str = Form(...), value: str = Form(...)) -> Response:
    with session_scope() as s:
        r = control.set_setting(s, key, value, source="UI", actor=actor(request))
    return toast(r.ok, r.message, refresh=True)


@router.post("/reset")
def reset_setting(request: Request, key: str = Form(...)) -> Response:
    with session_scope() as s:
        r = control.reset_setting(s, key, source="UI", actor=actor(request))
    return toast(r.ok, r.message, refresh=True)
