"""Environment configuration.

Every env var is declared and validated here; the process refuses to start on a bad value.
Runtime overrides (UI / Telegram `/set`) live in the DB and are layered on top by
`app.services.settings`; the whitelist and its bounds are defined at the bottom of this file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LIVE_CONFIRM_PHRASE = "I_ACCEPT_LOSSES"


class Mode(StrEnum):
    SCAN = "SCAN"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Confidence(StrEnum):
    LOW = "LOW"
    MED = "MED"
    HIGH = "HIGH"

    @property
    def rank(self) -> int:
        return {"LOW": 0, "MED": 1, "HIGH": 2}[self.value]


def _csv(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list | tuple):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Mode & safety ---
    MODE: Mode = Mode.SCAN
    MODE_FORCE_ENV: bool = False
    LIVE_CONFIRM: str = ""
    REQUIRE_PAPER_BEFORE_LIVE: bool = False
    PAPER_MIN_DAYS: int = Field(14, ge=0, le=365)
    BANKROLL_USD: float = Field(200, gt=0)
    HARD_MAX_STAKE_PER_ORDER_USD: float = Field(50, gt=0)
    HARD_MAX_TOTAL_EXPOSURE_USD: float = Field(300, gt=0)
    HARD_MAX_DAILY_LOSS_USD: float = Field(60, gt=0)
    MAX_STAKE_PER_ORDER_USD: float = Field(10, gt=0)
    MAX_EXPOSURE_PER_MARKET_USD: float = Field(20, gt=0)
    MAX_EXPOSURE_PER_CITY_DAY_USD: float = Field(30, gt=0)
    MAX_TOTAL_EXPOSURE_USD: float = Field(100, gt=0)
    MAX_DAILY_LOSS_USD: float = Field(20, gt=0)
    MAX_DRAWDOWN_PCT: float = Field(15, gt=0, le=100)
    KELLY_FRACTION: float = Field(0.15, gt=0, le=1)
    ROUND_UP_TO_MIN_ORDER: bool = False

    # --- Edge & execution ---
    MIN_EDGE: float = Field(0.06, gt=0, lt=1)
    SANITY_MAX_EDGE: float = Field(0.40, gt=0, le=1)
    MAX_SPREAD_CENTS: float = Field(6, gt=0, le=100)
    MAX_SLIPPAGE_CENTS: float = Field(1, ge=0, le=100)
    MIN_LIQUIDITY_USD: float = Field(50, ge=0)
    MAX_BOOK_DEPTH_PCT: float = Field(25, gt=0, le=100)
    ENTRY_MIN_HOURS: float = Field(6, ge=0)
    ENTRY_MAX_HOURS: float = Field(72, gt=0)
    NO_NEW_ENTRY_HOURS_BEFORE_CLOSE: float = Field(2, ge=0)
    REEVAL_MINUTES: int = Field(30, ge=1, le=1440)
    TP_BUFFER: float = Field(0.02, ge=0, lt=1)
    EXIT_EDGE: float = Field(0.03, ge=0, lt=1)

    # --- Markets ---
    ENABLE_WEATHER: bool = True
    ENABLE_CPI: bool = False
    CITY_ALLOWLIST: CsvList = []
    CITY_BLOCKLIST: CsvList = []
    MAX_BRIER: float = Field(0.20, gt=0, le=1)

    # --- Forecast ---
    MODEL_WEIGHTS: Annotated[dict[str, float], NoDecode] = {"ecmwf_ifs": 0.6, "gfs": 0.4}
    MAX_FORECAST_AGE_MIN: int = Field(360, ge=10)
    OPEN_METEO_BASE: str = "https://ensemble-api.open-meteo.com"
    OPEN_METEO_PREVIOUS_RUNS_BASE: str = "https://previous-runs-api.open-meteo.com"
    OPEN_METEO_SINGLE_RUNS_BASE: str = "https://single-runs-api.open-meteo.com"
    OPEN_METEO_HISTORICAL_FORECAST_BASE: str = "https://historical-forecast-api.open-meteo.com"
    IEM_BASE: str = "https://mesonet.agron.iastate.edu"
    NWS_BASE: str = "https://api.weather.gov"
    AWC_BASE: str = "https://aviationweather.gov"
    AUTO_RESOLVE_STATIONS: bool = True
    NWS_USER_AGENT: str = "edgecast (you@example.com)"

    # --- Polymarket ---
    POLY_PRIVATE_KEY: SecretStr | None = None
    POLY_PRIVATE_KEY_FILE: Path | None = None
    POLY_FUNDER_ADDRESS: str = ""
    POLY_SIGNATURE_TYPE: int | None = None
    POLY_CLOB_URL: str = "https://clob.polymarket.com"
    POLY_GAMMA_URL: str = "https://gamma-api.polymarket.com"
    POLY_DATA_URL: str = "https://data-api.polymarket.com"
    POLY_GEOBLOCK_URL: str = "https://polymarket.com/api/geoblock"
    POLY_CHAIN_ID: int = 137
    POLYGON_RPC_URL: str = ""
    WITHDRAW_TO_ADDRESS: str = ""

    # --- App ---
    DATABASE_URL: str = "sqlite:////data/edgecast.db"
    DATA_DIR: Path = Path("/data")
    UI_USERNAME: str = "admin"
    UI_PASSWORD_HASH: SecretStr = SecretStr("")
    SESSION_SECRET: SecretStr = SecretStr("")
    PUBLIC_DOMAIN: str = "localhost"
    TZ: str = "UTC"
    LOG_LEVEL: str = "INFO"
    BACKUP_S3_URI: str = ""
    API_TOKEN: SecretStr = SecretStr("")
    API_CORS_ORIGINS: CsvList = []

    # --- Scanner / manual trading ---
    MANUAL_BANKROLL_USD: float = Field(200, gt=0)
    MANUAL_MAX_STAKE_USD: float = Field(25, gt=0)
    MANUAL_WALLET_ADDRESS: str = ""
    SIGNAL_REALERT_EDGE_DELTA: float = Field(0.03, ge=0, lt=1)
    MIN_SIGNAL_CONFIDENCE: Confidence = Confidence.MED

    # --- Telegram ---
    TELEGRAM_ENABLED: bool = False
    TELEGRAM_BOT_TOKEN: SecretStr | None = None
    TELEGRAM_ALLOWED_USER_IDS: Annotated[list[int], NoDecode] = []
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_NOTIFY_SIGNALS: bool = True
    TELEGRAM_NOTIFY_ORDERS: bool = True
    TELEGRAM_NOTIFY_EXITS: bool = True
    TELEGRAM_NOTIFY_RESOLUTIONS: bool = True
    TELEGRAM_DAILY_SUMMARY_TIME: str = "21:00"
    TELEGRAM_QUIET_HOURS: str = ""

    # ---------------------------------------------------------------- validators
    @field_validator("CITY_ALLOWLIST", "CITY_BLOCKLIST", mode="before")
    @classmethod
    def _split_csv(cls, v: Any) -> list[str]:
        return [c.lower() for c in _csv(v)]

    @field_validator("API_CORS_ORIGINS", mode="before")
    @classmethod
    def _origins(cls, v: Any) -> list[str]:
        return _csv(v)

    @field_validator("API_TOKEN")
    @classmethod
    def _token(cls, v: SecretStr) -> SecretStr:
        if v.get_secret_value() and len(v.get_secret_value()) < 24:
            raise ValueError("API_TOKEN must be at least 24 characters (or empty to disable the API)")
        return v

    @field_validator("TELEGRAM_ALLOWED_USER_IDS", mode="before")
    @classmethod
    def _split_ids(cls, v: Any) -> list[int]:
        try:
            return [int(x) for x in _csv(v)]
        except ValueError as e:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must be comma-separated integers") from e

    @field_validator("MODEL_WEIGHTS", mode="before")
    @classmethod
    def _weights(cls, v: Any) -> dict[str, float]:
        if isinstance(v, dict):
            weights = {str(k): float(x) for k, x in v.items()}
        else:
            weights = {}
            for part in _csv(v):
                name, _, w = part.partition(":")
                weights[name.strip()] = float(w)
        if not weights or any(w < 0 for w in weights.values()) or sum(weights.values()) <= 0:
            raise ValueError("MODEL_WEIGHTS must be name:weight pairs with a positive total")
        unknown = set(weights) - {"ecmwf_ifs", "gfs", "ecmwf_aifs", "icon", "gem"}
        if unknown:
            raise ValueError(f"unknown model families in MODEL_WEIGHTS: {sorted(unknown)}")
        total = sum(weights.values())
        return {k: w / total for k, w in weights.items()}

    @field_validator("TELEGRAM_DAILY_SUMMARY_TIME")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        parse_hhmm(v)
        return v

    @field_validator("TELEGRAM_QUIET_HOURS")
    @classmethod
    def _quiet(cls, v: str) -> str:
        if v:
            parse_quiet_hours(v)
        return v

    @field_validator("POLY_SIGNATURE_TYPE", mode="before")
    @classmethod
    def _sigtype(cls, v: Any) -> int | None:
        if v in (None, ""):
            return None
        iv = int(v)
        if iv not in (0, 1, 2, 3):
            raise ValueError("POLY_SIGNATURE_TYPE must be 0 (EOA), 1 (proxy), 2 (safe) or 3 (1271)")
        return iv

    @field_validator("POLY_PRIVATE_KEY", "TELEGRAM_BOT_TOKEN", mode="before")
    @classmethod
    def _empty_secret(cls, v: Any) -> Any:
        return None if v == "" else v

    @field_validator("POLY_PRIVATE_KEY_FILE", mode="before")
    @classmethod
    def _empty_path(cls, v: Any) -> Any:
        return None if v == "" else v

    @field_validator("POLY_FUNDER_ADDRESS", "MANUAL_WALLET_ADDRESS", "WITHDRAW_TO_ADDRESS")
    @classmethod
    def _address(cls, v: str) -> str:
        if v and not re.fullmatch(r"0x[0-9a-fA-F]{40}", v):
            raise ValueError("must be a 0x-prefixed 20-byte hex address")
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> Settings:
        pairs = [
            ("MAX_STAKE_PER_ORDER_USD", "HARD_MAX_STAKE_PER_ORDER_USD"),
            ("MAX_TOTAL_EXPOSURE_USD", "HARD_MAX_TOTAL_EXPOSURE_USD"),
            ("MAX_DAILY_LOSS_USD", "HARD_MAX_DAILY_LOSS_USD"),
            ("MAX_EXPOSURE_PER_MARKET_USD", "HARD_MAX_TOTAL_EXPOSURE_USD"),
            ("MAX_EXPOSURE_PER_CITY_DAY_USD", "HARD_MAX_TOTAL_EXPOSURE_USD"),
        ]
        for soft, hard in pairs:
            if getattr(self, soft) > getattr(self, hard):
                raise ValueError(f"{soft} ({getattr(self, soft)}) exceeds {hard} ({getattr(self, hard)})")
        if self.ENTRY_MIN_HOURS >= self.ENTRY_MAX_HOURS:
            raise ValueError("ENTRY_MIN_HOURS must be < ENTRY_MAX_HOURS")
        if self.SANITY_MAX_EDGE <= self.MIN_EDGE:
            raise ValueError("SANITY_MAX_EDGE must be > MIN_EDGE")
        if self.POLY_PRIVATE_KEY and self.POLY_PRIVATE_KEY_FILE:
            raise ValueError("set POLY_PRIVATE_KEY or POLY_PRIVATE_KEY_FILE, not both")
        if self.TELEGRAM_ENABLED and (not self.TELEGRAM_BOT_TOKEN or not self.TELEGRAM_ALLOWED_USER_IDS):
            raise ValueError("TELEGRAM_ENABLED requires TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS")
        return self

    # ---------------------------------------------------------------- helpers
    @property
    def live_confirmed(self) -> bool:
        return self.LIVE_CONFIRM == LIVE_CONFIRM_PHRASE

    def private_key(self) -> str | None:
        """Return the signing key, or None. Never log the return value."""
        if self.POLY_PRIVATE_KEY:
            return self.POLY_PRIVATE_KEY.get_secret_value().strip()
        if self.POLY_PRIVATE_KEY_FILE:
            return self.POLY_PRIVATE_KEY_FILE.read_text().strip()
        return None

    @property
    def has_private_key(self) -> bool:
        return bool(self.POLY_PRIVATE_KEY or self.POLY_PRIVATE_KEY_FILE)

    @property
    def kill_file(self) -> Path:
        return self.DATA_DIR / "KILL"


def parse_hhmm(v: str) -> time:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", v.strip())
    if not m:
        raise ValueError(f"expected HH:MM, got {v!r}")
    return time(int(m.group(1)), int(m.group(2)))


def parse_quiet_hours(v: str) -> tuple[time, time]:
    start, sep, end = v.partition("-")
    if not sep:
        raise ValueError(f"expected HH:MM-HH:MM, got {v!r}")
    return parse_hhmm(start), parse_hhmm(end)


def in_quiet_hours(now: time, spec: str) -> bool:
    if not spec:
        return False
    start, end = parse_quiet_hours(spec)
    if start <= end:
        return start <= now < end
    return now >= start or now < end


# ------------------------------------------------------------------ runtime overrides
@dataclass(frozen=True)
class RuntimeKey:
    """A setting that may be changed at runtime from the UI or Telegram."""

    kind: str  # "float" | "bool" | "csv"
    lo: float | None = None
    hi: float | None = None
    ceiling: str | None = None  # name of the env hard ceiling (value may never exceed it)
    risk: bool = False  # risk limits need a confirm step in Telegram


RUNTIME_KEYS: dict[str, RuntimeKey] = {
    "MIN_EDGE": RuntimeKey("float", 0.01, 0.5, risk=True),
    "KELLY_FRACTION": RuntimeKey("float", 0.01, 0.5, risk=True),
    "MAX_STAKE_PER_ORDER_USD": RuntimeKey("float", 0.5, None, "HARD_MAX_STAKE_PER_ORDER_USD", risk=True),
    "MAX_DAILY_LOSS_USD": RuntimeKey("float", 1, None, "HARD_MAX_DAILY_LOSS_USD", risk=True),
    "MAX_TOTAL_EXPOSURE_USD": RuntimeKey("float", 1, None, "HARD_MAX_TOTAL_EXPOSURE_USD", risk=True),
    "MAX_EXPOSURE_PER_MARKET_USD": RuntimeKey("float", 1, None, "HARD_MAX_TOTAL_EXPOSURE_USD", risk=True),
    "MAX_EXPOSURE_PER_CITY_DAY_USD": RuntimeKey("float", 1, None, "HARD_MAX_TOTAL_EXPOSURE_USD", risk=True),
    "ROUND_UP_TO_MIN_ORDER": RuntimeKey("bool", risk=True),
    "CITY_ALLOWLIST": RuntimeKey("csv"),
    "CITY_BLOCKLIST": RuntimeKey("csv"),
    "TELEGRAM_NOTIFY_SIGNALS": RuntimeKey("bool"),
    "TELEGRAM_NOTIFY_ORDERS": RuntimeKey("bool"),
    "TELEGRAM_NOTIFY_EXITS": RuntimeKey("bool"),
    "TELEGRAM_NOTIFY_RESOLUTIONS": RuntimeKey("bool"),
}

SECRET_KEYS = {"POLY_PRIVATE_KEY", "UI_PASSWORD_HASH", "SESSION_SECRET", "TELEGRAM_BOT_TOKEN", "API_TOKEN"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
