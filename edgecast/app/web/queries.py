"""Read models for the dashboard pages."""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    AuditLog,
    BacktestRun,
    Command,
    EquitySnapshot,
    Fill,
    Heartbeat,
    OrderIntent,
    Position,
    Prediction,
    ResolvedStation,
    Signal,
    StationBias,
    WeatherEvent,
)
from app.forecast.calibrate import brier, lead_band, reliability
from app.services import books
from app.services.control import live_preflight, paper_days
from app.services.engine import day_end_utc
from app.services.state import current_mode, effective, get_state, is_halted, is_paused


def header(s: Session, settings: Settings) -> dict[str, Any]:
    halted, why = is_halted(s)
    hb = s.get(Heartbeat, "worker")
    return {
        "mode": current_mode(s, settings).value,
        "halted": halted,
        "halt_reason": why,
        "paused": is_paused(s),
        "geoblocked": get_state(s, "geoblocked"),
        "geoblock_detail": get_state(s, "geoblock_detail", ""),
        "worker_ts": hb.ts if hb else None,
        "worker_stale": hb is None or datetime.now(UTC) - hb.ts > timedelta(minutes=10),
        "mode_forced": settings.MODE_FORCE_ENV,
    }


def book_card(s: Session, book: str, tz: str) -> dict[str, Any] | None:
    if not books.deposits(s, book) and not books.open_positions(s, book):
        return None
    snap = books.snapshot(s, book, tz)
    return {
        "book": book,
        "equity": snap.equity,
        "cash": snap.cash,
        "deposits": snap.deposits,
        "today": snap.realized_today,
        "total": snap.total_pnl,
        "realized": snap.realized_total,
        "unrealized": snap.unrealized,
        "exposure": snap.exposure,
        "open": len(books.open_positions(s, book)),
    }


def overview(s: Session, settings: Settings) -> dict[str, Any]:
    eff = effective(s, settings)
    mode = current_mode(s, settings).value
    bot_book = mode if mode in ("PAPER", "LIVE") else ("LIVE" if books.deposits(s, "LIVE") else "PAPER")
    cards = {b: book_card(s, b, settings.TZ) for b in ("PAPER", "LIVE", "MANUAL")}
    bot = cards.get(bot_book)
    snap = books.snapshot(s, bot_book, settings.TZ) if bot else None
    exposure = []
    if snap:
        worst_city = max(snap.exposure_by_city_day.values(), default=0.0)
        worst_market = max(snap.exposure_by_market.values(), default=0.0)
        exposure = [
            ("Total exposure", snap.exposure, float(eff.MAX_TOTAL_EXPOSURE_USD)),
            ("Largest market", worst_market, float(eff.MAX_EXPOSURE_PER_MARKET_USD)),
            ("Largest city/day", worst_city, float(eff.MAX_EXPOSURE_PER_CITY_DAY_USD)),
            ("Loss today", max(0.0, -snap.realized_today), float(eff.MAX_DAILY_LOSS_USD)),
        ]
        peak = float(get_state(s, f"equity_peak:{bot_book}", 0) or 0)
        dd = 100 * (peak - snap.equity) / peak if peak > 0 else 0.0
        exposure.append(("Drawdown %", max(dd, 0.0), float(eff.MAX_DRAWDOWN_PCT)))
    since = datetime.now(UTC) - timedelta(days=60)
    series: dict[str, list[tuple[int, float]]] = {}
    for row in s.scalars(select(EquitySnapshot).where(EquitySnapshot.ts >= since).order_by(EquitySnapshot.ts)):
        series.setdefault(row.book, []).append((int(row.ts.timestamp()), round(row.equity, 2)))
    errors, warnings = live_preflight(s, settings)
    return {
        "cards": cards,
        "bot_book": bot_book,
        "bot": bot,
        "exposure": exposure,
        "equity_series": series,
        "drawdown_series": drawdown(series.get(bot_book, [])),
        "live_errors": errors,
        "live_warnings": warnings,
        "paper_days": paper_days(s),
        "paper_min_days": settings.PAPER_MIN_DAYS,
        "active_signals": s.scalar(select(func.count()).select_from(Signal).where(Signal.status == "ACTIVE")) or 0,
        "pending_commands": s.scalars(select(Command).order_by(Command.id.desc()).limit(5)).all(),
    }


def drawdown(points: list[tuple[int, float]]) -> list[tuple[int, float]]:
    out, peak = [], -math.inf
    for t, v in points:
        peak = max(peak, v)
        out.append((t, round(-100 * (peak - v) / peak, 3) if peak > 0 else 0.0))
    return out


def signals(
    s: Session,
    settings: Settings,
    *,
    confidence: str = "",
    city: str = "",
    min_edge: float = 0.0,
    show_expired: bool = True,
) -> dict[str, Any]:
    q = select(Signal).order_by(Signal.status, Signal.edge.desc())
    rows = []
    now = datetime.now(UTC)
    from app.markets.parser import load_stations
    from app.markets.stations import load_db_stations

    load_db_stations(s)
    stations = load_stations()
    for sig in s.scalars(q.limit(400)):
        ev = sig.market.event
        if sig.status == "RESOLVED" or (not show_expired and sig.status != "ACTIVE"):
            continue
        if sig.status == "EXPIRED" and now - sig.updated_at > timedelta(hours=24):
            continue
        if confidence and sig.confidence != confidence:
            continue
        if city and ev.city != city:
            continue
        if sig.edge < min_edge:
            continue
        st = stations.get(ev.icao)
        hours = (day_end_utc(ev.local_date, st.tz) - now).total_seconds() / 3600 if st and ev.local_date else None
        rows.append({"sig": sig, "m": sig.market, "ev": ev, "hours": hours})
    cities = sorted(
        {
            c
            for (c,) in s.execute(select(WeatherEvent.city).where(WeatherEvent.parse_status == "PARSED").distinct())
            if c
        }
    )
    return {
        "rows": rows,
        "cities": cities,
        "f_conf": confidence,
        "f_city": city,
        "f_edge": min_edge,
        "mode": current_mode(s, settings).value,
    }


def positions(s: Session, track: str) -> list[dict[str, Any]]:
    out = []
    for p in s.scalars(
        select(Position).where(Position.track == track, Position.status == "OPEN").order_by(Position.opened_at.desc())
    ):
        mark = p.last_mark if p.last_mark is not None else p.avg_price
        cost = books.usd(p.cost_micros)
        edge = (p.last_p_model - mark) if p.last_p_model is not None else None
        out.append({"p": p, "mark": mark, "upnl": p.size * mark - cost, "cost": cost, "edge": edge})
    return out


def history(
    s: Session, *, city: str = "", mode: str = "", outcome: str = "", day: str = "", limit: int = 500
) -> list[dict[str, Any]]:
    q = select(OrderIntent).order_by(OrderIntent.created_at.desc()).limit(limit)
    rows = []
    for it in s.scalars(q):
        ev = it.market.event
        if (city and ev.city != city) or (mode and it.mode != mode) or (outcome and it.outcome != outcome):
            continue
        if day and str(ev.local_date) != day:
            continue
        fills = s.scalars(select(Fill).where(Fill.intent_id == it.id)).all()
        avg = sum(f.price * f.size for f in fills) / sum(f.size for f in fills) if fills else None
        fees = sum(f.fee_micros for f in fills) / 1e6
        rows.append({"it": it, "ev": ev, "m": it.market, "avg": avg, "fees": fees, "fills": len(fills)})
    return rows


def model_page(s: Session, settings: Settings) -> dict[str, Any]:
    eff = effective(s, settings)
    since = datetime.now(UTC) - timedelta(days=90)
    preds = s.execute(
        select(Prediction.city, Prediction.lead_hours, Prediction.p_model, Prediction.outcome).where(
            Prediction.outcome.is_not(None), Prediction.ts >= since
        )
    ).all()
    by_city: dict[str, list[tuple[float, int]]] = {}
    by_cell: dict[tuple[str, str], list[tuple[float, int]]] = {}
    for city, lead, p, o in preds:
        by_city.setdefault(city, []).append((p, int(o)))
        by_cell.setdefault((city, lead_band(lead)), []).append((p, int(o)))
    bands = ["0-24h", "24-48h", "48-72h", "72h+"]
    table = []
    for city in sorted(by_city):
        score, n = brier(by_city[city])
        cells = {b: brier(by_cell.get((city, b), [])) for b in bands}
        table.append({"city": city, "brier": score, "n": n, "cells": cells})
    allp = [(p, int(o)) for _, _, p, o in preds]
    rel = reliability(allp)
    biases = s.scalars(
        select(StationBias).order_by(StationBias.icao, StationBias.family, StationBias.variable, StationBias.lead_day)
    ).all()
    signals_scored = s.execute(select(Signal.won, Signal.roi_at_limit).where(Signal.won.is_not(None))).all()
    hits = sum(1 for w, _ in signals_scored if w)
    roi = (sum(r for _, r in signals_scored if r is not None) / len(signals_scored)) if signals_scored else None
    unparsed = s.scalars(
        select(WeatherEvent)
        .where(WeatherEvent.parse_status == "UNPARSED")
        .order_by(WeatherEvent.updated_at.desc())
        .limit(50)
    ).all()
    parsed_n = (
        s.scalar(select(func.count()).select_from(WeatherEvent).where(WeatherEvent.parse_status == "PARSED")) or 0
    )
    return {
        "table": table,
        "bands": bands,
        "overall": brier(allp),
        "reliability": [asdict(b) for b in rel],
        "disabled": get_state(s, "disabled_cities", []) or [],
        "max_brier": float(eff.MAX_BRIER),
        "biases": biases,
        "signal_n": len(signals_scored),
        "signal_hits": hits,
        "signal_roi": roi,
        "unparsed": unparsed,
        "parsed_n": parsed_n,
        "resolved_stations": s.scalars(select(ResolvedStation).order_by(ResolvedStation.resolved_at.desc())).all(),
        "last_bias_fit": get_state(s, "last_bias_fit"),
    }


def backtests(s: Session, reports_dir: Path) -> list[dict[str, Any]]:
    runs = {r.report_path: r for r in s.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc()))}
    files = (
        sorted(reports_dir.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
        if reports_dir.exists()
        else []
    )
    out = []
    for f in files:
        run = runs.get(f.name)
        out.append(
            {
                "name": f.name,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime, UTC),
                "summary": run.summary if run else {},
            }
        )
    return out


def audit_log(s: Session, limit: int = 500) -> list[AuditLog]:
    return list(s.scalars(select(AuditLog).order_by(AuditLog.ts.desc()).limit(limit)))


def health(s: Session) -> dict[str, Any]:
    hb = s.get(Heartbeat, "worker")
    return {
        "worker": hb.ts if hb else None,
        "worker_info": hb.info if hb else {},
        "last_forecast_pull": get_state(s, "last_forecast_pull"),
        "last_market_sync": get_state(s, "last_market_sync"),
        "last_strategy_run": get_state(s, "last_strategy_run"),
        "last_bias_fit": get_state(s, "last_bias_fit"),
        "geoblock_checked_at": get_state(s, "geoblock_checked_at"),
        "telegram_connected": get_state(s, "telegram_connected"),
        "telegram_checked_at": get_state(s, "telegram_checked_at"),
        "api": get_state(s, "api_health", {}) or {},
    }
