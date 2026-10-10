from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

from app.config import RUNTIME_KEYS, get_settings
from app.db.session import session_scope
from app.services.state import masked_config
from app.web import queries
from app.web.deps import csrf_token, require_user, templates

router = APIRouter(dependencies=[Depends(require_user)])
REPORTS_DIR_NAME = "reports"


def reports_dir() -> Any:
    import os
    from pathlib import Path

    env = os.environ.get("EDGECAST_REPORTS_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[3] / "backtest" / REPORTS_DIR_NAME


def render(request: Request, name: str, ctx: dict[str, Any], *, page: str) -> Response:
    settings = get_settings()
    with session_scope() as s:
        hdr = queries.header(s, settings)
    return templates.TemplateResponse(
        request,
        name,
        {**ctx, "hdr": hdr, "page": page, "csrf": csrf_token(request), "user": request.session.get("user")},
    )


@router.get("/", response_class=HTMLResponse)
def overview(request: Request) -> Response:
    with session_scope() as s:
        ctx = queries.overview(s, get_settings())
        return render(request, "overview.html", ctx, page="overview")


@router.get("/signals", response_class=HTMLResponse)
def signals(
    request: Request, confidence: str = "", city: str = "", min_edge: float = 0.0, partial: int = 0, q: str = ""
) -> Response:
    with session_scope() as s:
        ctx = queries.signals(
            s, get_settings(), text=q.strip()[:80], confidence=confidence, city=city, min_edge=min_edge / 100
        )
        if partial:
            return templates.TemplateResponse(
                request, "partials/signals_table.html", {**ctx, "csrf": csrf_token(request)}
            )
        return render(request, "signals.html", ctx, page="signals")


@router.get("/positions", response_class=HTMLResponse)
def positions(request: Request, tab: str = "BOT") -> Response:
    tab = "MANUAL" if tab.upper() == "MANUAL" else "BOT"
    settings = get_settings()
    with session_scope() as s:
        ctx = {
            "tab": tab,
            "rows": queries.positions(s, tab),
            "reeval": settings.REEVAL_MINUTES,
            "cards": {b: queries.book_card(s, b, settings.TZ) for b in ("PAPER", "LIVE", "MANUAL")},
        }
        return render(request, "positions.html", ctx, page="positions")


@router.get("/history", response_class=HTMLResponse)
def history(request: Request, city: str = "", mode: str = "", outcome: str = "", day: str = "") -> Response:
    with session_scope() as s:
        rows = queries.history(s, city=city, mode=mode, outcome=outcome, day=day)
        ctx = {"rows": rows, "f": {"city": city, "mode": mode, "outcome": outcome, "day": day}}
        return render(request, "history.html", ctx, page="history")


@router.get("/history.csv")
def history_csv(city: str = "", mode: str = "", outcome: str = "", day: str = "") -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "created_at",
            "mode",
            "city",
            "date",
            "market",
            "bucket",
            "side",
            "outcome",
            "limit",
            "size",
            "filled",
            "avg_fill",
            "fees_usd",
            "status",
            "reason",
            "intent_id",
            "exchange_order_id",
        ]
    )
    with session_scope() as s:
        for r in queries.history(s, city=city, mode=mode, outcome=outcome, day=day, limit=100_000):
            it = r["it"]
            w.writerow(
                [
                    it.created_at.isoformat(),
                    it.mode,
                    r["ev"].city,
                    r["ev"].local_date,
                    r["ev"].title,
                    r["m"].bucket_label,
                    it.side,
                    it.outcome,
                    it.price,
                    it.size,
                    it.filled_size,
                    f"{r['avg']:.4f}" if r["avg"] is not None else "",
                    f"{r['fees']:.6f}",
                    it.status,
                    it.reason,
                    it.id,
                    it.exchange_order_id or "",
                ]
            )
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=edgecast-history.csv"},
    )


@router.get("/model", response_class=HTMLResponse)
def model(request: Request) -> Response:
    with session_scope() as s:
        return render(request, "model.html", queries.model_page(s, get_settings()), page="model")


@router.get("/backtests", response_class=HTMLResponse)
def backtests(request: Request) -> Response:
    with session_scope() as s:
        return render(request, "backtests.html", {"reports": queries.backtests(s, reports_dir())}, page="backtests")


@router.get("/backtests/{name}")
def backtest_report(name: str) -> Response:
    d = reports_dir().resolve()
    path = (d / name).resolve()
    if path.parent != d or path.suffix != ".html" or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="text/html")


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> Response:
    with session_scope() as s:
        ctx = {"config": masked_config(s), "runtime_keys": RUNTIME_KEYS, "health": queries.health(s)}
        return render(request, "settings.html", ctx, page="settings")


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request) -> Response:
    with session_scope() as s:
        return render(request, "audit.html", {"rows": queries.audit_log(s)}, page="audit")
