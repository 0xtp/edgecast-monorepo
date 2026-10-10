"""Telegram control bot (python-telegram-bot, async long polling) and alert delivery.

Security model:
* only user ids in TELEGRAM_ALLOWED_USER_IDS get any response; others are ignored and the attempt is logged;
* destructive commands need an inline two-step confirm that expires after 60 s; /live also needs the typed
  phrase CONFIRM LIVE and the LIVE_CONFIRM env flag;
* there is no command that can reveal secrets, change the wallet, withdraw, or exceed env hard ceilings.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time as _time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import RUNTIME_KEYS, Mode, Settings, in_quiet_hours
from app.db.models import Outbox, Position, Signal
from app.services import books, control
from app.services.state import audit, current_mode, effective, get_state, is_halted, is_paused, masked_config

log = logging.getLogger(__name__)
CONFIRM_TTL = 60
ALWAYS_ON = {"risk", "health", "summary", "info"}
TOGGLE = {
    "signal": "TELEGRAM_NOTIFY_SIGNALS",
    "order": "TELEGRAM_NOTIFY_ORDERS",
    "exit": "TELEGRAM_NOTIFY_EXITS",
    "resolution": "TELEGRAM_NOTIFY_RESOLUTIONS",
}

HELP = """Edgecast commands
/status – mode, halt, bankroll, P&L, exposure, geoblock
/opps [n] – top signals
/positions · /manual · /history [n]
/scan · /paper · /live – switch mode
/pause · /resume · /kill
/closeall · /close <position_id>
/took <signal_id> <price> <usd>
/set <key> <value> · /config · /help"""


def should_deliver(category: str, eff: Any, now_local: datetime) -> tuple[bool, str]:
    if category in TOGGLE and not getattr(eff, TOGGLE[category]):
        return False, "disabled by toggle"
    if category not in ALWAYS_ON and in_quiet_hours(now_local.time(), eff.TELEGRAM_QUIET_HOURS):
        return False, "muted (quiet hours)"
    return True, ""


def send_direct(token: str, chat_id: str, text: str) -> bool:
    """Plain Bot API call (used by the web process's heartbeat watchdog when the worker is down)."""
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000]},
            timeout=15,
        )
        return r.status_code == 200
    except Exception as e:
        log.warning("telegram direct send failed: %s", e)
        return False


@dataclass
class Pending:
    user_id: int
    label: str
    action: Callable[[], Awaitable[str]]
    expires: float


class TelegramBot:
    def __init__(self, settings: Settings, sessions: sessionmaker[Session], engine: Any):
        self.s = settings
        self.sessions = sessions
        self.engine = engine
        self.allowed = set(settings.TELEGRAM_ALLOWED_USER_IDS)
        self.pending: dict[str, Pending] = {}
        self.awaiting_live: dict[int, float] = {}
        self.app: Any = None
        self.connected = False

    # ------------------------------------------------------------------ plumbing
    async def db(self, fn: Callable[[Session], Any]) -> Any:
        def run() -> Any:
            with self.sessions() as s:
                out = fn(s)
                s.commit()
                return out

        return await asyncio.to_thread(run)

    def authorised(self, update: Any) -> bool:
        user = update.effective_user
        return user is not None and user.id in self.allowed

    async def gate(self, update: Any, context: Any) -> None:
        """Group -1 handler: drop everything from users not on the allow list."""
        from telegram.ext import ApplicationHandlerStop

        if not self.authorised(update):
            user = update.effective_user
            log.warning("telegram: ignored update from unauthorised user %s", getattr(user, "id", None))
            await self.db(
                lambda s: audit(
                    s, "TELEGRAM", "UNAUTHORISED", f"user {getattr(user, 'id', None)} @{getattr(user, 'username', '')}"
                )
            )
            raise ApplicationHandlerStop

    async def log_cmd(self, update: Any) -> None:
        text = (update.effective_message.text or "") if update.effective_message else ""
        uid = str(update.effective_user.id)
        await self.db(lambda s: audit(s, "TELEGRAM", "CMD", text[:500], uid))

    async def reply(self, update: Any, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        markup = None
        if buttons:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in buttons]
            )
        await update.effective_message.reply_text(text[:4000], reply_markup=markup, disable_web_page_preview=True)

    async def confirm(self, update: Any, label: str, action: Callable[[], Awaitable[str]]) -> None:
        token = secrets.token_hex(6)
        self.pending[token] = Pending(update.effective_user.id, label, action, _time.monotonic() + CONFIRM_TTL)
        await self.reply(
            update,
            f"Confirm: {label}? (expires in {CONFIRM_TTL}s)",
            [[("✅ Confirm", f"cf:{token}"), ("✖ Cancel", f"cx:{token}")]],
        )

    # ------------------------------------------------------------------ commands
    async def cmd_help(self, update: Any, context: Any) -> None:
        await self.log_cmd(update)
        await self.reply(update, HELP)

    async def cmd_status(self, update: Any, context: Any) -> None:
        await self.log_cmd(update)
        await self.reply(update, await self.db(lambda s: status_text(s, self.s)))

    async def cmd_opps(self, update: Any, context: Any) -> None:
        await self.log_cmd(update)
        n = _int_arg(context.args, 5)

        def q(s: Session) -> str:
            sigs = s.scalars(
                select(Signal).where(Signal.status == "ACTIVE").order_by(Signal.edge.desc()).limit(n)
            ).all()
            if not sigs:
                return "No active signals."
            return "\n\n".join(
                f"#{g.id} BUY {g.outcome} {g.market.bucket_label} — {g.market.event.title}\n"
                f"model {100 * g.p_model:.0f}% vs ask {100 * g.ask:.0f}% · edge {100 * g.edge:.1f}% · "
                f"max {g.max_limit_price:.2f} · ${g.suggested_stake_usd:.0f} · {g.confidence}\n{g.market.url}"
                for g in sigs
            )

        await self.reply(update, await self.db(q))

    async def _positions(self, update: Any, track: str) -> None:
        def q(s: Session) -> str:
            rows = s.scalars(select(Position).where(Position.track == track, Position.status == "OPEN")).all()
            if not rows:
                return f"No open {track.lower()} positions."
            lines = []
            for p in rows:
                mark = p.last_mark if p.last_mark is not None else p.avg_price
                upnl = p.size * mark - books.usd(p.cost_micros)
                sig = f" · EXIT: {p.exit_signal}" if p.exit_signal else ""
                lines.append(
                    f"[{p.id}] {p.mode} {p.outcome} {p.size:g} {p.market.bucket_label} · "
                    f"{p.market.event.title}\nentry {p.avg_price:.3f} mark {mark:.2f} uPnL ${upnl:+.2f}{sig}"
                )
            return "\n\n".join(lines)

        await self.log_cmd(update)
        await self.reply(update, await self.db(q))

    async def cmd_positions(self, update: Any, context: Any) -> None:
        await self._positions(update, "BOT")

    async def cmd_manual(self, update: Any, context: Any) -> None:
        await self._positions(update, "MANUAL")

    async def cmd_history(self, update: Any, context: Any) -> None:
        await self.log_cmd(update)
        n = _int_arg(context.args, 10)

        def q(s: Session) -> str:
            from app.db.models import OrderIntent

            rows = s.scalars(select(OrderIntent).order_by(OrderIntent.created_at.desc()).limit(n)).all()
            return (
                "\n".join(
                    f"{r.created_at:%m-%d %H:%M} {r.mode} {r.side} {r.outcome} {r.filled_size:g}/{r.size:g} @ "
                    f"{r.price:.2f} {r.status} · {r.market.bucket_label} · {r.reason[:60]}"
                    for r in rows
                )
                or "No orders yet."
            )

        await self.reply(update, await self.db(q))

    async def _switch(self, update: Any, target: Mode) -> None:
        await self.log_cmd(update)
        uid = str(update.effective_user.id)
        current = await self.db(lambda s: current_mode(s, self.s))
        if current == Mode.LIVE and target != Mode.LIVE:
            choices = [
                [("Keep orders · hold positions", f"lx:{target.value}:0:0")],
                [("Cancel orders · hold positions", f"lx:{target.value}:1:0")],
                [("Cancel orders · close positions", f"lx:{target.value}:1:1")],
            ]
            await self.reply(
                update,
                f"Leaving LIVE for {target.value}. New orders stop immediately. What about open orders and positions?",
                choices,
            )
            return
        if target == Mode.LIVE:
            errors, warnings = await self.db(lambda s: control.live_preflight(s, self.s))
            if errors:
                await self.reply(update, "Cannot switch to LIVE:\n• " + "\n• ".join(errors))
                return
            self.awaiting_live[update.effective_user.id] = _time.monotonic() + CONFIRM_TTL
            warn = ("\nWarnings:\n• " + "\n• ".join(warnings)) if warnings else ""
            await self.reply(update, f"Type {control.LIVE_PHRASE} within {CONFIRM_TTL}s to continue.{warn}")
            return

        async def go() -> str:
            r = await self.db(lambda s: control.switch_mode(s, target, source="TELEGRAM", actor=uid, settings=self.s))
            return r.message

        await self.reply(update, await go())

    async def cmd_scan(self, update: Any, context: Any) -> None:
        await self._switch(update, Mode.SCAN)

    async def cmd_paper(self, update: Any, context: Any) -> None:
        await self._switch(update, Mode.PAPER)

    async def cmd_live(self, update: Any, context: Any) -> None:
        await self._switch(update, Mode.LIVE)

    async def on_text(self, update: Any, context: Any) -> None:
        uid = update.effective_user.id
        text = (update.effective_message.text or "").strip()
        deadline = self.awaiting_live.pop(uid, None)
        if deadline is None:
            return
        await self.log_cmd(update)
        if _time.monotonic() > deadline or text != control.LIVE_PHRASE:
            await self.reply(update, "LIVE switch cancelled (phrase missing, wrong or expired).")
            return

        async def go() -> str:
            r = await self.db(
                lambda s: control.switch_mode(
                    s, Mode.LIVE, source="TELEGRAM", actor=str(uid), confirm_text=text, settings=self.s
                )
            )
            return r.message

        await self.confirm(update, "switch to LIVE trading with real money", go)

    async def cmd_pause(self, update: Any, context: Any) -> None:
        await self.log_cmd(update)
        uid = str(update.effective_user.id)
        r = await self.db(lambda s: control.pause(s, source="TELEGRAM", actor=uid))
        await self.reply(update, r.message)

    async def cmd_resume(self, update: Any, context: Any) -> None:
        await self.log_cmd(update)
        uid = str(update.effective_user.id)
        r = await self.db(lambda s: control.resume(s, source="TELEGRAM", actor=uid, settings=self.s))
        await self.reply(update, r.message)

    async def cmd_kill(self, update: Any, context: Any) -> None:
        await self.log_cmd(update)
        uid = str(update.effective_user.id)

        async def go() -> str:
            return (await self.db(lambda s: control.kill(s, source="TELEGRAM", actor=uid, settings=self.s))).message

        await self.confirm(update, "KILL: cancel all orders and halt", go)

    async def cmd_closeall(self, update: Any, context: Any) -> None:
        await self.log_cmd(update)
        uid = str(update.effective_user.id)

        async def go() -> str:
            return (await self.db(lambda s: control.close_all(s, source="TELEGRAM", actor=uid))).message

        await self.confirm(update, "close ALL bot positions at the bid", go)

    async def cmd_close(self, update: Any, context: Any) -> None:
        await self.log_cmd(update)
        uid = str(update.effective_user.id)
        if not context.args or not context.args[0].isdigit():
            await self.reply(update, "Usage: /close <position_id>")
            return
        pid = int(context.args[0])

        async def go() -> str:
            return (await self.db(lambda s: control.close_position(s, pid, source="TELEGRAM", actor=uid))).message

        await self.confirm(update, f"close position {pid} at the bid", go)

    async def cmd_took(self, update: Any, context: Any) -> None:
        await self.log_cmd(update)
        uid = str(update.effective_user.id)
        try:
            sid, price, usd = int(context.args[0]), float(context.args[1]), float(context.args[2])
        except (IndexError, ValueError):
            await self.reply(update, "Usage: /took <signal_id> <price 0-1> <usd>")
            return
        r = await self.db(lambda s: control.took(s, sid, price, usd, source="TELEGRAM", actor=uid))
        await self.reply(update, r.message)

    async def cmd_set(self, update: Any, context: Any) -> None:
        await self.log_cmd(update)
        uid = str(update.effective_user.id)
        if len(context.args) < 2:
            keys = ", ".join(RUNTIME_KEYS)
            await self.reply(update, f"Usage: /set <key> <value>\nEditable: {keys}")
            return
        key, value = context.args[0].upper(), " ".join(context.args[1:])
        spec = RUNTIME_KEYS.get(key)
        if spec is None:
            await self.reply(update, f"{key} is not editable at runtime.")
            return

        async def go() -> str:
            return (await self.db(lambda s: control.set_setting(s, key, value, source="TELEGRAM", actor=uid))).message

        if spec.risk:
            await self.confirm(update, f"set {key} = {value}", go)
        else:
            await self.reply(update, await go())

    async def cmd_config(self, update: Any, context: Any) -> None:
        await self.log_cmd(update)

        def q(s: Session) -> str:
            rows = masked_config(s)
            return "\n".join(
                f"{r['key']} = {r['value']}{' (override)' if r['origin'] == 'override' else ''}" for r in rows
            )

        await self.reply(update, await self.db(q))

    async def on_callback(self, update: Any, context: Any) -> None:
        q = update.callback_query
        await q.answer()
        uid = update.effective_user.id
        data = q.data or ""
        await self.db(lambda s: audit(s, "TELEGRAM", "BUTTON", data, str(uid)))
        if data.startswith(("cf:", "cx:")):
            p = self.pending.pop(data[3:], None)
            if p is None or p.user_id != uid or _time.monotonic() > p.expires:
                await q.edit_message_text("Expired or unknown confirmation.")
                return
            if data.startswith("cx:"):
                await q.edit_message_text(f"Cancelled: {p.label}")
                return
            await q.edit_message_text(f"Confirmed: {p.label}\n{await p.action()}")
        elif data.startswith("lx:"):
            _, target, cancel, close = data.split(":")
            r = await self.db(
                lambda s: control.switch_mode(
                    s,
                    target,
                    source="TELEGRAM",
                    actor=str(uid),
                    cancel_orders=cancel == "1",
                    close_positions=close == "1",
                    settings=self.s,
                )
            )
            await q.edit_message_text(r.message)
        elif data.startswith("took:"):
            sid = int(data[5:])
            sig = await self.db(lambda s: s.get(Signal, sid))
            hint = f"/took {sid} {sig.max_limit_price:.2f} {sig.suggested_stake_usd:.0f}" if sig else f"/took {sid} …"
            await q.message.reply_text(f"Log your fill with your actual price and USD, e.g.\n{hint}")
        elif data.startswith("ignore:"):
            sid = int(data[7:])
            r = await self.db(lambda s: control.ignore_signal(s, sid, source="TELEGRAM", actor=str(uid)))
            await q.message.reply_text(r.message)

    # ------------------------------------------------------------------ outbox
    async def drain_outbox(self) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        def fetch(s: Session) -> list[tuple[int, str, str, list[Any], bool, str]]:
            eff = effective(s, self.s)
            local = datetime.now(UTC).astimezone(ZoneInfo(self.s.TZ))
            out = []
            for o in s.scalars(select(Outbox).where(Outbox.sent.is_(False)).order_by(Outbox.id).limit(20)):
                ok, why = should_deliver(o.category, eff, local)
                out.append((o.id, o.category, o.text, o.buttons, ok, why))
            return out

        for oid, _cat, text, buttons, ok, why in await self.db(fetch):
            err = why
            if ok:
                try:
                    markup = None
                    if buttons:
                        markup = InlineKeyboardMarkup(
                            [[InlineKeyboardButton(b[0], callback_data=b[1]) for b in buttons]]
                        )
                    await self.app.bot.send_message(
                        chat_id=self.s.TELEGRAM_CHAT_ID,
                        text=text[:4000],
                        reply_markup=markup,
                        disable_web_page_preview=True,
                    )
                    self.connected = True
                except Exception as e:
                    err = f"send failed: {e}"
                    self.connected = False
                    log.warning("telegram send failed: %s", e)

            def mark(s: Session, oid: int = oid, err: str = err) -> None:
                o = s.get(Outbox, oid)
                if o is not None:
                    o.sent, o.sent_at, o.error = True, datetime.now(UTC), err

            await self.db(mark)

    async def outbox_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.drain_outbox()
                await self.db(lambda s: _set_tg_state(s, self.connected))
            except Exception:
                log.exception("outbox drain failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=3)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        from telegram import Update
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, TypeHandler, filters

        token = self.s.TELEGRAM_BOT_TOKEN.get_secret_value() if self.s.TELEGRAM_BOT_TOKEN else ""
        self.app = Application.builder().token(token).build()
        self.app.add_handler(TypeHandler(Update, self.gate), group=-1)
        for name in (
            "help",
            "start",
            "status",
            "opps",
            "positions",
            "manual",
            "history",
            "scan",
            "paper",
            "live",
            "pause",
            "resume",
            "kill",
            "closeall",
            "close",
            "took",
            "set",
            "config",
        ):
            fn = getattr(self, f"cmd_{'help' if name == 'start' else name}")
            self.app.add_handler(CommandHandler(name, fn))
        self.app.add_handler(CallbackQueryHandler(self.on_callback))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        self.connected = True
        log.info("telegram bot started")

    async def stop(self) -> None:
        if self.app is not None:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()


def _set_tg_state(s: Session, connected: bool) -> None:
    from app.services.state import set_state

    set_state(s, "telegram_connected", connected)
    set_state(s, "telegram_checked_at", datetime.now(UTC).isoformat())


def _int_arg(args: list[str] | None, default: int) -> int:
    try:
        return max(1, min(50, int(args[0]))) if args else default
    except ValueError:
        return default


def status_text(s: Session, settings: Settings) -> str:
    eff = effective(s, settings)
    mode = current_mode(s, settings)
    halted, why = is_halted(s)
    lines = [f"Mode: {mode.value}" + (" · PAUSED" if is_paused(s) else "") + (f" · HALTED ({why})" if halted else "")]
    geo = get_state(s, "geoblocked")
    lines.append(
        f"Geoblock: {'BLOCKED' if geo else 'ok' if geo is False else 'unknown'} ({get_state(s, 'geoblock_detail', '')})"
    )
    for book in ("PAPER", "LIVE", "MANUAL"):
        if not books.deposits(s, book):
            continue
        snap = books.snapshot(s, book, settings.TZ)
        lines.append(
            f"{book}: equity ${snap.equity:.2f} · today ${snap.realized_today:+.2f} · "
            f"total ${snap.total_pnl:+.2f} · exposure ${snap.exposure:.2f}"
        )
    lines.append(
        f"Caps: order ${eff.MAX_STAKE_PER_ORDER_USD:g} · market ${eff.MAX_EXPOSURE_PER_MARKET_USD:g} · "
        f"city/day ${eff.MAX_EXPOSURE_PER_CITY_DAY_USD:g} · total ${eff.MAX_TOTAL_EXPOSURE_USD:g} · "
        f"daily loss ${eff.MAX_DAILY_LOSS_USD:g} · DD {eff.MAX_DRAWDOWN_PCT:g}%"
    )
    active = s.scalar(select(func.count()).select_from(Signal).where(Signal.status == "ACTIVE")) or 0
    lines.append(f"Active signals: {active}")
    lines.append(f"Last forecast pull: {get_state(s, 'last_forecast_pull', 'never')}")
    return "\n".join(lines)


def daily_summary_text(s: Session, settings: Settings) -> str:
    since = books.start_of_today(settings.TZ)
    sigs = s.scalar(select(func.count()).select_from(Signal).where(Signal.created_at >= since)) or 0
    scored = s.execute(select(Signal.won).where(Signal.won.is_not(None))).all()
    hit = (sum(1 for (w,) in scored if w) / len(scored)) if scored else None
    parts = ["🗓 Daily summary", status_text(s, settings), f"Signals today: {sigs}"]
    parts.append(
        f"Signal hit rate (all time): {100 * hit:.0f}% of {len(scored)}"
        if hit is not None
        else "Signal hit rate: no resolved signals yet"
    )
    return "\n".join(parts)
