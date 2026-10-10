"""Backtest: replay resolved weather markets with forecasts as they were available at decision time.

Usage:
    python -m backtest.runner --months 9 [--leads 24,36,48] [--spread-penalty 0.02] [--cache backtest/cache]

No lookahead:
* forecasts come from Open-Meteo's Previous Runs API. For a decision L hours before the end of the local day
  we use lead N = ceil((L + 6) / 24) days, so every hour of the target day was forecast at least 6 h before
  the decision (a conservative run-availability lag);
* bias and error spread for that station/lead are fitted only on days that had already resolved before
  the decision (expanding window, same `forecast.bias.fit` as live);
* prices are the CLOB price history at or before the decision time, with a conservative spread penalty
  (no historical order books are available), and today's market fee parameters.

Limitations (also printed in every report):
* deterministic forecast + fitted Gaussian error stands in for the live ensemble KDE;
* fills assume the visible price plus penalty was executable at our size; real liquidity is not replayed;
* positions are held to resolution (take-profit / edge-reversal exits are not simulated);
* fee parameters are today's, not necessarily those in force at the time.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import random
import statistics
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from app.forecast import bias as biasmod
from app.forecast.calibrate import brier, lead_band
from app.markets.parser import EventSpec, ParseError, parse_event
from app.models.weather_prob import bucket_edges, kde_bucket_probs
from app.services.engine import day_end_utc
from app.strategy.edge import Book, EdgeParams, FeeModel, Level, evaluate
from app.strategy.sizing import size_order

REPORTS = Path(os.environ.get("EDGECAST_REPORTS_DIR") or Path(__file__).resolve().parent / "reports")
AVAILABILITY_LAG_H = 6
MIN_HISTORY_DAYS = 30
SMALL_SAMPLE_BETS = 200


class DataSource(Protocol):
    def resolved_events(self, since: date) -> list[dict[str, Any]]: ...
    def price_history(self, token_id: str, start: datetime, end: datetime) -> list[tuple[int, float]]: ...
    def fee(self, condition_id: str) -> FeeModel | None: ...
    def previous_runs(self, spec: EventSpec, family: str, start: date, end: date) -> dict[int, dict[date, float]]: ...
    def truth(self, spec: EventSpec, start: date, end: date) -> dict[date, int]: ...


@dataclass
class Params:
    leads: tuple[int, ...] = (24, 36, 48, 60)
    spread_penalty: float = 0.02
    bankroll: float = 200.0
    kelly: float = 0.15
    min_edge: float = 0.06
    sanity_max_edge: float = 0.40
    max_stake: float = 10.0
    max_market: float = 20.0
    max_city_day: float = 30.0
    families: dict[str, float] = field(default_factory=lambda: {"ecmwf_ifs": 0.6, "gfs": 0.4})


@dataclass
class Bet:
    event: str
    city: str
    local_date: str
    bucket: str
    outcome: str
    lead_hours: int
    decided_at: str
    p: float
    price: float
    fee: float
    shares: float
    stake: float
    won: bool
    pnl: float


@dataclass
class Result:
    params: Params
    bets: list[Bet]
    predictions: list[tuple[float, int, float | None, str]]  # (p_model, outcome, market price, lead band)
    skipped: dict[str, int]
    events_total: int
    events_parsed: int
    started: str
    finished: str

    # ---------------------------------------------------------------- metrics
    def equity_curve(self) -> list[tuple[str, float]]:
        eq, out = self.params.bankroll, []
        for b in sorted(self.bets, key=lambda b: (b.local_date, b.decided_at)):
            eq += b.pnl
            out.append((b.local_date, eq))
        return out

    def summary(self) -> dict[str, Any]:
        n = len(self.bets)
        staked = sum(b.stake for b in self.bets)
        pnl = sum(b.pnl for b in self.bets)
        curve = self.equity_curve()
        peak, mdd = self.params.bankroll, 0.0
        for _, v in curve:
            peak = max(peak, v)
            mdd = max(mdd, peak - v)
        daily: dict[str, float] = {}
        for b in self.bets:
            daily[b.local_date] = daily.get(b.local_date, 0.0) + b.pnl
        dv = list(daily.values())
        sharpe = (
            (statistics.mean(dv) / statistics.stdev(dv) * math.sqrt(365))
            if len(dv) > 2 and statistics.stdev(dv) > 0
            else None
        )
        model_brier, npred = brier([(p, o) for p, o, _, _ in self.predictions])
        market_brier, _ = brier([(m, o) for _, o, m, _ in self.predictions if m is not None])
        ci = bootstrap_roi(self.bets)
        verdict = self.verdict(n, len(daily), ci)
        return {
            "bets": n,
            "hit_rate": (sum(b.won for b in self.bets) / n) if n else None,
            "staked": round(staked, 2),
            "pnl": round(pnl, 2),
            "roi": (pnl / staked) if staked else None,
            "roi_ci95": ci,
            "max_drawdown": round(mdd, 2),
            "sharpe_like": sharpe,
            "days": len(daily),
            "model_brier": model_brier,
            "market_brier": market_brier,
            "predictions": npred,
            "events_total": self.events_total,
            "events_parsed": self.events_parsed,
            "skipped": self.skipped,
            "verdict": verdict,
        }

    def verdict(self, n: int, days: int, ci: tuple[float, float] | None) -> str:
        if n == 0:
            return "No bets: nothing to conclude."
        if n < SMALL_SAMPLE_BETS or days < 60:
            return (
                f"Sample too small to conclude anything ({n} bets over {days} days; want ≥{SMALL_SAMPLE_BETS} "
                "bets over ≥60 days)."
            )
        if ci and ci[0] > 0:
            return "ROI 95% CI is above zero, subject to the limitations listed."
        if ci and ci[1] < 0:
            return "ROI 95% CI is below zero: the strategy lost money in this replay."
        return "ROI 95% CI includes zero: no evidence of an edge either way."

    def breakdown(self, key: Callable[[Bet], str]) -> list[tuple[str, int, float, float]]:
        groups: dict[str, list[Bet]] = {}
        for b in self.bets:
            groups.setdefault(key(b), []).append(b)
        rows = []
        for k, bs in sorted(groups.items()):
            st = sum(b.stake for b in bs)
            rows.append((k, len(bs), sum(b.pnl for b in bs), (sum(b.pnl for b in bs) / st) if st else 0.0))
        return rows


def bootstrap_roi(bets: list[Bet], n: int = 2000, seed: int = 7) -> tuple[float, float] | None:
    if len(bets) < 10:
        return None
    rng = random.Random(seed)
    rois = []
    for _ in range(n):
        sample = [bets[rng.randrange(len(bets))] for _ in bets]
        st = sum(b.stake for b in sample)
        rois.append(sum(b.pnl for b in sample) / st if st else 0.0)
    rois.sort()
    return rois[int(0.025 * n)], rois[int(0.975 * n)]


def price_at(history: list[tuple[int, float]], t: datetime) -> float | None:
    ts = t.timestamp()
    best = None
    for when, p in history:
        if when <= ts:
            best = p
        else:
            break
    return best


def lead_days_for(lead_hours: int) -> int:
    return min(7, max(1, math.ceil((lead_hours + AVAILABILITY_LAG_H) / 24)))


# ------------------------------------------------------------------ core replay (pure given a DataSource)
def run(source: DataSource, since: date, params: Params) -> Result:
    started = datetime.now(UTC).isoformat()
    raw = source.resolved_events(since)
    specs: list[EventSpec] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for ev in raw:
        try:
            spec = parse_event(ev)
        except ParseError:
            skip("unparsed")
            continue
        if not any(m.resolved_yes for m in spec.markets):
            skip("no resolved winner")
            continue
        specs.append(spec)

    # Forecast + truth history per (station, unit, variable, family), fetched once.
    hist_start = since - timedelta(days=400)
    hist_end = max((s.local_date for s in specs), default=since)
    prev: dict[tuple[str, str, str, str], dict[int, dict[date, float]]] = {}
    truth: dict[tuple[str, str, str], dict[date, int]] = {}
    for spec in specs:
        tk = (spec.station.icao, spec.unit, spec.variable)
        if tk not in truth:
            try:
                truth[tk] = source.truth(spec, hist_start, hist_end)
            except Exception:
                truth[tk] = {}
        for fam in params.families:
            pk = (*tk, fam)
            if pk not in prev:
                try:
                    prev[pk] = source.previous_runs(spec, fam, hist_start, hist_end)
                except Exception:
                    prev[pk] = {}

    bets: list[Bet] = []
    preds: list[tuple[float, int, float | None, str]] = []
    bankroll = params.bankroll
    exposure_city: dict[str, float] = {}
    for spec in sorted(specs, key=lambda s: s.local_date):
        tk = (spec.station.icao, spec.unit, spec.variable)
        obs = truth.get(tk, {})
        if not obs:
            skip("no station truth")
            continue
        end = day_end_utc(spec.local_date, spec.station.tz)
        buckets = [m.bucket.continuous for m in spec.markets]
        edges = bucket_edges(buckets)
        entered: set[str] = set()
        city_key = f"{spec.city}|{spec.local_date}"
        for lead in sorted(params.leads, reverse=True):
            t = end - timedelta(hours=lead)
            n_day = lead_days_for(lead)
            fam_probs: dict[str, np.ndarray] = {}
            for fam, w in params.families.items():
                series = prev.get((*tk, fam), {}).get(n_day, {})
                x = series.get(spec.local_date)
                if x is None or w <= 0:
                    continue
                # Errors known strictly before the decision: days whose local day ended before t.
                known = {d: v for d, v in series.items() if day_end_utc(d, spec.station.tz) <= t and d in obs}
                if len(known) < MIN_HISTORY_DAYS:
                    continue
                fit = biasmod.fit(known, {d: obs[d] for d in known}, n_day, spec.unit)
                fam_probs[fam] = kde_bucket_probs(np.array([x - fit.mu]), fit.sigma, edges)
            if not fam_probs:
                skip("no forecast history at decision time")
                continue
            tw = sum(params.families[f] for f in fam_probs)
            probs = sum(params.families[f] * p for f, p in fam_probs.items()) / tw
            for i, m in enumerate(spec.markets):
                won_yes = bool(m.resolved_yes)
                hist = source.price_history(m.token_yes, t - timedelta(days=3), t)
                px = price_at(hist, t)
                preds.append((float(probs[i]), int(won_yes), px, lead_band(lead)))
                if px is None:
                    skip("no price at decision time")
                    continue
                if m.condition_id in entered:
                    continue
                fee = source.fee(m.condition_id)
                if fee is None:
                    skip("fee unknown")
                    continue
                pen = params.spread_penalty
                ep = EdgeParams(params.min_edge, params.sanity_max_edge, 2 * pen + 1e-9, 0.0, 0.0, 0.01)
                big = 1e6
                yes_book = Book([Level(max(px - pen, 0.01), big)], [Level(min(px + pen, 0.99), big)])
                no_px = 1 - px
                no_book = Book([Level(max(no_px - pen, 0.01), big)], [Level(min(no_px + pen, 0.99), big)])
                p_yes = float(probs[i])
                opps = [evaluate(p_yes, "YES", yes_book, fee, ep), evaluate(1 - p_yes, "NO", no_book, fee, ep)]
                opps = [o for o in opps if o.tradeable]
                if not opps:
                    continue
                o = max(opps, key=lambda o: o.edge)
                fams_out = {f: (p[i] if o.outcome == "YES" else 1 - p[i]) for f, p in fam_probs.items()}
                assert o.ask is not None
                if len(fams_out) < 2 or any(
                    v - o.ask - fee.per_share(o.ask) < params.min_edge / 2 for v in fams_out.values()
                ):
                    skip("single-family edge")
                    continue
                assert o.ask is not None
                price = o.ask
                fps = fee.per_share(price)
                room = min(params.max_stake, params.max_market, params.max_city_day - exposure_city.get(city_key, 0.0))
                sized = size_order(
                    p=o.p,
                    price=price,
                    fee_per_share=fps,
                    bankroll=max(bankroll, 0.0),
                    kelly_mult=params.kelly,
                    max_stake_usd=max(room, 0.0),
                    depth_shares=big,
                    max_depth_pct=100,
                    min_order_size=5,
                )
                if sized.shares <= 0:
                    skip("size below minimum / caps")
                    continue
                won = (o.outcome == "YES") == won_yes
                pnl = (sized.shares if won else 0.0) - sized.stake_usd
                bankroll += pnl
                exposure_city[city_key] = exposure_city.get(city_key, 0.0) + sized.stake_usd
                entered.add(m.condition_id)
                bets.append(
                    Bet(
                        spec.slug,
                        spec.city,
                        spec.local_date.isoformat(),
                        m.bucket.label,
                        o.outcome,
                        lead,
                        t.isoformat(),
                        o.p,
                        price,
                        fps,
                        sized.shares,
                        sized.stake_usd,
                        won,
                        pnl,
                    )
                )
    return Result(params, bets, preds, skipped, len(raw), len(specs), started, datetime.now(UTC).isoformat())


# ------------------------------------------------------------------ report
def _svg_curve(points: list[tuple[str, float]], w: int = 760, h: int = 220) -> str:
    if len(points) < 2:
        return "<p class='muted'>Not enough bets for a curve.</p>"
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pad = 36
    xs = [pad + i * (w - 2 * pad) / (len(vals) - 1) for i in range(len(vals))]
    ys = [h - pad - (v - lo) * (h - 2 * pad) / span for v in vals]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(zip(xs, ys, strict=True)))
    return (
        f"<svg viewBox='0 0 {w} {h}' width='100%' role='img' aria-label='Equity curve'>"
        f"<line x1='{pad}' y1='{h - pad}' x2='{w - pad}' y2='{h - pad}' class='axis'/>"
        f"<text x='4' y='{pad}' class='lbl'>${hi:,.0f}</text><text x='4' y='{h - pad}' class='lbl'>${lo:,.0f}</text>"
        f"<path d='{path}' class='line'/></svg>"
    )


def render_html(res: Result) -> str:
    s = res.summary()
    e = html.escape

    def pct(v: Any) -> str:
        return "—" if v is None else f"{100 * v:+.1f}%"

    def num(v: Any, d: int = 3) -> str:
        return "—" if v is None else f"{v:.{d}f}"

    rows_city = "".join(
        f"<tr><td>{e(k)}</td><td>{n}</td><td>${p:+.2f}</td><td>{pct(r)}</td></tr>"
        for k, n, p, r in res.breakdown(lambda b: b.city)
    )
    rows_lead = "".join(
        f"<tr><td>{k}h</td><td>{n}</td><td>${p:+.2f}</td><td>{pct(r)}</td></tr>"
        for k, n, p, r in res.breakdown(lambda b: f"{b.lead_hours:03d}")
    )
    skipped = "".join(f"<li>{e(k)}: {v}</li>" for k, v in sorted(res.skipped.items(), key=lambda kv: -kv[1]))
    ci = s["roi_ci95"]
    bets_rows = "".join(
        f"<tr><td>{b.local_date}</td><td>{e(b.city)}</td><td>{e(b.bucket)}</td><td>{b.outcome}</td><td>{b.lead_hours}h</td>"
        f"<td>{100 * b.p:.1f}%</td><td>{b.price:.2f}</td><td>${b.stake:.2f}</td><td>{'W' if b.won else 'L'}</td>"
        f"<td>${b.pnl:+.2f}</td></tr>"
        for b in res.bets[-300:]
    )
    small = s["bets"] < SMALL_SAMPLE_BETS or s["days"] < 60
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Edgecast backtest</title>
<style>
:root{{--bg:#fafafa;--fg:#18181b;--muted:#52525b;--line:#e4e4e7;--card:#fff;--accent:#2a78d6;--warn:#92400e;--warnbg:#fef3c7}}
@media (prefers-color-scheme: dark){{:root{{--bg:#09090b;--fg:#e4e4e7;--muted:#a1a1aa;--line:#27272a;--card:#18181b;--accent:#3987e5;--warn:#fde68a;--warnbg:#422006}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 Inter,system-ui,sans-serif;margin:0;padding:24px 16px}}
main{{max-width:980px;margin:auto}} h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:28px 0 8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}} .card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}}
.k{{font-size:12px;color:var(--muted)}} .v{{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}} td,th{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}} th{{color:var(--muted);font-weight:500;font-size:12px}}
.warn{{background:var(--warnbg);color:var(--warn);border-radius:10px;padding:10px 12px;font-weight:600}} .muted{{color:var(--muted)}}
svg .line{{fill:none;stroke:var(--accent);stroke-width:2}} svg .axis{{stroke:var(--line)}} svg .lbl{{fill:var(--muted);font-size:11px}}
.scroll{{overflow-x:auto}}
</style></head><body><main>
<h1>Edgecast backtest</h1><p class="muted">Generated {e(res.finished[:19])}Z · {s["events_total"]} resolved events fetched, {s["events_parsed"]} parsed.</p>
<p class="{"warn" if small else "card"}">{e(s["verdict"])}</p>
<div class="grid">
<div class="card"><div class="k">Bets</div><div class="v">{s["bets"]}</div></div>
<div class="card"><div class="k">Hit rate</div><div class="v">{pct(s["hit_rate"]).lstrip("+")}</div></div>
<div class="card"><div class="k">ROI</div><div class="v">{pct(s["roi"])}</div><div class="k">95% CI {pct(ci[0]) if ci else "—"} … {pct(ci[1]) if ci else "—"}</div></div>
<div class="card"><div class="k">P&amp;L</div><div class="v">${s["pnl"]:+.2f}</div><div class="k">on ${s["staked"]:.2f} staked</div></div>
<div class="card"><div class="k">Max drawdown</div><div class="v">${s["max_drawdown"]:.2f}</div></div>
<div class="card"><div class="k">Sharpe-like (daily, ann.)</div><div class="v">{num(s["sharpe_like"], 2)}</div></div>
</div>
<h2>Equity</h2><div class="card">{_svg_curve(res.equity_curve())}</div>
<h2>Calibration</h2><div class="grid">
<div class="card"><div class="k">Model Brier (all buckets)</div><div class="v">{num(s["model_brier"])}</div><div class="k">{s["predictions"]} predictions</div></div>
<div class="card"><div class="k">Market-price Brier (baseline)</div><div class="v">{num(s["market_brier"])}</div><div class="k">lower = better; model must beat this</div></div></div>
<h2>P&amp;L by city</h2><div class="card scroll"><table><tr><th>City</th><th>Bets</th><th>P&amp;L</th><th>ROI</th></tr>{rows_city}</table></div>
<h2>P&amp;L by lead time</h2><div class="card scroll"><table><tr><th>Lead</th><th>Bets</th><th>P&amp;L</th><th>ROI</th></tr>{rows_lead}</table></div>
<h2>Skipped decisions</h2><div class="card"><ul>{skipped or "<li>none</li>"}</ul></div>
<h2>Parameters</h2><div class="card"><pre>{e(json.dumps(asdict(res.params), indent=2))}</pre></div>
<h2>Method &amp; limitations</h2><div class="card"><ul>
<li>Forecasts: Open-Meteo Previous Runs (deterministic) at lead N = ceil((L+6)/24) days, so no hour of the target day uses a run issued after the decision time minus 6 h.</li>
<li>Bias/spread fitted on station observations (IEM METARs, same reduction as the market's resolution rule) for days that resolved before each decision; Gaussian error replaces the live ensemble KDE.</li>
<li>Prices: CLOB price history at or before the decision, plus a {res.params.spread_penalty:.2f} spread penalty each side. No historical order books: depth and queue position are not replayed.</li>
<li>Fees: today's market fee parameters applied to every fill.</li>
<li>Exits not simulated: every bet is held to resolution.</li>
<li>One entry per market; city/day and per-market caps applied; Kelly {res.params.kelly} on a running bankroll.</li></ul></div>
<h2>Bets (last 300)</h2><div class="card scroll"><table><tr><th>Date</th><th>City</th><th>Bucket</th><th>Side</th><th>Lead</th><th>p</th><th>Price</th><th>Stake</th><th>W/L</th><th>P&amp;L</th></tr>{bets_rows}</table></div>
</main></body></html>"""


def save(res: Result, name: str | None = None) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    name = name or f"backtest-{datetime.now(UTC):%Y%m%d-%H%M%S}.html"
    path = REPORTS / name
    path.write_text(render_html(res))
    (REPORTS / (path.stem + ".json")).write_text(json.dumps(res.summary(), indent=2, default=str))
    try:
        from app.db.models import BacktestRun
        from app.db.session import session_scope

        with session_scope() as s:
            s.add(BacktestRun(report_path=path.name, summary=json.loads(json.dumps(res.summary(), default=str))))
    except Exception as e:  # the DB is optional for offline runs
        print(f"(not recorded in DB: {e})")
    return path


# ------------------------------------------------------------------ live data source
class LiveSource:  # pragma: no cover - network
    def __init__(self, cache: Path | None = None):
        from app.config import get_settings
        from app.forecast.observations import Iem
        from app.forecast.openmeteo import OpenMeteo
        from app.polymarket.clob import Clob
        from app.polymarket.gamma import Gamma

        s = get_settings()
        self.gamma, self.clob = Gamma(s.POLY_GAMMA_URL), Clob(s.POLY_CLOB_URL)
        self.om = OpenMeteo(s.OPEN_METEO_BASE, s.OPEN_METEO_PREVIOUS_RUNS_BASE)
        self.iem = Iem(s.IEM_BASE)
        self.cache = cache

    def _cached(self, key: str, fn: Callable[[], Any]) -> Any:
        if self.cache is None:
            return fn()
        self.cache.mkdir(parents=True, exist_ok=True)
        f = self.cache / (key.replace("/", "_")[:180] + ".json")
        if f.exists():
            return json.loads(f.read_text())
        v = fn()
        f.write_text(json.dumps(v, default=str))
        return v

    def resolved_events(self, since: date) -> list[dict[str, Any]]:
        evs = self._cached(f"events-{since}", lambda: self.gamma.weather_events(closed=True, max_pages=60))
        return [e for e in evs if (e.get("endDate") or "") >= since.isoformat()]

    def price_history(self, token_id: str, start: datetime, end: datetime) -> list[tuple[int, float]]:
        key = f"px-{token_id}-{int(start.timestamp())}"
        rows = self._cached(
            key,
            lambda: self.clob.prices_history(
                token_id, start_ts=int(start.timestamp()), end_ts=int(end.timestamp()), fidelity=60
            ),
        )
        return [(int(t), float(p)) for t, p in rows]

    def fee(self, condition_id: str) -> FeeModel | None:
        info = self._cached(f"fee-{condition_id}", lambda: asdict(self.clob.market_info(condition_id)))
        fd = info.get("fee")
        return FeeModel(fd["rate"], fd["exponent"]) if fd else None

    def previous_runs(self, spec: EventSpec, family: str, start: date, end: date) -> dict[int, dict[date, float]]:
        st = spec.station
        key = f"prev-{st.icao}-{family}-{spec.unit}-{spec.variable}-{start}-{end}"
        raw = self._cached(
            key,
            lambda: {
                str(n): {d.isoformat(): v for d, v in series.items()}
                for n, series in self.om.previous_runs_daily(
                    st.lat, st.lon, st.tz, family, spec.unit, spec.variable, start, end
                ).items()
            },
        )
        return {int(n): {date.fromisoformat(d): v for d, v in s.items()} for n, s in raw.items()}

    def truth(self, spec: EventSpec, start: date, end: date) -> dict[date, int]:
        from app.forecast.observations import daily_extremes

        key = f"truth-{spec.station.icao}-{spec.source}-{spec.unit}-{spec.variable}-{start}-{end}"

        def fetch() -> dict[str, int]:
            obs = self.iem.observations(spec.station, start, end, spec.source)
            return {d.isoformat(): v for d, v in daily_extremes(obs, spec.unit, spec.variable).items()}

        return {date.fromisoformat(d): v for d, v in self._cached(key, fetch).items()}


def main() -> None:  # pragma: no cover - CLI
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months", type=int, default=9)
    ap.add_argument("--leads", default="24,36,48,60")
    ap.add_argument("--spread-penalty", type=float, default=0.02)
    ap.add_argument("--cache", type=Path, default=Path(__file__).resolve().parent / "cache")
    a = ap.parse_args()
    from app.config import get_settings

    s = get_settings()
    params = Params(
        leads=tuple(int(x) for x in a.leads.split(",")),
        spread_penalty=a.spread_penalty,
        bankroll=s.BANKROLL_USD,
        kelly=s.KELLY_FRACTION,
        min_edge=s.MIN_EDGE,
        sanity_max_edge=s.SANITY_MAX_EDGE,
        max_stake=s.MAX_STAKE_PER_ORDER_USD,
        max_market=s.MAX_EXPOSURE_PER_MARKET_USD,
        max_city_day=s.MAX_EXPOSURE_PER_CITY_DAY_USD,
        families=dict(s.MODEL_WEIGHTS),
    )
    since = (datetime.now(UTC) - timedelta(days=30 * a.months)).date()
    res = run(LiveSource(a.cache), since, params)
    path = save(res)
    print(json.dumps(res.summary(), indent=2, default=str))
    print(f"report: {path}")


if __name__ == "__main__":  # pragma: no cover
    main()
