"""SQLAlchemy 2 models.

Money convention: every cash amount that feeds P&L is stored as integer micro-USD (1e-6, the collateral's
own precision) so the books reconcile exactly. Prices are floats in [0, 1]; share sizes are floats rounded
to the market's size step.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo on the way back)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


def ts_col(**kw: Any) -> Any:
    return mapped_column(UTCDateTime, **kw)


# --------------------------------------------------------------------------- control plane
class RuntimeState(Base):
    """Key/value store for mode, halt, pause, equity peaks, health timestamps."""

    __tablename__ = "runtime_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = ts_col(default=utcnow, onupdate=utcnow)


class SettingOverride(Base):
    __tablename__ = "setting_override"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)  # {"v": <value>}
    source: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = ts_col(default=utcnow, onupdate=utcnow)


class Command(Base):
    """Instructions from the web/Telegram to the worker, which is the only process that acts on them."""

    __tablename__ = "command"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    result: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = ts_col(default=utcnow)
    done_at: Mapped[datetime | None] = ts_col(nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = ts_col(default=utcnow, index=True)
    source: Mapped[str] = mapped_column(String(16))  # UI | TELEGRAM | WORKER | ENV
    actor: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")


class Heartbeat(Base):
    __tablename__ = "heartbeat"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = ts_col(default=utcnow)
    info: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------- markets
class WeatherEvent(Base):
    """One city/date/variable event (e.g. "Highest temperature in NYC on Oct 7?")."""

    __tablename__ = "weather_event"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # Gamma event id
    slug: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(16), default="weather")  # weather | cpi
    parse_status: Mapped[str] = mapped_column(String(16), index=True)  # PARSED | UNPARSED
    parse_error: Mapped[str] = mapped_column(Text, default="")
    city: Mapped[str] = mapped_column(String(64), default="", index=True)
    icao: Mapped[str] = mapped_column(String(8), default="")
    local_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    variable: Mapped[str] = mapped_column(String(8), default="")  # max | min
    unit: Mapped[str] = mapped_column(String(1), default="")  # F | C
    source: Mapped[str] = mapped_column(String(16), default="")  # WU | NOAA
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    end_date: Mapped[datetime | None] = ts_col(nullable=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = ts_col(default=utcnow, onupdate=utcnow)

    markets: Mapped[list[Market]] = relationship(back_populates="event", cascade="all, delete-orphan")


class Market(Base):
    """One bucket market inside an event, with its YES and NO tokens."""

    __tablename__ = "market"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # condition id
    event_id: Mapped[str] = mapped_column(ForeignKey("weather_event.id"), index=True)
    gamma_id: Mapped[str] = mapped_column(String(32), default="")
    slug: Mapped[str] = mapped_column(String(255), default="")
    question: Mapped[str] = mapped_column(String(512))
    bucket_label: Mapped[str] = mapped_column(String(64))
    bucket_lo: Mapped[float | None] = mapped_column(Float, nullable=True)  # inclusive integer, None=-inf
    bucket_hi: Mapped[float | None] = mapped_column(Float, nullable=True)  # inclusive integer, None=+inf
    token_yes: Mapped[str] = mapped_column(String(100))
    token_no: Mapped[str] = mapped_column(String(100))
    neg_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    tick_size: Mapped[float] = mapped_column(Float, default=0.01)
    min_order_size: Mapped[float] = mapped_column(Float, default=5)
    fees_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    fee_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_exponent: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_checked_at: Mapped[datetime | None] = ts_col(nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    accepting_orders: Mapped[bool] = mapped_column(Boolean, default=True)
    resolved_yes: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    end_date: Mapped[datetime | None] = ts_col(nullable=True)
    updated_at: Mapped[datetime] = ts_col(default=utcnow, onupdate=utcnow)

    event: Mapped[WeatherEvent] = relationship(back_populates="markets")

    @property
    def url(self) -> str:
        return f"https://polymarket.com/event/{self.event.slug}"


# --------------------------------------------------------------------------- forecasts & model
class ForecastSnapshot(Base):
    """Reduced ensemble members (local-day max or min) for one station/day/variable/model family."""

    __tablename__ = "forecast_snapshot"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    icao: Mapped[str] = mapped_column(String(8), index=True)
    local_date: Mapped[date] = mapped_column(Date)
    variable: Mapped[str] = mapped_column(String(8))
    family: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(48))
    unit: Mapped[str] = mapped_column(String(1))
    members: Mapped[list[Any]] = mapped_column(JSON)
    run_init: Mapped[datetime | None] = ts_col(nullable=True)
    fetched_at: Mapped[datetime] = ts_col(default=utcnow, index=True)

    __table_args__ = (Index("ix_fc_lookup", "icao", "local_date", "variable", "family", "fetched_at"),)


class StationBias(Base):
    __tablename__ = "station_bias"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    icao: Mapped[str] = mapped_column(String(8))
    family: Mapped[str] = mapped_column(String(16))
    variable: Mapped[str] = mapped_column(String(8))
    lead_day: Mapped[int] = mapped_column(Integer)
    unit: Mapped[str] = mapped_column(String(1))
    mu: Mapped[float] = mapped_column(Float)  # mean(forecast - observed)
    sigma: Mapped[float] = mapped_column(Float)  # sd of error
    n: Mapped[int] = mapped_column(Integer)
    fitted_at: Mapped[datetime] = ts_col(default=utcnow)

    __table_args__ = (UniqueConstraint("icao", "family", "variable", "lead_day", name="uq_bias"),)


class Prediction(Base):
    """Every model probability we produced, scored after resolution for calibration."""

    __tablename__ = "prediction"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("market.id"), index=True)
    city: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = ts_col(default=utcnow)
    lead_hours: Mapped[float] = mapped_column(Float)
    p_model: Mapped[float] = mapped_column(Float)
    p_by_family: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    market_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1 yes / 0 no


# --------------------------------------------------------------------------- signals
class Signal(Base):
    __tablename__ = "signal"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("market.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(3))  # YES | NO (the token to buy)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)  # ACTIVE|EXPIRED|RESOLVED
    created_at: Mapped[datetime] = ts_col(default=utcnow, index=True)
    updated_at: Mapped[datetime] = ts_col(default=utcnow, onupdate=utcnow)
    p_model: Mapped[float] = mapped_column(Float)
    ask: Mapped[float] = mapped_column(Float)
    fee_per_share: Mapped[float] = mapped_column(Float)
    edge: Mapped[float] = mapped_column(Float)
    max_limit_price: Mapped[float] = mapped_column(Float)
    suggested_stake_usd: Mapped[float] = mapped_column(Float)
    bot_stake_usd: Mapped[float] = mapped_column(Float, default=0)
    confidence: Mapped[str] = mapped_column(String(4))
    liquidity_usd: Mapped[float] = mapped_column(Float, default=0)
    forecast_run: Mapped[datetime | None] = ts_col(nullable=True)
    resolves_at: Mapped[datetime | None] = ts_col(nullable=True)
    last_alert_edge: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_alert_at: Mapped[datetime | None] = ts_col(nullable=True)
    action: Mapped[str] = mapped_column(String(255), default="")  # what the bot did / why it skipped
    expire_reason: Mapped[str] = mapped_column(String(128), default="")
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    won: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    roi_at_limit: Mapped[float | None] = mapped_column(Float, nullable=True)

    market: Mapped[Market] = relationship()


# --------------------------------------------------------------------------- execution & books
class OrderIntent(Base):
    """Stored before any order is sent. The id is our idempotency key."""

    __tablename__ = "order_intent"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    mode: Mapped[str] = mapped_column(String(8), index=True)  # PAPER | LIVE
    market_id: Mapped[str] = mapped_column(ForeignKey("market.id"), index=True)
    token_id: Mapped[str] = mapped_column(String(100))
    outcome: Mapped[str] = mapped_column(String(3))
    side: Mapped[str] = mapped_column(String(4))  # BUY | SELL
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    # PENDING (stored, not yet acknowledged) | OPEN | PARTIAL | FILLED | CANCELLED | FAILED | REJECTED
    exchange_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    signed_salt: Mapped[str | None] = mapped_column(String(80), nullable=True)
    filled_size: Mapped[float] = mapped_column(Float, default=0)
    reason: Mapped[str] = mapped_column(String(255), default="")  # entry / exit reason
    error: Mapped[str] = mapped_column(Text, default="")
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signal.id"), nullable=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("position.id"), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = ts_col(default=utcnow, index=True)
    updated_at: Mapped[datetime] = ts_col(default=utcnow, onupdate=utcnow)

    market: Mapped[Market] = relationship()


class Fill(Base):
    __tablename__ = "fill"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("order_intent.id"), index=True)
    exchange_trade_id: Mapped[str] = mapped_column(String(100), unique=True)
    ts: Mapped[datetime] = ts_col(default=utcnow)
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    fee_micros: Mapped[int] = mapped_column(BigInteger, default=0)


class Position(Base):
    __tablename__ = "position"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track: Mapped[str] = mapped_column(String(8), index=True)  # BOT | MANUAL
    mode: Mapped[str] = mapped_column(String(8), index=True)  # PAPER | LIVE | MANUAL
    market_id: Mapped[str] = mapped_column(ForeignKey("market.id"), index=True)
    token_id: Mapped[str] = mapped_column(String(100))
    outcome: Mapped[str] = mapped_column(String(3))
    size: Mapped[float] = mapped_column(Float, default=0)
    cost_micros: Mapped[int] = mapped_column(BigInteger, default=0)  # remaining cost basis incl. fees
    realized_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(10), default="OPEN", index=True)  # OPEN|CLOSED|RESOLVED
    opened_at: Mapped[datetime] = ts_col(default=utcnow)
    closed_at: Mapped[datetime | None] = ts_col(nullable=True)
    entry_reason: Mapped[str] = mapped_column(String(255), default="")
    exit_reason: Mapped[str] = mapped_column(String(255), default="")
    exit_signal: Mapped[str] = mapped_column(String(32), default="")  # "" | TAKE_PROFIT | EDGE_REVERSAL
    exit_signal_detail: Mapped[str] = mapped_column(String(255), default="")
    last_p_model: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_mark: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_eval_at: Mapped[datetime | None] = ts_col(nullable=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signal.id"), nullable=True)
    external_ref: Mapped[str] = mapped_column(String(120), default="")  # data-api import key
    redeemed: Mapped[bool] = mapped_column(Boolean, default=False)

    market: Mapped[Market] = relationship()

    @property
    def avg_price(self) -> float:
        return (self.cost_micros / 1e6) / self.size if self.size > 0 else 0.0


class LedgerEntry(Base):
    """Cash movements per book. Book = PAPER | LIVE | MANUAL. Sum = cash balance."""

    __tablename__ = "ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book: Mapped[str] = mapped_column(String(8), index=True)
    ts: Mapped[datetime] = ts_col(default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(12))  # DEPOSIT | BUY | SELL | FEE | REDEEM | ADJUST
    amount_micros: Mapped[int] = mapped_column(BigInteger)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("position.id"), nullable=True)
    pnl_micros: Mapped[int] = mapped_column(BigInteger, default=0)  # P&L realised by this entry
    ref: Mapped[str] = mapped_column(String(120), default="")


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshot"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book: Mapped[str] = mapped_column(String(8), index=True)
    ts: Mapped[datetime] = ts_col(default=utcnow, index=True)
    cash: Mapped[float] = mapped_column(Float)
    exposure: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)


class BacktestRun(Base):
    __tablename__ = "backtest_run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = ts_col(default=utcnow)
    report_path: Mapped[str] = mapped_column(String(255))
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Outbox(Base):
    """Alerts waiting to be delivered (Telegram). Any process may write; the worker's bot drains it."""

    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = ts_col(default=utcnow, index=True)
    category: Mapped[str] = mapped_column(String(16))  # signal|order|exit|resolution|risk|health|summary|info
    text: Mapped[str] = mapped_column(Text)
    buttons: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [[label, callback_data], ...]
    sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sent_at: Mapped[datetime | None] = ts_col(nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")


class ResolvedStation(Base):
    """A resolution station not in the curated stations.yaml, looked up automatically from official metadata."""

    __tablename__ = "resolved_station"
    icao: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    city: Mapped[str] = mapped_column(String(64))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    tz: Mapped[str] = mapped_column(String(64))
    country: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(64), default="aviationweather.gov + open-meteo tz")
    resolved_at: Mapped[datetime] = ts_col(default=utcnow)
