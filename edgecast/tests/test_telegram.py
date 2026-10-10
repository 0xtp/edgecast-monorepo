"""Telegram bot: authorisation, two-step confirms, LIVE phrase, /set ceilings, outbox delivery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from telegram.ext import ApplicationHandlerStop

from app import config as config_mod
from app.alerts import TelegramBot, daily_summary_text, should_deliver, status_text
from app.config import Mode
from app.db.models import AuditLog, Command, Outbox
from app.services.state import alert, current_mode, effective, get_state, set_state

ALLOWED = 111


@dataclass
class FakeUser:
    id: int
    username: str = "u"


@dataclass
class FakeMessage:
    text: str = ""
    replies: list[tuple[str, Any]] = field(default_factory=list)

    async def reply_text(self, text: str, reply_markup: Any = None, **kw: Any) -> None:
        self.replies.append((text, reply_markup))


@dataclass
class FakeCallback:
    data: str
    message: FakeMessage
    edited: list[str] = field(default_factory=list)

    async def answer(self) -> None:
        return None

    async def edit_message_text(self, text: str) -> None:
        self.edited.append(text)


@dataclass
class FakeUpdate:
    effective_user: FakeUser
    effective_message: FakeMessage
    callback_query: FakeCallback | None = None


@dataclass
class Ctx:
    args: list[str] = field(default_factory=list)


def upd(text: str = "", uid: int = ALLOWED) -> FakeUpdate:
    return FakeUpdate(FakeUser(uid), FakeMessage(text))


def buttons(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.fixture
def bot(sessions, monkeypatch, env):
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:abc")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", str(ALLOWED))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(ALLOWED))
    config_mod.get_settings.cache_clear()
    return TelegramBot(config_mod.get_settings(), sessions, engine=None)


async def test_unauthorised_users_are_ignored_and_logged(bot, sessions):
    with pytest.raises(ApplicationHandlerStop):
        await bot.gate(upd("/kill", uid=999), Ctx())
    await bot.gate(upd("/status"), Ctx())  # allowed: no exception
    with sessions() as s:
        assert s.scalar(select(AuditLog).where(AuditLog.action == "UNAUTHORISED")) is not None


async def test_status_and_help(bot):
    u = upd("/status")
    await bot.cmd_status(u, Ctx())
    assert "Mode: SCAN" in u.effective_message.replies[0][0]
    h = upd("/help")
    await bot.cmd_help(h, Ctx())
    assert "/took" in h.effective_message.replies[0][0]


async def test_kill_requires_confirm_and_expires(bot, sessions):
    u = upd("/kill")
    await bot.cmd_kill(u, Ctx())
    text, markup = u.effective_message.replies[0]
    assert "Confirm" in text
    token = buttons(markup)[0]
    # A different (even allowed) user cannot confirm someone else's prompt
    other = FakeUpdate(FakeUser(222), FakeMessage(), FakeCallback(token, FakeMessage()))
    await bot.on_callback(other, Ctx())
    assert "Expired or unknown" in other.callback_query.edited[0]
    # Fresh prompt, confirmed by the right user
    await bot.cmd_kill(u, Ctx())
    token = buttons(u.effective_message.replies[-1][1])[0]
    cb = FakeUpdate(FakeUser(ALLOWED), FakeMessage(), FakeCallback(token, FakeMessage()))
    await bot.on_callback(cb, Ctx())
    assert "Confirmed" in cb.callback_query.edited[0]
    with sessions() as s:
        assert get_state(s, "halted") is True
        assert s.scalar(select(Command).where(Command.kind == "CANCEL_ALL"))


async def test_confirm_expiry(bot, sessions, monkeypatch):
    u = upd("/closeall")
    await bot.cmd_closeall(u, Ctx())
    token = buttons(u.effective_message.replies[0][1])[0]
    for p in bot.pending.values():
        p.expires = 0
    cb = FakeUpdate(FakeUser(ALLOWED), FakeMessage(), FakeCallback(token, FakeMessage()))
    await bot.on_callback(cb, Ctx())
    assert "Expired" in cb.callback_query.edited[0]
    with sessions() as s:
        assert s.scalar(select(Command)) is None


async def test_live_needs_env_flag(bot):
    u = upd("/live")
    await bot.cmd_live(u, Ctx())
    assert "Cannot switch to LIVE" in u.effective_message.replies[0][0]


async def test_live_phrase_then_confirm_then_leave_live(sessions, monkeypatch, env):
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:abc")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", str(ALLOWED))
    monkeypatch.setenv("LIVE_CONFIRM", "I_ACCEPT_LOSSES")
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "1" * 64)
    config_mod.get_settings.cache_clear()
    s = config_mod.get_settings()
    b = TelegramBot(s, sessions, None)
    with sessions() as db:
        set_state(db, "geoblocked", False)
        db.commit()
    u = upd("/live")
    await b.cmd_live(u, Ctx())
    assert "Type CONFIRM LIVE" in u.effective_message.replies[0][0]
    wrong = upd("confirm live")
    await b.on_text(wrong, Ctx())
    assert "cancelled" in wrong.effective_message.replies[0][0]
    await b.cmd_live(u, Ctx())
    ok = upd("CONFIRM LIVE")
    await b.on_text(ok, Ctx())
    token = buttons(ok.effective_message.replies[0][1])[0]
    await b.on_callback(FakeUpdate(FakeUser(ALLOWED), FakeMessage(), FakeCallback(token, FakeMessage())), Ctx())
    with sessions() as db:
        assert current_mode(db, s) == Mode.LIVE
    # /scan from LIVE offers the keep/cancel/close choice
    sc = upd("/scan")
    await b.cmd_scan(sc, Ctx())
    choices = buttons(sc.effective_message.replies[0][1])
    assert choices == ["lx:SCAN:0:0", "lx:SCAN:1:0", "lx:SCAN:1:1"]
    cb = FakeUpdate(FakeUser(ALLOWED), FakeMessage(), FakeCallback("lx:SCAN:1:1", FakeMessage()))
    await b.on_callback(cb, Ctx())
    with sessions() as db:
        assert current_mode(db, s) == Mode.SCAN
        cmd = db.scalar(select(Command).where(Command.kind == "LIVE_EXIT"))
        assert cmd.payload == {"cancel_orders": True, "close_positions": True}


async def test_set_risk_needs_confirm_and_respects_ceiling(bot, sessions):
    u = upd("/set MAX_STAKE_PER_ORDER_USD 500")
    await bot.cmd_set(u, Ctx(["MAX_STAKE_PER_ORDER_USD", "500"]))
    token = buttons(u.effective_message.replies[0][1])[0]
    cb = FakeUpdate(FakeUser(ALLOWED), FakeMessage(), FakeCallback(token, FakeMessage()))
    await bot.on_callback(cb, Ctx())
    assert "hard ceiling" in cb.callback_query.edited[0]
    nr = upd()
    await bot.cmd_set(nr, Ctx(["TELEGRAM_NOTIFY_SIGNALS", "false"]))  # non-risk: applied immediately
    assert "False" in nr.effective_message.replies[0][0]
    bad = upd()
    await bot.cmd_set(bad, Ctx(["POLY_PRIVATE_KEY", "0xdead"]))
    assert "not editable" in bad.effective_message.replies[0][0]
    usage = upd()
    await bot.cmd_set(usage, Ctx([]))
    assert "Usage" in usage.effective_message.replies[0][0]


async def test_misc_commands(bot, sessions):
    for name, args in [
        ("opps", ["3"]),
        ("positions", []),
        ("manual", []),
        ("history", ["2"]),
        ("config", []),
        ("pause", []),
        ("resume", []),
        ("paper", []),
        ("scan", []),
    ]:
        u = upd(f"/{name}")
        await getattr(bot, f"cmd_{name}")(u, Ctx(args))
        assert u.effective_message.replies, name
    cfg = upd()
    await bot.cmd_config(cfg, Ctx())
    assert "1:abc" not in cfg.effective_message.replies[0][0]
    t = upd()
    await bot.cmd_took(t, Ctx(["5", "0.4", "10"]))
    assert "Logging manual trade" in t.effective_message.replies[0][0]
    t2 = upd()
    await bot.cmd_took(t2, Ctx(["x"]))
    assert "Usage" in t2.effective_message.replies[0][0]
    c = upd()
    await bot.cmd_close(c, Ctx(["7"]))
    assert "Confirm" in c.effective_message.replies[0][0]
    c2 = upd()
    await bot.cmd_close(c2, Ctx([]))
    assert "Usage" in c2.effective_message.replies[0][0]
    with sessions() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == "CMD")).first() is not None


def test_should_deliver(settings, db):
    eff = effective(db, settings)
    night = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
    eff.overrides["TELEGRAM_QUIET_HOURS"] = "00:30-07:00"
    assert should_deliver("signal", eff, night) == (False, "muted (quiet hours)")
    assert should_deliver("risk", eff, night)[0] and should_deliver("health", eff, night)[0]
    eff.overrides["TELEGRAM_NOTIFY_SIGNALS"] = False
    assert should_deliver("signal", eff, datetime(2026, 1, 1, 12, tzinfo=UTC)) == (False, "disabled by toggle")


async def test_outbox_drain(bot, sessions):
    sent: list[str] = []

    class FakeBot:
        async def send_message(self, chat_id: str, text: str, **kw: Any) -> None:
            sent.append(text)

    class FakeApp:
        bot = FakeBot()

    bot.app = FakeApp()
    with sessions() as s:
        alert(s, "risk", "halted!")
        alert(s, "signal", "new signal", [["Took it", "took:1"]])
        s.commit()
    await bot.drain_outbox()
    assert sent == ["halted!", "new signal"]
    with sessions() as s:
        assert all(o.sent for o in s.scalars(select(Outbox)))


def test_summary_texts(db, settings):
    assert "Mode:" in status_text(db, settings)
    assert "Daily summary" in daily_summary_text(db, settings)
