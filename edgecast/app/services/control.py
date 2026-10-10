"""Operator actions shared by the web UI and the Telegram bot. Every action is audited.

State changes (mode, pause, halt) are written directly; exchange side-effects (cancel, close) are queued as
commands for the worker, the only process that talks to the exchange.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import Mode, Settings, get_settings
from app.services import books
from app.services.state import (
    alert,
    audit,
    current_mode,
    enqueue,
    get_state,
    halt,
    reset_override,
    set_override,
    set_state,
)

LIVE_PHRASE = "CONFIRM LIVE"


@dataclass
class Result:
    ok: bool
    message: str
    needs: str = ""  # "confirm_text" | "live_exit_choice" when more input is required


def paper_days(session: Session) -> float:
    started = get_state(session, "paper_started_at")
    if not started:
        return 0.0
    return (datetime.now(UTC) - datetime.fromisoformat(started)).total_seconds() / 86400


def live_preflight(session: Session, s: Settings) -> tuple[list[str], list[str]]:
    """(blocking problems, warnings) for switching into LIVE."""
    errors, warnings = [], []
    if not s.live_confirmed:
        errors.append("LIVE_CONFIRM is not set to I_ACCEPT_LOSSES in the environment")
    if not s.has_private_key:
        errors.append("no POLY_PRIVATE_KEY / POLY_PRIVATE_KEY_FILE configured")
    if get_state(session, "geoblocked", True):
        errors.append(f"geoblock check says BLOCKED ({get_state(session, 'geoblock_detail', 'unknown')})")
    days = paper_days(session)
    if days < s.PAPER_MIN_DAYS:
        msg = f"PAPER has run {days:.1f} of the suggested {s.PAPER_MIN_DAYS} days"
        (errors if s.REQUIRE_PAPER_BEFORE_LIVE else warnings).append(msg)
    if get_state(session, "halted", False):
        warnings.append("trading is currently halted; LIVE will not place entries until you resume")
    return errors, warnings


def switch_mode(
    session: Session,
    target: Mode | str,
    *,
    source: str,
    actor: str = "",
    confirm_text: str | None = None,
    cancel_orders: bool | None = None,
    close_positions: bool | None = None,
    settings: Settings | None = None,
) -> Result:
    s = settings or get_settings()
    target = Mode(str(target).upper())
    current = current_mode(session, s)
    if s.MODE_FORCE_ENV:
        return Result(False, "MODE_FORCE_ENV=true: the mode is pinned by the environment")
    if target == current:
        return Result(True, f"already in {target.value}")
    if target == Mode.LIVE:
        errors, warnings = live_preflight(session, s)
        if errors:
            return Result(False, "Cannot switch to LIVE: " + "; ".join(errors))
        if (confirm_text or "").strip() != LIVE_PHRASE:
            note = (" Warnings: " + "; ".join(warnings)) if warnings else ""
            return Result(False, f"Type {LIVE_PHRASE} to switch to LIVE.{note}", needs="confirm_text")
    if current == Mode.LIVE:
        if cancel_orders is None or close_positions is None:
            return Result(
                False,
                "Leaving LIVE: keep or cancel open orders, hold or close open positions?",
                needs="live_exit_choice",
            )
        enqueue(
            session,
            "LIVE_EXIT",
            {"cancel_orders": cancel_orders, "close_positions": close_positions},
            source=source,
            actor=actor,
        )
    set_state(session, "mode", target.value)
    if target == Mode.PAPER:
        books.ensure_deposit(session, "PAPER", float(s.BANKROLL_USD))
        if not get_state(session, "paper_started_at"):
            set_state(session, "paper_started_at", datetime.now(UTC).isoformat())
    if target == Mode.LIVE:
        set_state(session, "live_started_at", datetime.now(UTC).isoformat())
        enqueue(session, "RECONCILE", {}, source=source, actor=actor)
    extra = ""
    if current == Mode.LIVE:
        extra = (
            f" (orders: {'cancel' if cancel_orders else 'keep'}, positions: {'close' if close_positions else 'hold'})"
        )
    audit(session, source, "MODE", f"{current.value} -> {target.value}{extra}", actor)
    alert(session, "risk", f"🔀 Mode {current.value} → {target.value}{extra} (by {source} {actor})".strip())
    session.flush()
    return Result(True, f"Mode switched {current.value} → {target.value}{extra}")


def pause(session: Session, *, source: str, actor: str = "") -> Result:
    set_state(session, "paused", True)
    audit(session, source, "PAUSE", "no new entries; exits continue", actor)
    alert(session, "risk", f"⏸ Paused by {source}: no new entries, exits continue")
    return Result(True, "Paused: no new entries, exits continue")


def resume(session: Session, *, source: str, actor: str = "", settings: Settings | None = None) -> Result:
    s = settings or get_settings()
    if s.kill_file.exists():
        try:
            s.kill_file.unlink()
        except OSError as e:
            return Result(False, f"KILL file present and could not be removed: {e}")
    was = get_state(session, "halt_reason", "")
    set_state(session, "paused", False)
    set_state(session, "halted", False)
    set_state(session, "halt_reason", "")
    set_state(session, "kill_file_seen", "0")
    # Re-baseline the drawdown peak so an old peak cannot re-trigger the halt immediately.
    for book in ("PAPER", "LIVE"):
        if books.deposits(session, book):
            snap = books.snapshot(session, book, s.TZ)
            set_state(session, f"equity_peak:{book}", snap.equity)
    audit(session, source, "RESUME", f"cleared halt ({was})" if was else "resumed", actor)
    alert(session, "risk", f"▶️ Resumed by {source}{f' (was halted: {was})' if was else ''}")
    return Result(True, "Resumed")


def kill(session: Session, *, source: str, actor: str = "", settings: Settings | None = None) -> Result:
    s = settings or get_settings()
    try:
        s.kill_file.parent.mkdir(parents=True, exist_ok=True)
        s.kill_file.write_text(f"kill switch by {source} {actor} at {datetime.now(UTC).isoformat()}\n")
    except OSError:
        pass  # the DB halt + command below still stop trading
    halt(session, f"kill switch ({source})", source, actor)
    set_state(session, "kill_file_seen", "1")
    enqueue(session, "CANCEL_ALL", {}, source=source, actor=actor)
    audit(session, source, "KILL", "cancel all orders and halt", actor)
    return Result(True, "Kill switch: cancelling all orders and halting")


def close_position(session: Session, position_id: int, *, source: str, actor: str = "") -> Result:
    enqueue(session, "CLOSE_POSITION", {"position_id": position_id}, source=source, actor=actor)
    audit(session, source, "CLOSE", f"position {position_id}", actor)
    return Result(True, f"Close requested for position {position_id}")


def close_all(session: Session, *, source: str, actor: str = "") -> Result:
    mode = current_mode(session)
    book = mode.value if mode != Mode.SCAN else None
    enqueue(session, "CLOSE_ALL", {"book": book}, source=source, actor=actor)
    audit(session, source, "CLOSE_ALL", f"book={book or 'all'}", actor)
    return Result(True, "Close-all requested for bot positions")


def took(session: Session, signal_id: int, price: float, usd: float, *, source: str, actor: str = "") -> Result:
    if not (0 < price < 1):
        return Result(False, "price must be between 0 and 1 (e.g. 0.42)")
    if usd <= 0 or usd > 100_000:
        return Result(False, "usd must be positive")
    enqueue(session, "TOOK", {"signal_id": signal_id, "price": price, "usd": usd}, source=source, actor=actor)
    return Result(True, f"Logging manual trade on signal #{signal_id}: ${usd:g} @ {price:.2f}")


def ignore_signal(session: Session, signal_id: int, *, source: str, actor: str = "") -> Result:
    from app.db.models import Signal

    sig = session.get(Signal, signal_id)
    if sig is None:
        return Result(False, "signal not found")
    sig.ignored = True
    audit(session, source, "IGNORE", f"signal {signal_id}", actor)
    return Result(True, f"Signal #{signal_id} ignored (no more alerts)")


def set_setting(session: Session, key: str, value: Any, *, source: str, actor: str = "") -> Result:
    try:
        v = set_override(session, key.upper(), value, source=source, actor=actor)
    except ValueError as e:
        return Result(False, str(e))
    return Result(True, f"{key.upper()} = {v}")


def reset_setting(session: Session, key: str, *, source: str, actor: str = "") -> Result:
    reset_override(session, key.upper(), source=source, actor=actor)
    return Result(True, f"{key.upper()} reset to env value")


def stale_heartbeat(session: Session, name: str = "worker", minutes: int = 10) -> bool:
    from app.db.models import Heartbeat

    hb = session.get(Heartbeat, name)
    return hb is None or datetime.now(UTC) - hb.ts > timedelta(minutes=minutes)
