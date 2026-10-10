"""Config validation, runtime overrides and mode-switch rules (shared by UI and Telegram)."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app import config as config_mod
from app.config import Mode, Settings, in_quiet_hours, parse_hhmm
from app.db.models import AuditLog, Command
from app.services import control
from app.services.state import (
    SettingError,
    coerce_setting,
    current_mode,
    effective,
    get_state,
    masked_config,
    set_state,
)

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ config
def test_env_example_is_valid(tmp_path, monkeypatch, env):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = Settings()
    assert s.MODE == Mode.SCAN and pytest.approx({"ecmwf_ifs": 0.6, "gfs": 0.4}) == s.MODEL_WEIGHTS
    assert not s.has_private_key and not s.live_confirmed
    assert s.NWS_USER_AGENT == "edgecast (you@example.com)"


@pytest.mark.parametrize(
    "bad",
    [
        {"MAX_STAKE_PER_ORDER_USD": "60"},  # above HARD_MAX
        {"MODEL_WEIGHTS": "foo:1"},
        {"MODEL_WEIGHTS": "gfs:-1"},
        {"TELEGRAM_ALLOWED_USER_IDS": "abc"},
        {"TELEGRAM_ENABLED": "true"},  # no token / ids
        {"ENTRY_MIN_HOURS": "80"},
        {"TELEGRAM_QUIET_HOURS": "25:00-07:00"},
        {"POLY_FUNDER_ADDRESS": "0x123"},
        {"POLY_SIGNATURE_TYPE": "7"},
        {"MIN_EDGE": "0.5", "SANITY_MAX_EDGE": "0.4"},
    ],
)
def test_bad_config_fails_fast(env, monkeypatch, bad):
    for k, v in bad.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        Settings()


def test_private_key_sources(env, monkeypatch, tmp_path):
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "1" * 64)
    assert Settings().private_key() == "0x" + "1" * 64
    monkeypatch.delenv("POLY_PRIVATE_KEY")
    f = tmp_path / "key"
    f.write_text("0x" + "2" * 64 + "\n")
    monkeypatch.setenv("POLY_PRIVATE_KEY_FILE", str(f))
    assert Settings().private_key() == "0x" + "2" * 64
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "1" * 64)
    with pytest.raises(ValidationError):
        Settings()


def test_quiet_hours():
    assert in_quiet_hours(time(1, 0), "00:30-07:00")
    assert not in_quiet_hours(time(8, 0), "00:30-07:00")
    assert in_quiet_hours(time(23, 30), "22:00-06:00") and in_quiet_hours(time(5, 0), "22:00-06:00")
    assert not in_quiet_hours(time(5, 0), "")
    assert parse_hhmm("7:05") == time(7, 5)


# ------------------------------------------------------------------ runtime settings
def test_overrides_respect_ranges_and_hard_ceilings(db, settings):
    assert coerce_setting("MIN_EDGE", "0.08", settings) == 0.08
    assert coerce_setting("CITY_ALLOWLIST", "NYC, London", settings) == ["nyc", "london"]
    assert coerce_setting("TELEGRAM_NOTIFY_SIGNALS", "off", settings) is False
    for key, val in [
        ("MAX_STAKE_PER_ORDER_USD", "51"),
        ("MAX_DAILY_LOSS_USD", "61"),
        ("MIN_EDGE", "0.9"),
        ("KELLY_FRACTION", "nan"),
        ("MIN_EDGE", "x"),
        ("TELEGRAM_NOTIFY_ORDERS", "maybe"),
        ("POLY_PRIVATE_KEY", "0xabc"),
        ("UI_PASSWORD_HASH", "x"),
        ("MAX_TOTAL_EXPOSURE_USD", "301"),
    ]:
        with pytest.raises(SettingError):
            coerce_setting(key, val, settings)
    r = control.set_setting(db, "max_stake_per_order_usd", "25", source="UI", actor="me")
    assert r.ok
    assert effective(db, settings).MAX_STAKE_PER_ORDER_USD == 25
    assert not control.set_setting(db, "MAX_STAKE_PER_ORDER_USD", "500", source="TELEGRAM").ok
    control.reset_setting(db, "MAX_STAKE_PER_ORDER_USD", source="UI")
    assert effective(db, settings).MAX_STAKE_PER_ORDER_USD == settings.MAX_STAKE_PER_ORDER_USD
    actions = [a.action for a in db.scalars(select(AuditLog))]
    assert "SET" in actions and "RESET" in actions


def test_masked_config_never_leaks_secrets(db, monkeypatch, env):
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "ab" * 32)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TOKVALUEXYZ")
    config_mod.get_settings.cache_clear()
    rows = masked_config(db)
    blob = repr(rows)
    assert "abab" not in blob and "TOKVALUEXYZ" not in blob and "test-secret" not in blob
    keyrow = next(r for r in rows if r["key"] == "POLY_PRIVATE_KEY")
    assert keyrow["value"] == "•••• set"


# ------------------------------------------------------------------ mode switching
def test_scan_to_paper_and_back(db, settings):
    assert current_mode(db, settings) == Mode.SCAN
    r = control.switch_mode(db, "PAPER", source="UI", settings=settings)
    assert r.ok and current_mode(db, settings) == Mode.PAPER
    assert get_state(db, "paper_started_at")
    assert control.switch_mode(db, "PAPER", source="UI", settings=settings).message.startswith("already")
    assert control.switch_mode(db, "SCAN", source="TELEGRAM", settings=settings).ok


def live_env(monkeypatch, **extra):
    monkeypatch.setenv("LIVE_CONFIRM", "I_ACCEPT_LOSSES")
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "1" * 64)
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    config_mod.get_settings.cache_clear()
    return config_mod.get_settings()


def test_live_requires_env_flag(db, settings):
    r = control.switch_mode(db, "LIVE", source="UI", confirm_text="CONFIRM LIVE", settings=settings)
    assert not r.ok and "LIVE_CONFIRM" in r.message


def test_live_requires_geoblock_clear_and_phrase(db, monkeypatch, env):
    s = live_env(monkeypatch)
    r = control.switch_mode(db, "LIVE", source="UI", confirm_text="CONFIRM LIVE", settings=s)
    assert not r.ok and "geoblock" in r.message  # unknown geoblock = blocked
    set_state(db, "geoblocked", False)
    r = control.switch_mode(db, "LIVE", source="UI", confirm_text="confirm live", settings=s)
    assert not r.ok and r.needs == "confirm_text" and "suggested" in r.message  # paper warning shown
    r = control.switch_mode(db, "LIVE", source="UI", confirm_text="CONFIRM LIVE", settings=s)
    assert r.ok and current_mode(db, s) == Mode.LIVE  # SCAN -> LIVE directly is allowed
    assert db.scalar(select(Command).where(Command.kind == "RECONCILE"))
    # Leaving LIVE asks what to do with orders/positions, then queues the choice.
    r = control.switch_mode(db, "SCAN", source="UI", settings=s)
    assert not r.ok and r.needs == "live_exit_choice" and current_mode(db, s) == Mode.LIVE
    r = control.switch_mode(db, "PAPER", source="UI", settings=s, cancel_orders=True, close_positions=False)
    assert r.ok and current_mode(db, s) == Mode.PAPER
    cmd = db.scalar(select(Command).where(Command.kind == "LIVE_EXIT"))
    assert cmd.payload == {"cancel_orders": True, "close_positions": False}


def test_require_paper_before_live(db, monkeypatch, env):
    s = live_env(monkeypatch, REQUIRE_PAPER_BEFORE_LIVE="true", PAPER_MIN_DAYS="14")
    set_state(db, "geoblocked", False)
    r = control.switch_mode(db, "LIVE", source="UI", confirm_text="CONFIRM LIVE", settings=s)
    assert not r.ok and "PAPER has run" in r.message
    set_state(db, "paper_started_at", (datetime.now(UTC) - timedelta(days=15)).isoformat())
    assert control.switch_mode(db, "LIVE", source="UI", confirm_text="CONFIRM LIVE", settings=s).ok


def test_mode_force_env(db, monkeypatch, env):
    monkeypatch.setenv("MODE_FORCE_ENV", "true")
    monkeypatch.setenv("MODE", "PAPER")
    config_mod.get_settings.cache_clear()
    s = config_mod.get_settings()
    set_state(db, "mode", "SCAN")
    assert current_mode(db, s) == Mode.PAPER
    assert not control.switch_mode(db, "SCAN", source="UI", settings=s).ok


def test_pause_resume_kill(db, settings):
    assert control.pause(db, source="UI").ok and get_state(db, "paused")
    assert control.kill(db, source="TELEGRAM", actor="42", settings=settings).ok
    assert settings.kill_file.exists() and get_state(db, "halted")
    assert db.scalar(select(Command).where(Command.kind == "CANCEL_ALL"))
    assert control.resume(db, source="UI", settings=settings).ok
    assert not settings.kill_file.exists() and not get_state(db, "halted") and not get_state(db, "paused")


def test_took_validation_and_close(db):
    assert not control.took(db, 1, 1.5, 10, source="UI").ok
    assert not control.took(db, 1, 0.5, -1, source="UI").ok
    assert control.took(db, 1, 0.5, 10, source="UI").ok
    assert control.close_position(db, 3, source="UI").ok
    assert control.close_all(db, source="UI").ok
    assert not control.ignore_signal(db, 999, source="UI").ok
    kinds = [c.kind for c in db.scalars(select(Command))]
    assert kinds == ["TOOK", "CLOSE_POSITION", "CLOSE_ALL"]
    assert control.stale_heartbeat(db)


def test_env_example_has_no_inline_comments():
    """docker compose's env_file parser keeps `KEY=  # note` as the literal value "# note"."""
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            assert "#" not in line.split("=", 1)[1], line
