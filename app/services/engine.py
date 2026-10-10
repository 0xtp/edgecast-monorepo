"""The trading engine: forecasts -> probabilities -> signals -> (optionally) orders, plus exits and resolutions.

Every job here is synchronous and runs in the worker's scheduler threads. Jobs that touch orders hold
`self.exec_lock` so placement, exits, cancels and reconciliation never interleave.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Confidence, Mode, Settings
from app.db.models import (
    Command,
    EquitySnapshot,
    ForecastSnapshot,
    Heartbeat,
    Market,
    OrderIntent,
    Position,
    Prediction,
    Signal,
    StationBias,
    WeatherEvent,
)
from app.execution.base import Executor, place_order
from app.execution.fills import fee_per_share
from app.execution.paper import PaperExecutor
from app.forecast import bias as biasmod
from app.forecast.calibrate import brier, should_disable
from app.forecast.nws import Nws
from app.forecast.observations import Iem, daily_extremes
from app.forecast.openmeteo import FAMILIES, OpenMeteo
from app.http import API_HEALTH
from app.markets import registry
from app.markets.parser import load_stations
from app.models.weather_prob import BucketProbs, FamilyInput, bucket_probabilities, lead_day_for
from app.polymarket.clob import Clob, OrderBook
from app.polymarket.data_api import DataApi
from app.polymarket.gamma import Gamma
from app.polymarket.geoblock import check_geoblock
from app.risk import OrderRequest, RiskLimits, breach, check, drawdown_fraction_of_limit
from app.services import books
from app.services.state import (
    Effective,
    alert,
    audit,
    current_mode,
    effective,
    get_state,
    halt,
    is_halted,
    is_paused,
    set_state,
)
from app.strategy.edge import Book, EdgeParams, FeeModel, Level, Opportunity, best_opportunity, family_agreement
from app.strategy.exits import ExitKind, decide_exit
from app.strategy.sizing import size_order

log = logging.getLogger(__name__)

RUN_MAX_AGE = timedelta(hours=30)  # a model run older than this means we missed run cycles
PREDICTION_EVERY = timedelta(minutes=55)
NWS_SIGMA_CONFLICT = 3.0


@dataclass
class Clients:
    gamma: Gamma
    clob: Clob
    data_api: DataApi
    openmeteo: OpenMeteo
    iem: Iem
    nws: Nws

    @classmethod
    def from_settings(cls, s: Settings) -> Clients:
        return cls(
            gamma=Gamma(s.POLY_GAMMA_URL),
            clob=Clob(s.POLY_CLOB_URL),
            data_api=DataApi(s.POLY_DATA_URL),
            openmeteo=OpenMeteo(s.OPEN_METEO_BASE, s.OPEN_METEO_PREVIOUS_RUNS_BASE, s.OPEN_METEO_SINGLE_RUNS_BASE),
            iem=Iem(s.IEM_BASE),
            nws=Nws(s.NWS_BASE, s.NWS_USER_AGENT),
        )


@dataclass
class EventModel:
    probs: BucketProbs | None
    lead_hours: float
    forecast_run: datetime | None
    latest_fetch: datetime | None
    stale_reason: str = ""
    nws_conflict: bool = False
    markets: list[Market] = field(default_factory=list)


def day_end_utc(local_date: date, tz: str) -> datetime:
    return datetime.combine(local_date + timedelta(days=1), time(0), ZoneInfo(tz)).astimezone(UTC)


def to_book(ob: OrderBook | None) -> Book:
    if ob is None:
        return Book((), ())
    return Book([Level(lv.price, lv.size) for lv in ob.bids], [Level(lv.price, lv.size) for lv in ob.asks])


def compute_confidence(
    p_by_family: dict[str, float], p: float, calib_brier: float | None, max_brier: float, nws_conflict: bool
) -> Confidence:
    if len(p_by_family) < 2 or nws_conflict:
        return Confidence.LOW
    spread = max(abs(v - p) for v in p_by_family.values())
    if spread <= 0.08 and calib_brier is not None and calib_brier <= 0.8 * max_brier:
        return Confidence.HIGH
    if spread <= 0.15:
        return Confidence.MED
    return Confidence.LOW


class Engine:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        settings: Settings,
        clients: Clients,
        live_client_factory: Any = None,
    ):
        self.sessions = sessions
        self.settings = settings
        self.c = clients
        self.exec_lock = threading.RLock()
        self._book_cache: dict[str, tuple[datetime, OrderBook]] = {}
        self.paper = PaperExecutor(self.book, max_depth_pct=100.0)
        self._live: Executor | None = None
        self._live_client: Any = None
        self._live_client_factory = live_client_factory

    # ------------------------------------------------------------------ helpers
    def book(self, token_id: str, max_age_s: float = 5.0) -> OrderBook:
        now = datetime.now(UTC)
        hit = self._book_cache.get(token_id)
        if hit and (now - hit[0]).total_seconds() < max_age_s:
            return hit[1]
        ob = self.c.clob.book(token_id)
        self._book_cache[token_id] = (now, ob)
        return ob

    def books(self, token_ids: list[str]) -> dict[str, OrderBook]:
        if not token_ids:
            return {}
        out = self.c.clob.books(token_ids)
        now = datetime.now(UTC)
        for t, ob in out.items():
            self._book_cache[t] = (now, ob)
        return out

    def live_client(self, session: Session) -> Any:
        if self._live_client is None:
            if self._live_client_factory is None:
                from app.execution.live import SdkTradingClient

                self._live_client = SdkTradingClient(self.settings, session)
            else:
                self._live_client = self._live_client_factory(session)
        return self._live_client

    def executor(self, session: Session, mode: Mode) -> Executor | None:
        if mode == Mode.PAPER:
            return self.paper
        if mode == Mode.LIVE:
            if self._live is None:
                from app.execution.live import LiveExecutor

                self._live = LiveExecutor(self.live_client(session))
            return self._live
        return None

    def limits(self, eff: Effective) -> RiskLimits:
        return RiskLimits(
            max_stake_per_order=float(eff.MAX_STAKE_PER_ORDER_USD),
            max_exposure_per_market=float(eff.MAX_EXPOSURE_PER_MARKET_USD),
            max_exposure_per_city_day=float(eff.MAX_EXPOSURE_PER_CITY_DAY_USD),
            max_total_exposure=float(eff.MAX_TOTAL_EXPOSURE_USD),
            max_daily_loss=float(eff.MAX_DAILY_LOSS_USD),
            max_drawdown_pct=float(eff.MAX_DRAWDOWN_PCT),
        )

    def heartbeat(self, name: str = "worker", **info: Any) -> None:
        with self.sessions() as s:
            hb = s.get(Heartbeat, name) or Heartbeat(name=name)
            hb.ts, hb.info = datetime.now(UTC), {**(hb.info or {}), **info}
            s.merge(hb)
            health = {
                k: {kk: (vv.isoformat() if isinstance(vv, datetime) else vv) for kk, vv in v.items()}
                for k, v in API_HEALTH.items()
            }
            set_state(s, "api_health", health)
            s.commit()

    def city_allowed(self, eff: Effective, session: Session, city: str) -> str:
        allow = [c.lower() for c in (eff.CITY_ALLOWLIST or [])]
        block = [c.lower() for c in (eff.CITY_BLOCKLIST or [])]
        disabled = get_state(session, "disabled_cities", []) or []
        if allow and city not in allow:
            return "city not in allowlist"
        if city in block:
            return "city blocklisted"
        if city in disabled:
            return "city auto-disabled (calibration)"
        return ""

    # ------------------------------------------------------------------ markets
    def sync_markets(self) -> tuple[int, int]:
        if not self.settings.ENABLE_WEATHER:
            return 0, 0
        live = self.c.gamma.weather_events(closed=False)
        recent_closed = self.c.gamma.weather_events(closed=True, max_pages=2)
        with self.sessions() as s:
            parsed, unparsed = registry.sync(s, live + recent_closed)
            registry.refresh_fees(s, self.c.clob)
            set_state(s, "last_market_sync", datetime.now(UTC).isoformat())
            s.commit()
        log.info("market sync: %d parsed, %d unparsed", parsed, unparsed)
        return parsed, unparsed

    def candidate_events(self, s: Session, now: datetime, horizon_h: float) -> list[WeatherEvent]:
        events = s.scalars(
            select(WeatherEvent).where(WeatherEvent.parse_status == "PARSED", WeatherEvent.closed.is_(False))
        ).all()
        stations = load_stations()
        out = []
        for ev in events:
            st = stations.get(ev.icao)
            if st is None or ev.local_date is None:
                continue
            end = day_end_utc(ev.local_date, st.tz)
            if end <= now or (end - now).total_seconds() / 3600 > horizon_h:
                continue
            out.append(ev)
        return out

    # ------------------------------------------------------------------ forecasts
    def refresh_forecasts(self) -> int:
        now = datetime.now(UTC)
        stations = load_stations()
        with self.sessions() as s:
            eff = effective(s, self.settings)
            events = self.candidate_events(s, now, float(eff.ENTRY_MAX_HOURS) + 24)
            held = s.scalars(select(Position).where(Position.status == "OPEN")).all()
            for pos in held:
                ev = pos.market.event
                if ev not in events and not ev.closed and ev.parse_status == "PARSED":
                    events.append(ev)
            needs: dict[tuple[str, str], set[tuple[date, str]]] = {}
            for ev in events:
                assert ev.local_date is not None
                needs.setdefault((ev.icao, ev.unit), set()).add((ev.local_date, ev.variable))
            families = [f for f, w in eff.MODEL_WEIGHTS.items() if w > 0]
        n = 0
        for (icao, unit), wanted in needs.items():
            st = stations[icao]
            days_ahead = max((d - now.astimezone(ZoneInfo(st.tz)).date()).days for d, _ in wanted) + 2
            for fam in families:
                try:
                    fc = self.c.openmeteo.ensemble(st.lat, st.lon, st.tz, fam, unit, forecast_days=min(16, days_ahead))
                except Exception as e:
                    log.warning("ensemble %s %s failed: %s", fam, icao, e)
                    continue
                with self.sessions() as s:
                    for d, var in wanted:
                        members = fc.daily(d, var)
                        if len(members) >= 2:
                            s.add(
                                ForecastSnapshot(
                                    icao=icao,
                                    local_date=d,
                                    variable=var,
                                    family=fam,
                                    model=fc.model,
                                    unit=unit,
                                    members=members,
                                    run_init=fc.run_init,
                                    fetched_at=fc.fetched_at,
                                )
                            )
                            n += 1
                    s.commit()
            if st.is_us:
                try:
                    nws = self.c.nws.daily_high_low(st.lat, st.lon)
                except Exception as e:
                    log.info("NWS %s failed: %s", icao, e)
                    nws = {}
                with self.sessions() as s:
                    for d, var in wanted:
                        v = nws.get(d, {}).get(var)
                        if v is not None:
                            val = v if unit == "F" else (v - 32) * 5 / 9
                            s.add(
                                ForecastSnapshot(
                                    icao=icao,
                                    local_date=d,
                                    variable=var,
                                    family="nws",
                                    model="nws",
                                    unit=unit,
                                    members=[val],
                                    run_init=None,
                                )
                            )
                    s.commit()
        with self.sessions() as s:
            set_state(s, "last_forecast_pull", now.isoformat())
            s.commit()
        return n

    def latest_snapshot(self, s: Session, ev: WeatherEvent, family: str) -> ForecastSnapshot | None:
        return s.scalar(
            select(ForecastSnapshot)
            .where(
                ForecastSnapshot.icao == ev.icao,
                ForecastSnapshot.local_date == ev.local_date,
                ForecastSnapshot.variable == ev.variable,
                ForecastSnapshot.family == family,
            )
            .order_by(ForecastSnapshot.fetched_at.desc())
            .limit(1)
        )

    def model_event(self, s: Session, ev: WeatherEvent, eff: Effective, now: datetime) -> EventModel:
        st = load_stations()[ev.icao]
        assert ev.local_date is not None
        lead_hours = (day_end_utc(ev.local_date, st.tz) - now).total_seconds() / 3600
        markets = sorted(ev.markets, key=lambda m: -math.inf if m.bucket_lo is None else m.bucket_lo)
        result = EventModel(None, lead_hours, None, None, markets=markets)
        lead_day = lead_day_for(lead_hours)
        fams: dict[str, FamilyInput] = {}
        runs: list[datetime] = []
        fetches: list[datetime] = []
        stale: list[str] = []
        for fam, w in eff.MODEL_WEIGHTS.items():
            if w <= 0:
                continue
            snap = self.latest_snapshot(s, ev, fam)
            if snap is None:
                stale.append(f"{fam}: no forecast")
                continue
            if now - snap.fetched_at > timedelta(minutes=int(eff.MAX_FORECAST_AGE_MIN)):
                stale.append(f"{fam}: forecast pulled {int((now - snap.fetched_at).total_seconds() // 60)} min ago")
                continue
            if snap.run_init and now - snap.run_init > RUN_MAX_AGE:
                stale.append(f"{fam}: model run {snap.run_init:%Y-%m-%d %H}Z is old")
                continue
            b = s.scalar(
                select(StationBias).where(
                    StationBias.icao == ev.icao,
                    StationBias.family == fam,
                    StationBias.variable == ev.variable,
                    StationBias.lead_day == lead_day,
                )
            )
            bias = biasmod.BiasFit(b.mu, b.sigma, b.n) if b and b.unit == ev.unit else None
            fams[fam] = FamilyInput(snap.members, bias)
            fetches.append(snap.fetched_at)
            if snap.run_init:
                runs.append(snap.run_init)
        if not fams:
            result.stale_reason = "; ".join(stale) or "no forecast"
            return result
        buckets = [
            (
                -math.inf if m.bucket_lo is None else m.bucket_lo - 0.5,
                math.inf if m.bucket_hi is None else m.bucket_hi + 0.5,
            )
            for m in markets
        ]
        try:
            probs = bucket_probabilities(fams, eff.MODEL_WEIGHTS, buckets, ev.unit, lead_day)
        except (ValueError, AssertionError) as e:
            result.stale_reason = f"model error: {e}"
            return result
        result.probs = probs
        result.forecast_run = min(runs) if runs else None
        result.latest_fetch = max(fetches)
        if stale:
            result.stale_reason = ""  # some families are usable; agreement check handles single-family
        nws = self.latest_snapshot(s, ev, "nws")
        if nws and probs.sd > 0 and now - nws.fetched_at < timedelta(hours=12):
            result.nws_conflict = abs(nws.members[0] - probs.mean) > NWS_SIGMA_CONFLICT * probs.sd
        return result

    def city_brier(self, s: Session, city: str, days: int = 30) -> tuple[float | None, int]:
        since = datetime.now(UTC) - timedelta(days=days)
        rows = s.execute(
            select(Prediction.p_model, Prediction.outcome).where(
                Prediction.city == city, Prediction.outcome.is_not(None), Prediction.ts >= since
            )
        ).all()
        return brier([(p, int(o)) for p, o in rows])

    # ------------------------------------------------------------------ strategy
    def run_strategy(self) -> dict[str, int]:
        stats = {"events": 0, "signals": 0, "orders": 0}
        now = datetime.now(UTC)
        with self.exec_lock, self.sessions() as s:
            eff = effective(s, self.settings)
            mode = current_mode(s, self.settings)
            book_name = mode.value if mode != Mode.SCAN else "PAPER"
            halted, _ = is_halted(s)
            geo = bool(get_state(s, "geoblocked", True))
            if mode != Mode.SCAN:
                rstate, _snap = books.risk_state(
                    s,
                    book_name,
                    tz=self.settings.TZ,
                    halted=halted,
                    paused=is_paused(s),
                    geoblocked=geo and mode == Mode.LIVE,
                )
                reason = breach(rstate, self.limits(eff))
                if reason and not halted:
                    halt(s, reason, "WORKER")
                    s.commit()
            params = EdgeParams(
                min_edge=float(eff.MIN_EDGE),
                sanity_max_edge=float(eff.SANITY_MAX_EDGE),
                max_spread=float(eff.MAX_SPREAD_CENTS) / 100,
                max_slippage=float(eff.MAX_SLIPPAGE_CENTS) / 100,
                min_liquidity_usd=float(eff.MIN_LIQUIDITY_USD),
                tick=0.01,
            )
            events = self.candidate_events(s, now, float(eff.ENTRY_MAX_HOURS) + 24)
            active_ids: set[int] = set()
            for ev in events:
                stats["events"] += 1
                model = self.model_event(s, ev, eff, now)
                gate = self.city_allowed(eff, s, ev.city)
                if not gate:
                    if model.lead_hours < float(eff.ENTRY_MIN_HOURS):
                        gate = f"inside {eff.ENTRY_MIN_HOURS:g}h of resolution"
                    elif model.lead_hours > float(eff.ENTRY_MAX_HOURS):
                        gate = f"more than {eff.ENTRY_MAX_HOURS:g}h out"
                if model.probs is None:
                    gate = gate or f"forecast unavailable: {model.stale_reason}"
                tokens = [t for m in model.markets for t in (m.token_yes, m.token_no)]
                try:
                    obs = self.books(tokens) if model.probs is not None and not gate else {}
                except Exception as e:
                    log.warning("books fetch failed for %s: %s", ev.slug, e)
                    obs, gate = {}, gate or "order book unavailable"
                calib, _ = self.city_brier(s, ev.city)
                for i, m in enumerate(model.markets):
                    sig_id = self._evaluate_market(s, eff, mode, ev, m, i, model, obs, params, gate, calib, now, stats)
                    if sig_id:
                        active_ids.add(sig_id)
            # Anything still ACTIVE that we did not touch has left the window/universe.
            for sig in s.scalars(select(Signal).where(Signal.status == "ACTIVE")):
                if sig.id not in active_ids:
                    sig.status, sig.expire_reason = "EXPIRED", sig.expire_reason or "entry window closed"
            set_state(s, "last_strategy_run", now.isoformat())
            s.commit()
        return stats

    def _evaluate_market(
        self,
        s: Session,
        eff: Effective,
        mode: Mode,
        ev: WeatherEvent,
        m: Market,
        i: int,
        model: EventModel,
        obs: dict[str, OrderBook],
        params: EdgeParams,
        gate: str,
        calib: float | None,
        now: datetime,
        stats: dict[str, int],
    ) -> int | None:
        active = s.scalar(select(Signal).where(Signal.market_id == m.id, Signal.status == "ACTIVE"))
        reason = gate
        if not reason and (m.closed or not m.accepting_orders):
            reason = "market not accepting orders"
        if not reason and (m.fee_rate is None or m.fee_exponent is None):
            reason = "fee rate unknown"
        if (
            not reason
            and m.end_date
            and (m.end_date - now).total_seconds() / 3600 < float(eff.NO_NEW_ENTRY_HOURS_BEFORE_CLOSE)
        ):
            reason = "too close to market close"
        if model.probs is None or reason:
            if active:
                active.status, active.expire_reason = "EXPIRED", reason or model.stale_reason
            return None
        assert m.fee_rate is not None and m.fee_exponent is not None
        fee = FeeModel(m.fee_rate, m.fee_exponent)
        p_yes = model.probs.probs[i]
        pf_yes = {f: v[i] for f, v in model.probs.by_family.items()}
        params_m = EdgeParams(
            params.min_edge,
            params.sanity_max_edge,
            params.max_spread,
            params.max_slippage,
            params.min_liquidity_usd,
            m.tick_size or 0.01,
        )
        best, _other = best_opportunity(
            p_yes, to_book(obs.get(m.token_yes)), to_book(obs.get(m.token_no)), fee, params_m
        )
        self._record_prediction(s, m, ev.city, p_yes, pf_yes, model, obs.get(m.token_yes), now)
        pf_out = pf_yes if best.outcome == "YES" else {f: 1 - v for f, v in pf_yes.items()}
        agree = best.ask is not None and family_agreement(pf_out, best.ask, fee, params.min_edge)
        why_not = best.skip_reason or ("" if agree else "edge driven by a single model family")
        if why_not:
            if active:
                active.status, active.expire_reason = "EXPIRED", why_not
            return None
        conf = compute_confidence(pf_out, best.p, calib, float(eff.MAX_BRIER), model.nws_conflict)
        flipped = False
        if active and active.outcome != best.outcome:
            active.status, active.expire_reason = "EXPIRED", "direction flipped"
            active, flipped = None, True
        is_new = active is None
        if active is None:
            active = Signal(
                market_id=m.id,
                outcome=best.outcome,
                p_model=best.p,
                ask=best.ask or 0,
                fee_per_share=0,
                edge=best.edge,
                max_limit_price=best.max_limit_price or 0,
                suggested_stake_usd=0,
                confidence=conf.value,
            )
            s.add(active)
        assert best.ask is not None and best.limit_price is not None
        manual = size_order(
            p=best.p,
            price=best.limit_price,
            fee_per_share=fee.per_share(best.limit_price),
            bankroll=float(eff.MANUAL_BANKROLL_USD),
            kelly_mult=float(eff.KELLY_FRACTION),
            max_stake_usd=float(eff.MANUAL_MAX_STAKE_USD),
            depth_shares=best.depth_shares,
            max_depth_pct=float(eff.MAX_BOOK_DEPTH_PCT),
            min_order_size=m.min_order_size,
        )
        active.p_model, active.ask, active.fee_per_share = best.p, best.ask, best.fee_at_ask
        active.edge, active.max_limit_price = best.edge, best.max_limit_price or best.limit_price
        active.suggested_stake_usd = manual.stake_usd
        active.confidence, active.liquidity_usd = conf.value, best.liquidity_usd
        active.forecast_run, active.resolves_at = model.forecast_run, m.end_date
        s.flush()
        stats["signals"] += 1
        if mode == Mode.SCAN:
            active.action = "signal only (SCAN)"
        else:
            self._maybe_enter(s, eff, mode, ev, m, best, fee, active)
            if active.action.startswith(mode.value):
                stats["orders"] += 1
        min_conf = Confidence(eff.MIN_SIGNAL_CONFIDENCE)
        delta = float(eff.SIGNAL_REALERT_EDGE_DELTA)
        changed = active.last_alert_edge is None or abs(active.edge - active.last_alert_edge) > delta
        if conf.rank >= min_conf.rank and not active.ignored and (is_new or flipped or changed):
            active.last_alert_edge, active.last_alert_at = active.edge, now
            alert(
                s,
                "signal",
                format_signal(active, m, ev, model.lead_hours),
                [["✅ Took it", f"took:{active.id}"], ["🙈 Ignore", f"ignore:{active.id}"]],
            )
        return active.id

    def _record_prediction(
        self,
        s: Session,
        m: Market,
        city: str,
        p_yes: float,
        pf: dict[str, float],
        model: EventModel,
        ob: OrderBook | None,
        now: datetime,
    ) -> None:
        last = s.scalar(
            select(Prediction.ts).where(Prediction.market_id == m.id).order_by(Prediction.ts.desc()).limit(1)
        )
        if last and now - last < PREDICTION_EVERY:
            return
        s.add(
            Prediction(
                market_id=m.id,
                city=city,
                ts=now,
                lead_hours=model.lead_hours,
                p_model=p_yes,
                p_by_family=pf,
                market_ask=ob.best_ask if ob else None,
            )
        )

    def _maybe_enter(
        self,
        s: Session,
        eff: Effective,
        mode: Mode,
        ev: WeatherEvent,
        m: Market,
        opp: Opportunity,
        fee: FeeModel,
        sig: Signal,
    ) -> None:
        book_name = mode.value
        held = s.scalar(
            select(Position.id).where(Position.mode == book_name, Position.market_id == m.id, Position.status == "OPEN")
        )
        working = s.scalar(
            select(OrderIntent.id).where(
                OrderIntent.mode == book_name,
                OrderIntent.market_id == m.id,
                OrderIntent.side == "BUY",
                OrderIntent.status.in_(("PENDING", "OPEN", "PARTIAL")),
            )
        )
        if held or working:
            sig.action = f"{book_name}: already {'holding' if held else 'working an order'}"
            return
        halted, _why = is_halted(s)
        geo = bool(get_state(s, "geoblocked", True))
        rstate, snap = books.risk_state(
            s, book_name, tz=self.settings.TZ, halted=halted, paused=is_paused(s), geoblocked=geo and mode == Mode.LIVE
        )
        assert opp.limit_price is not None
        fps = fee.per_share(opp.limit_price)
        sized = size_order(
            p=opp.p,
            price=opp.limit_price,
            fee_per_share=fps,
            bankroll=max(snap.equity, 0.0),
            kelly_mult=float(eff.KELLY_FRACTION),
            max_stake_usd=float(eff.MAX_STAKE_PER_ORDER_USD),
            depth_shares=opp.depth_shares,
            max_depth_pct=float(eff.MAX_BOOK_DEPTH_PCT),
            min_order_size=m.min_order_size,
        )
        if sized.shares <= 0:
            sig.action = f"skipped: {sized.reason}"
            return
        decision = check(
            OrderRequest(
                market_id=m.id,
                city_day=books.city_day_key(ev),
                side="BUY",
                price=opp.limit_price,
                size=sized.shares,
                fee_per_share=fps,
                min_size=m.min_order_size,
            ),
            rstate,
            self.limits(eff),
        )
        if not decision.approved:
            sig.action = f"skipped: {decision.reason}"
            if decision.reason.startswith("cap reached"):
                alert(
                    s,
                    "risk",
                    f"🧱 Risk cap hit ({decision.reason}) — {m.bucket_label} · {ev.title}",
                    dedupe_key=f"cap:{decision.reason}",
                )
            return
        ex = self.executor(s, mode)
        assert ex is not None
        sig.bot_stake_usd = round(decision.size * (opp.limit_price + fps), 2)
        sig.action = f"{book_name} BUY {opp.outcome} {decision.size:g} @ {opp.limit_price:.2f}"
        s.commit()
        place_order(
            self.sessions,
            ex,
            market=m,
            outcome=opp.outcome,
            side="BUY",
            price=opp.limit_price,
            size=decision.size,
            reason=f"entry: edge {100 * opp.edge:.1f}% p={100 * opp.p:.0f}%",
            signal_id=sig.id,
        )
        s.expire_all()

    # ------------------------------------------------------------------ execution upkeep
    def sync_orders(self) -> int:
        with self.exec_lock, self.sessions() as s:
            mode = current_mode(s, self.settings)
            n = self.paper.sync(s)
            if mode == Mode.LIVE or s.scalar(
                select(OrderIntent.id).where(
                    OrderIntent.mode == "LIVE", OrderIntent.status.in_(("PENDING", "OPEN", "PARTIAL"))
                )
            ):
                ex = self.executor(s, Mode.LIVE)
                if ex is not None:
                    n += ex.sync(s)
            s.commit()
        return n

    def reconcile_live(self) -> None:
        from app.execution.reconcile import reconcile

        with self.exec_lock, self.sessions() as s:
            rep = reconcile(s, self.live_client(s))
            if rep.adopted or rep.failed or rep.cancelled_unknown:
                audit(
                    s,
                    "WORKER",
                    "RECONCILE",
                    f"adopted={rep.adopted} failed={rep.failed} "
                    f"cancelled_unknown={rep.cancelled_unknown} fills={rep.fills}",
                )
            s.commit()

    # ------------------------------------------------------------------ exits
    def run_exits(self) -> int:
        now = datetime.now(UTC)
        n = 0
        with self.exec_lock, self.sessions() as s:
            eff = effective(s, self.settings)
            mode = current_mode(s, self.settings)
            books_to_eval = ["MANUAL"] + ([mode.value] if mode != Mode.SCAN else [])
            positions = [p for b in books_to_eval for p in books.open_positions(s, b)]
            cache: dict[str, EventModel] = {}
            for pos in positions:
                ev = pos.market.event
                if ev.closed:
                    continue
                if ev.id not in cache:
                    cache[ev.id] = self.model_event(s, ev, eff, now)
                model = cache[ev.id]
                try:
                    ob = self.book(pos.token_id, max_age_s=30)
                except Exception as e:
                    log.warning("exit eval: book failed %s: %s", pos.token_id, e)
                    continue
                if ob.best_bid is not None:
                    pos.last_mark = ob.best_bid
                if model.probs is None:
                    continue
                idx = next(i for i, mk in enumerate(model.markets) if mk.id == pos.market_id)
                p_yes = model.probs.probs[idx]
                p_out = p_yes if pos.outcome == "YES" else 1 - p_yes
                new_fc = pos.last_eval_at is None or (
                    model.latest_fetch is not None and model.latest_fetch > pos.last_eval_at
                )
                d = decide_exit(
                    p_model=p_out,
                    best_bid=ob.best_bid,
                    tp_buffer=float(eff.TP_BUFFER),
                    exit_edge=float(eff.EXIT_EDGE),
                    new_forecast=new_fc,
                )
                pos.last_p_model, pos.last_eval_at = p_out, now
                if d.kind == ExitKind.HOLD:
                    if pos.exit_signal:
                        pos.exit_signal, pos.exit_signal_detail = "", ""
                    continue
                if pos.track == "MANUAL":
                    if pos.exit_signal != d.kind.value:
                        pos.exit_signal, pos.exit_signal_detail = d.kind.value, d.reason
                        label = "market caught up" if d.kind == ExitKind.TAKE_PROFIT else "forecast flipped"
                        alert(
                            s,
                            "exit",
                            f"💸 SELL — {label}\n{pos.market.bucket_label} {pos.outcome} · {ev.title}\n"
                            f"new p_model {100 * p_out:.1f}% · bid {100 * (ob.best_bid or 0):.1f}%\n"
                            f"{pos.market.url}",
                        )
                    continue
                n += self._exit_bot_position(s, eff, mode, pos, ob, d.kind.value, d.reason)
            s.commit()
        return n

    def _exit_bot_position(
        self, s: Session, eff: Effective, mode: Mode, pos: Position, ob: OrderBook, kind: str, reason: str
    ) -> int:
        working = s.scalar(
            select(OrderIntent.id).where(
                OrderIntent.position_id == pos.id,
                OrderIntent.side == "SELL",
                OrderIntent.status.in_(("PENDING", "OPEN", "PARTIAL")),
            )
        )
        if working or ob.best_bid is None:
            return 0
        ex = self.executor(s, Mode(pos.mode))
        if ex is None:
            return 0
        price = ob.best_bid
        size = round(min(pos.size, ob.bid_depth(price)), 2)
        if size < min(pos.market.min_order_size, pos.size) or size <= 0:
            return 0
        pos.exit_signal, pos.exit_signal_detail = kind, reason
        s.commit()
        place_order(
            self.sessions,
            ex,
            market=pos.market,
            outcome=pos.outcome,
            side="SELL",
            price=price,
            size=size,
            reason=f"exit {kind}: {reason}",
            position_id=pos.id,
        )
        alert(
            s,
            "exit",
            f"🚪 {pos.mode} exit {kind} — {pos.market.bucket_label} {pos.outcome}: SELL {size:g} @ "
            f"{price:.2f}\n{reason}",
        )
        s.expire_all()
        return 1

    def close_position(self, position_id: int, reason: str = "manual close") -> str:
        with self.exec_lock, self.sessions() as s:
            pos = s.get(Position, position_id)
            if pos is None or pos.status != "OPEN":
                return "position not open"
            if pos.track == "MANUAL":
                pos.status, pos.closed_at, pos.exit_reason = "CLOSED", datetime.now(UTC), reason
                s.commit()
                return "manual position marked closed"
            ob = self.book(pos.token_id, max_age_s=0)
            if ob.best_bid is None:
                return "no bid to sell into"
            ex = self.executor(s, Mode(pos.mode))
            if ex is None:
                return "no executor"
            place_order(
                self.sessions,
                ex,
                market=pos.market,
                outcome=pos.outcome,
                side="SELL",
                price=ob.best_bid,
                size=pos.size,
                reason=reason,
                position_id=pos.id,
            )
            return f"SELL {pos.size:g} @ {ob.best_bid:.2f} sent"

    def cancel_all(self, book: str | None = None) -> int:
        n = 0
        with self.exec_lock, self.sessions() as s:
            if book in (None, "PAPER"):
                n += self.paper.cancel_all(s)
            if book in (None, "LIVE") and (self._live is not None or self._has_live_orders(s)):
                try:
                    ex = self.executor(s, Mode.LIVE)
                    if ex is not None:
                        n += ex.cancel_all(s)
                except Exception as e:
                    log.error("live cancel_all failed: %s", e)
                    alert(s, "health", f"❗ LIVE cancel-all failed: {e}")
            s.commit()
        return n

    def _has_live_orders(self, s: Session) -> bool:
        return bool(
            s.scalar(
                select(OrderIntent.id).where(
                    OrderIntent.mode == "LIVE", OrderIntent.status.in_(("PENDING", "OPEN", "PARTIAL"))
                )
            )
        )

    # ------------------------------------------------------------------ resolutions
    def process_resolutions(self) -> int:
        n = 0
        with self.exec_lock, self.sessions() as s:
            eff = effective(s, self.settings)
            markets = s.scalars(select(Market).where(Market.resolved_yes.is_not(None))).all()
            for m in markets:
                won_yes = bool(m.resolved_yes)
                for pos in s.scalars(select(Position).where(Position.market_id == m.id, Position.status == "OPEN")):
                    won = (pos.outcome == "YES") == won_yes
                    if pos.mode == "LIVE" and won and pos.size > 0 and not pos.redeemed:
                        try:
                            tx = self.live_client(s).redeem(m.id)
                            pos.redeemed = True
                            audit(s, "WORKER", "REDEEM", f"{m.id} tx {tx}")
                        except Exception as e:
                            alert(
                                s,
                                "health",
                                f"❗ Redeem failed for {m.bucket_label} · {m.event.title}: {e}",
                                dedupe_key=f"redeem:{m.id}",
                            )
                            continue
                    pnl = books.apply_resolution(s, pos.mode, pos, won)
                    n += 1
                    alert(
                        s,
                        "resolution",
                        f"🏁 Resolved {'WIN' if won else 'LOSS'} ({pos.mode}) — {m.bucket_label} "
                        f"{pos.outcome} · {m.event.title}\nP&L ${books.usd(pnl):+.2f}",
                    )
                for pr in s.scalars(
                    select(Prediction).where(Prediction.market_id == m.id, Prediction.outcome.is_(None))
                ):
                    pr.outcome = 1 if won_yes else 0
                for sig in s.scalars(select(Signal).where(Signal.market_id == m.id, Signal.won.is_(None))):
                    sig.won = (sig.outcome == "YES") == won_yes
                    cost = sig.max_limit_price + fee_or_zero(m, sig.max_limit_price)
                    sig.roi_at_limit = (1 - cost) / cost if sig.won and cost > 0 else -1.0
                    sig.status = "RESOLVED"
            # calibration-based auto-disable
            disabled = set(get_state(s, "disabled_cities", []) or [])
            for city in {c for (c,) in s.execute(select(Prediction.city).distinct())}:
                since = datetime.now(UTC) - timedelta(days=30)
                pairs = [
                    (p, int(o))
                    for p, o in s.execute(
                        select(Prediction.p_model, Prediction.outcome).where(
                            Prediction.city == city, Prediction.outcome.is_not(None), Prediction.ts >= since
                        )
                    )
                ]
                if city not in disabled and should_disable(pairs, float(eff.MAX_BRIER)):
                    disabled.add(city)
                    score, cnt = brier(pairs)
                    audit(s, "WORKER", "CITY_DISABLED", f"{city}: Brier {score:.3f} over {cnt}")
                    alert(s, "risk", f"🧭 Auto-disabled {city}: 30-day Brier {score:.3f} > {eff.MAX_BRIER} (n={cnt})")
            set_state(s, "disabled_cities", sorted(disabled))
            s.commit()
        return n

    # ------------------------------------------------------------------ bias refit
    def refit_bias(self, days: int = 365) -> int:
        stations = load_stations()
        with self.sessions() as s:
            eff = effective(s, self.settings)
            rows = s.execute(
                select(WeatherEvent.icao, WeatherEvent.unit, WeatherEvent.source)
                .where(WeatherEvent.parse_status == "PARSED")
                .distinct()
            ).all()
            families = [f for f, w in eff.MODEL_WEIGHTS.items() if w > 0 and f in FAMILIES]
        end = datetime.now(UTC).date() - timedelta(days=1)
        start = end - timedelta(days=days)
        n = 0
        for icao, unit, source in {(r[0], r[1], r[2]) for r in rows}:
            st = stations.get(icao)
            if st is None:
                continue
            try:
                obs = self.c.iem.observations(st, start, end, source)
            except Exception as e:
                log.warning("IEM %s failed: %s", icao, e)
                continue
            truth = {var: daily_extremes(obs, unit, var) for var in ("max", "min")}
            for fam in families:
                for var in ("max", "min"):
                    try:
                        fc = self.c.openmeteo.previous_runs_daily(st.lat, st.lon, st.tz, fam, unit, var, start, end)
                    except Exception as e:
                        log.warning("previous runs %s %s failed: %s", fam, icao, e)
                        continue
                    with self.sessions() as s:
                        for lead, series in fc.items():
                            fit = biasmod.fit(series, truth[var], lead, unit)
                            row = s.scalar(
                                select(StationBias).where(
                                    StationBias.icao == icao,
                                    StationBias.family == fam,
                                    StationBias.variable == var,
                                    StationBias.lead_day == lead,
                                )
                            )
                            if row is None:
                                row = StationBias(
                                    icao=icao, family=fam, variable=var, lead_day=lead, unit=unit, mu=0, sigma=0, n=0
                                )
                                s.add(row)
                            row.unit, row.mu, row.sigma, row.n = unit, fit.mu, fit.sigma, fit.n
                            row.fitted_at = datetime.now(UTC)
                            n += 1
                        s.commit()
        with self.sessions() as s:
            set_state(s, "last_bias_fit", datetime.now(UTC).isoformat())
            s.commit()
        return n

    # ------------------------------------------------------------------ equity / health
    def snapshot_equity(self) -> None:
        with self.sessions() as s:
            eff = effective(s, self.settings)
            for book in ("PAPER", "LIVE", "MANUAL"):
                if not books.deposits(s, book) and not books.open_positions(s, book):
                    continue
                positions = books.open_positions(s, book)
                if positions:
                    try:
                        obs = self.books([p.token_id for p in positions])
                        for p in positions:
                            ob = obs.get(p.token_id)
                            if ob and ob.best_bid is not None:
                                p.last_mark = ob.best_bid
                    except Exception as e:
                        log.info("mark refresh failed: %s", e)
                snap = books.snapshot(s, book, self.settings.TZ)
                peak = books.equity_peak(s, book, snap.equity)
                s.add(EquitySnapshot(book=book, cash=snap.cash, exposure=snap.exposure, equity=snap.equity))
                if book != "MANUAL":
                    from app.risk import RiskState

                    frac = drawdown_fraction_of_limit(
                        RiskState(realized_today=snap.realized_today, equity=snap.equity, equity_peak=peak),
                        self.limits(eff),
                    )
                    level = 80 if frac >= 0.8 else 50 if frac >= 0.5 else 0
                    key = f"dd_warn:{book}:{books.start_of_today(self.settings.TZ).date()}"
                    if level and (get_state(s, key, 0) or 0) < level:
                        set_state(s, key, level)
                        alert(
                            s,
                            "risk",
                            f"⚠️ {book} drawdown at {100 * frac:.0f}% of the loss limit "
                            f"(equity ${snap.equity:.2f}, today ${snap.realized_today:+.2f})",
                        )
            s.commit()

    def check_geoblock(self) -> bool:
        st = check_geoblock(self.settings.POLY_GEOBLOCK_URL)
        with self.sessions() as s:
            was = get_state(s, "geoblocked")
            set_state(s, "geoblocked", st.blocked)
            set_state(s, "geoblock_detail", st.detail)
            set_state(s, "geoblock_checked_at", st.checked_at.isoformat())
            if st.blocked and was is not True:
                alert(s, "health", f"🌍 Geoblock: BLOCKED ({st.detail}). Orders are refused until this clears.")
            elif not st.blocked and was is True:
                alert(s, "health", f"🌍 Geoblock cleared ({st.detail}).")
            s.commit()
        return st.blocked

    # ------------------------------------------------------------------ manual track
    def took(self, signal_id: int, price: float, usd: float, source: str, actor: str = "") -> str:
        if not (0 < price < 1) or usd <= 0:
            raise ValueError("price must be in (0,1) and usd > 0")
        with self.sessions() as s:
            sig = s.get(Signal, signal_id)
            if sig is None:
                raise ValueError(f"signal {signal_id} not found")
            books.ensure_deposit(s, "MANUAL", float(self.settings.MANUAL_BANKROLL_USD))
            m = sig.market
            try:
                fee = fee_per_share(m, price)
            except ValueError:
                fee = 0.0
            shares = round(usd / (price + fee), 2)
            pos = books.get_or_create_position(
                s,
                book="MANUAL",
                market=m,
                outcome=sig.outcome,
                signal_id=sig.id,
                reason=f"manual from signal #{sig.id}",
            )
            books.apply_buy(s, "MANUAL", pos, price, shares, fee, ref=f"took:{sig.id}")
            sig.action = (sig.action + " · " if sig.action else "") + f"MANUAL took {shares:g} @ {price:.2f}"
            audit(s, source, "TOOK", f"signal {signal_id}: {shares:g} {sig.outcome} @ {price} (${usd})", actor)
            s.commit()
            return f"Logged MANUAL {sig.outcome} {shares:g} @ {price:.2f} on {m.bucket_label} · {m.event.title}"

    def import_manual_wallet(self) -> int:
        addr = self.settings.MANUAL_WALLET_ADDRESS
        if not addr:
            return 0
        rows = self.c.data_api.positions(addr)
        n = 0
        with self.sessions() as s:
            books.ensure_deposit(s, "MANUAL", float(self.settings.MANUAL_BANKROLL_USD))
            for r in rows:
                m = s.get(Market, str(r.get("conditionId") or ""))
                if m is None:
                    continue
                asset = str(r.get("asset") or "")
                outcome = "YES" if asset == m.token_yes else "NO" if asset == m.token_no else ""
                if not outcome:
                    continue
                size = float(r.get("size") or 0)
                avg = float(r.get("avgPrice") or 0)
                pos = books.get_or_create_position(
                    s, book="MANUAL", market=m, outcome=outcome, reason="imported from wallet"
                )
                pos.external_ref = asset
                delta = round(size - pos.size, 6)
                if delta > 1e-6:
                    books.apply_buy(s, "MANUAL", pos, avg, delta, 0.0, ref=f"import:{asset}")
                    n += 1
                elif delta < -1e-6:
                    px = float(r.get("curPrice") or avg)
                    books.apply_sell(
                        s,
                        "MANUAL",
                        pos,
                        px,
                        -delta,
                        0.0,
                        ref=f"import:{asset}",
                        reason="sold in wallet (imported, approximate price)",
                    )
                    n += 1
            s.commit()
        return n

    # ------------------------------------------------------------------ control plane
    def check_kill_file(self) -> None:
        if self.settings.kill_file.exists():
            with self.sessions() as s:
                if not get_state(s, "halted", False) or get_state(s, "kill_file_seen") != "1":
                    set_state(s, "kill_file_seen", "1")
                    halt(s, "KILL file present", "WORKER")
                    s.commit()
                    self.cancel_all()
        else:
            with self.sessions() as s:
                if get_state(s, "kill_file_seen") == "1":
                    set_state(s, "kill_file_seen", "0")
                    s.commit()

    def process_commands(self) -> int:
        with self.sessions() as s:
            cmds = list(s.scalars(select(Command).where(Command.status == "PENDING").order_by(Command.id)))
        for cmd in cmds:
            result, status = "", "DONE"
            try:
                result = self._run_command(cmd)
            except Exception as e:
                status, result = "FAILED", str(e)
                log.exception("command %s failed", cmd.kind)
            with self.sessions() as s:
                c = s.get(Command, cmd.id)
                assert c is not None
                c.status, c.result, c.done_at = status, result[:2000], datetime.now(UTC)
                if status == "FAILED":
                    alert(s, "health", f"❗ Command {cmd.kind} failed: {result[:300]}")
                s.commit()
        return len(cmds)

    def _run_command(self, cmd: Command) -> str:
        p = cmd.payload or {}
        if cmd.kind == "CANCEL_ALL":
            return f"cancelled {self.cancel_all(p.get('book'))} orders"
        if cmd.kind == "CLOSE_POSITION":
            return self.close_position(int(p["position_id"]), reason=f"manual close via {cmd.source}")
        if cmd.kind == "CLOSE_ALL":
            book = p.get("book")
            with self.sessions() as s:
                ids = [
                    pos.id
                    for pos in s.scalars(
                        select(Position).where(
                            Position.status == "OPEN",
                            Position.track == "BOT",
                            *([Position.mode == book] if book else []),
                        )
                    )
                ]
            return (
                "; ".join(self.close_position(i, reason=f"close all via {cmd.source}") for i in ids) or "nothing open"
            )
        if cmd.kind == "LIVE_EXIT":
            out = []
            if p.get("cancel_orders"):
                out.append(f"cancelled {self.cancel_all('LIVE')} LIVE orders")
            if p.get("close_positions"):
                with self.sessions() as s:
                    ids = [
                        pos.id
                        for pos in s.scalars(select(Position).where(Position.status == "OPEN", Position.mode == "LIVE"))
                    ]
                out += [self.close_position(i, reason="leaving LIVE") for i in ids]
            return "; ".join(out) or "kept LIVE orders and positions"
        if cmd.kind == "RECONCILE":
            self.reconcile_live()
            return "reconciled"
        if cmd.kind == "TOOK":
            return self.took(int(p["signal_id"]), float(p["price"]), float(p["usd"]), cmd.source, cmd.actor)
        return f"unknown command {cmd.kind}"

    def startup(self) -> None:
        with self.sessions() as s:
            mode = current_mode(s, self.settings)
            if self.settings.MODE_FORCE_ENV or get_state(s, "mode") is None:
                set_state(s, "mode", mode.value)
            books.ensure_deposit(s, "MANUAL", float(self.settings.MANUAL_BANKROLL_USD))
            if mode == Mode.PAPER:
                books.ensure_deposit(s, "PAPER", float(self.settings.BANKROLL_USD))
                if not get_state(s, "paper_started_at"):
                    set_state(s, "paper_started_at", datetime.now(UTC).isoformat())
            audit(s, "WORKER", "STARTUP", f"mode={mode.value}")
            s.commit()
        if mode == Mode.LIVE or self._has_live_orders_now():
            try:
                self.reconcile_live()  # before anything else touches the exchange
            except Exception as e:
                log.error("startup reconcile failed: %s — halting", e)
                with self.sessions() as s:
                    halt(s, f"startup reconcile failed: {e}", "WORKER")
                    s.commit()

    def _has_live_orders_now(self) -> bool:
        with self.sessions() as s:
            return self._has_live_orders(s)


def fee_or_zero(m: Market, price: float) -> float:
    try:
        return fee_per_share(m, price)
    except ValueError:
        return 0.0


def fmt_hours(h: float) -> str:
    if h < 0:
        return "closed"
    d, rem = divmod(int(h * 60), 1440)
    hh, mm = divmod(rem, 60)
    return f"{d}d {hh}h" if d else f"{hh}h {mm:02d}m"


def format_signal(sig: Signal, m: Market, ev: WeatherEvent, lead_hours: float) -> str:
    action = f"BUY {sig.outcome}"
    run = sig.forecast_run.strftime("%Y-%m-%d %HZ") if sig.forecast_run else "n/a"
    return (
        f"📈 #{sig.id} {action} — {m.bucket_label}\n"
        f"{ev.title} ({ev.city.title()}, {ev.local_date})\n"
        f"Model {100 * sig.p_model:.1f}% vs market {100 * sig.ask:.1f}% · edge {100 * sig.edge:.1f}% after fees\n"
        f"Max limit {sig.max_limit_price:.2f} · stake ${sig.suggested_stake_usd:.2f} · {sig.confidence}\n"
        f"Resolves in {fmt_hours(lead_hours)} · forecast run {run}\n"
        f"{m.url}"
    )
