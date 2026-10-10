"""Keep the DB's view of weather events/markets in sync with Gamma (and fee data from the CLOB)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Market, WeatherEvent
from app.markets.parser import EventSpec, ParseError, UnknownStation, _dt, parse_event
from app.polymarket.clob import Clob

log = logging.getLogger(__name__)
FEE_REFRESH = timedelta(hours=6)


def upsert_event(session: Session, raw: dict[str, Any], resolver: Any = None) -> WeatherEvent:
    """Parse and store one Gamma event. Unparseable events are stored as UNPARSED, never traded."""
    event_id = str(raw.get("id"))
    ev = session.get(WeatherEvent, event_id)
    if ev is None:
        ev = WeatherEvent(id=event_id, slug="", title="", parse_status="UNPARSED")
        session.add(ev)
    ev.slug = str(raw.get("slug") or "")
    ev.title = str(raw.get("title") or "")
    ev.end_date = _dt(raw.get("endDate"))
    ev.closed = bool(raw.get("closed"))
    ev.raw = raw
    try:
        try:
            spec = parse_event(raw)
        except UnknownStation as us:
            if resolver is None:
                raise
            try:
                resolver.resolve(session, us.icao, us.city)
            except Exception as e:
                raise ParseError(f"station {us.icao} unknown and could not be resolved: {e}") from e
            spec = parse_event(raw)
    except ParseError as e:
        ev.parse_status, ev.parse_error = "UNPARSED", str(e)
        log.info("UNPARSED %s: %s", ev.slug, e)
        return ev
    _apply_spec(session, ev, spec)
    return ev


def _apply_spec(session: Session, ev: WeatherEvent, spec: EventSpec) -> None:
    ev.parse_status, ev.parse_error = "PARSED", ""
    ev.city, ev.icao, ev.local_date = spec.city, spec.station.icao, spec.local_date
    ev.variable, ev.unit, ev.source = spec.variable, spec.unit, spec.source
    ev.spec = spec.to_json()
    for ms in spec.markets:
        m = session.get(Market, ms.condition_id)
        if m is None:
            m = Market(
                id=ms.condition_id,
                event_id=ev.id,
                question=ms.question,
                bucket_label=ms.bucket.label,
                token_yes=ms.token_yes,
                token_no=ms.token_no,
            )
            session.add(m)
        m.event_id = ev.id
        m.gamma_id, m.slug, m.question = ms.gamma_id, ms.slug, ms.question
        m.bucket_label, m.bucket_lo, m.bucket_hi = ms.bucket.label, ms.bucket.lo, ms.bucket.hi
        m.token_yes, m.token_no = ms.token_yes, ms.token_no
        m.neg_risk, m.tick_size, m.min_order_size = ms.neg_risk, ms.tick_size, ms.min_order_size
        m.fees_enabled = ms.fees_enabled
        m.active, m.closed, m.accepting_orders = ms.active, ms.closed, ms.accepting_orders
        m.end_date = ms.end_date
        if ms.resolved_yes is not None:
            m.resolved_yes = ms.resolved_yes


def refresh_fees(session: Session, clob: Clob, *, now: datetime | None = None) -> int:
    """Fetch per-market fee parameters. A market with unknown fees is never traded."""
    now = now or datetime.now(UTC)
    rows = session.scalars(
        select(Market).join(WeatherEvent).where(WeatherEvent.parse_status == "PARSED", Market.closed.is_(False))
    ).all()
    n = 0
    for m in rows:
        if m.fee_checked_at and now - m.fee_checked_at < FEE_REFRESH:
            continue
        try:
            info = clob.market_info(m.id)
        except Exception as e:
            log.warning("fee lookup failed for %s: %s", m.id, e)
            continue
        if info.fee is not None:
            m.fee_rate, m.fee_exponent = info.fee.rate, info.fee.exponent
        elif m.fees_enabled is False:
            m.fee_rate, m.fee_exponent = 0.0, 0.0
        else:
            m.fee_rate = m.fee_exponent = None  # unknown -> untradeable
        m.tick_size = info.tick_size or m.tick_size
        m.neg_risk = info.neg_risk
        if info.min_order_size:
            m.min_order_size = info.min_order_size
        m.fee_checked_at = now
        n += 1
    return n


def sync(session: Session, events: list[dict[str, Any]], resolver: Any = None) -> tuple[int, int]:
    parsed = unparsed = 0
    for raw in events:
        ev = upsert_event(session, raw, resolver)
        if ev.parse_status == "PARSED":
            parsed += 1
        else:
            unparsed += 1
    session.flush()
    return parsed, unparsed
