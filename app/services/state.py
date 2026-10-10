"""Runtime state (mode, halt, pause, health), effective settings, audit log, commands, and alerts.

Shared by the worker, the web app and the Telegram bot so every control path behaves identically.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import RUNTIME_KEYS, SECRET_KEYS, Mode, Settings, get_settings
from app.db.models import AuditLog, Command, Outbox, RuntimeState, SettingOverride

# ------------------------------------------------------------------ key/value state


def get_state(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(RuntimeState, key)
    return default if row is None else row.value.get("v", default)


def set_state(session: Session, key: str, value: Any) -> None:
    row = session.get(RuntimeState, key)
    if row is None:
        session.add(RuntimeState(key=key, value={"v": value}))
    else:
        row.value = {"v": value}
    session.flush()


def now_utc() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ audit, commands, alerts


def audit(session: Session, source: str, action: str, detail: str = "", actor: str = "") -> None:
    session.add(AuditLog(source=source, action=action, detail=detail[:4000], actor=actor))


def enqueue(session: Session, kind: str, payload: dict[str, Any] | None = None, *, source: str, actor: str = "") -> int:
    cmd = Command(kind=kind, payload=payload or {}, source=source, actor=actor)
    session.add(cmd)
    session.flush()
    return cmd.id


def alert(
    session: Session, category: str, text: str, buttons: list[list[str]] | None = None, *, dedupe_key: str = ""
) -> None:
    if dedupe_key:
        last = get_state(session, f"alert:{dedupe_key}")
        if last == text:
            return
        set_state(session, f"alert:{dedupe_key}", text)
    session.add(Outbox(category=category, text=text, buttons=buttons or []))


# ------------------------------------------------------------------ mode / halt


def current_mode(session: Session, settings: Settings | None = None) -> Mode:
    s = settings or get_settings()
    if s.MODE_FORCE_ENV:
        return s.MODE
    stored = get_state(session, "mode")
    return Mode(stored) if stored else s.MODE


def is_halted(session: Session) -> tuple[bool, str]:
    return bool(get_state(session, "halted", False)), str(get_state(session, "halt_reason", ""))


def is_paused(session: Session) -> bool:
    return bool(get_state(session, "paused", False))


def halt(session: Session, reason: str, source: str, actor: str = "") -> None:
    if not get_state(session, "halted", False):
        set_state(session, "halted", True)
        set_state(session, "halt_reason", reason)
        set_state(session, "halted_at", now_utc().isoformat())
        audit(session, source, "HALT", reason, actor)
        alert(session, "risk", f"⛔ Trading HALTED: {reason}\nExits still allowed. Resume from the UI or /resume.")


# ------------------------------------------------------------------ effective settings


class Effective:
    """Env settings with DB runtime overrides layered on top (overrides only for whitelisted keys)."""

    def __init__(self, settings: Settings, overrides: dict[str, Any]):
        self._s = settings
        self.overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self.overrides:
            return self.overrides[name]
        return getattr(self._s, name)

    @property
    def env(self) -> Settings:
        return self._s


def load_overrides(session: Session) -> dict[str, Any]:
    return {o.key: o.value.get("v") for o in session.scalars(select(SettingOverride)) if o.key in RUNTIME_KEYS}


def effective(session: Session, settings: Settings | None = None) -> Effective:
    return Effective(settings or get_settings(), load_overrides(session))


class SettingError(ValueError):
    pass


def coerce_setting(key: str, raw: Any, settings: Settings) -> Any:
    """Validate a runtime value against its range and the env hard ceiling. Raises SettingError."""
    spec = RUNTIME_KEYS.get(key)
    if spec is None:
        raise SettingError(f"{key} is not a runtime-editable setting")
    if spec.kind == "bool":
        s = str(raw).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        raise SettingError(f"{key} expects true/false")
    if spec.kind == "csv":
        if isinstance(raw, list):
            return [str(x).strip().lower() for x in raw if str(x).strip()]
        return [x.strip().lower() for x in str(raw).split(",") if x.strip() and x.strip() != "-"]
    try:
        v = float(raw)
    except (TypeError, ValueError) as e:
        raise SettingError(f"{key} expects a number") from e
    if v != v or v in (float("inf"), float("-inf")):
        raise SettingError(f"{key} must be finite")
    if spec.lo is not None and v < spec.lo:
        raise SettingError(f"{key} must be >= {spec.lo}")
    if spec.hi is not None and v > spec.hi:
        raise SettingError(f"{key} must be <= {spec.hi}")
    if spec.ceiling is not None:
        ceiling = float(getattr(settings, spec.ceiling))
        if v > ceiling:
            raise SettingError(f"{key} cannot exceed the hard ceiling {spec.ceiling}={ceiling:g}")
    return v


def set_override(session: Session, key: str, raw: Any, *, source: str, actor: str = "") -> Any:
    value = coerce_setting(key, raw, get_settings())
    row = session.get(SettingOverride, key)
    old = row.value.get("v") if row else getattr(get_settings(), key)
    if row is None:
        session.add(SettingOverride(key=key, value={"v": value}, source=source, actor=actor))
    else:
        row.value, row.source, row.actor = {"v": value}, source, actor
    audit(session, source, "SET", f"{key}: {json.dumps(old)} -> {json.dumps(value)}", actor)
    session.flush()
    return value


def reset_override(session: Session, key: str, *, source: str, actor: str = "") -> None:
    row = session.get(SettingOverride, key)
    if row is not None:
        session.delete(row)
        audit(session, source, "RESET", f"{key} -> env {getattr(get_settings(), key)!r}", actor)
        session.flush()


def masked_config(session: Session) -> list[dict[str, Any]]:
    """Effective settings for display: secrets masked, origin marked (env | override)."""
    s = get_settings()
    overrides = load_overrides(session)
    rows = []
    for name in type(s).model_fields:
        if name in overrides:
            value, origin = overrides[name], "override"
        else:
            value, origin = getattr(s, name), "env"
        secret = name in SECRET_KEYS or isinstance(value, SecretStr)
        if secret:
            raw = value.get_secret_value() if isinstance(value, SecretStr) else value
            value = "•••• set" if raw else "(not set)"
        elif hasattr(value, "value"):
            value = value.value
        rows.append(
            {
                "key": name,
                "value": value,
                "origin": origin,
                "env_value": "••••" if secret else getattr(s, name),
                "editable": name in RUNTIME_KEYS,
            }
        )
    return rows
