# Edgecast — Plan

## Status (build complete)

Built and tested end to end on synthetic fixtures. The live data sources were unreachable from the build
environment, so the live-verification steps in the README ("What is verified, and what is not") are still yours to run.

The open questions were not answered before the build. These defaults were used, and each is easy to change:

| Question | Default used | Where to change |
|---|---|---|
| VPS / region | Any 2 vCPU / 2–4 GB host; AWS eu-west-1 recommended | README → Deployment |
| Domain | `PUBLIC_DOMAIN` env; Caddy issues HTTPS automatically | `.env` |
| Telegram | Built and fully tested; off until `TELEGRAM_ENABLED=true` | `.env` |
| Bankroll / caps | Values from the brief ($200 bot, $200 manual, caps as listed) | `.env`, runtime overrides |
| Wallet type | Plain EOA (SDK auto-detects; proxy/safe via `POLY_FUNDER_ADDRESS`) | `.env` |
| Cities | All parsable cities with a curated station (50 stations) | `CITY_ALLOWLIST`, `stations.yaml` |
| Time zone | `TZ=UTC` | `.env` |
| SDK | `polymarket-client==0.13.0` (official unified SDK), pinned, behind an adapter | `pyproject.toml`, `app/execution/live.py` |


Phase 0 deliverable. Covers what the current Polymarket and Open-Meteo stack actually looks like,
the architecture, the probability model, and the open questions that block the build.

## 1. What the research found (and how)

The build container cannot reach `docs.polymarket.com`, any `*.polymarket.com` API, `*.open-meteo.com`, or
`api.weather.gov`. Facts below were read from the **official SDK source on PyPI**
(`polymarket-client` 0.13.0, `py-clob-client-v2` 1.2.0, published 2026-09-25) and from the
**Open-Meteo website source on GitHub**. Anything not confirmed that way is marked *unverified*.

### Polymarket has moved on from the stack in most tutorials

| Topic | Current state (from SDK source) | Implication |
|---|---|---|
| Python client | `py-clob-client` (v1) is legacy. `py-clob-client-v2` now points to the new unified **`polymarket-client`** (`Polymarket/py-sdk`, import `polymarket`), 0.x beta, typed, sync + async | Use `polymarket-client`, pinned to an exact version, behind our own thin adapter in `app/polymarket/` so SDK churn touches one package |
| Collateral | ERC-20 at `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`, 6 decimals. **Not** USDC.e (`0x2791…`) | The wallet must hold this token. How USDC becomes this token (wrap through the collateral adapter or deposit) is *unverified* |
| Exchanges | CTF Exchange V2 `0xE111…996B`, Neg-risk Exchange V2 `0xe222…0F59`, Exchange V3 `0xe333…00Aa` (position-id orders), v1 exchanges still listed | Order version is chosen by the server for `token_id` orders. We pass `token_id` and let the SDK route |
| Approvals | SDK `setup_trading_approvals()` grants max ERC-20 allowance on collateral to: standard exchange, neg-risk exchange, collateral adapter, neg-risk collateral adapter, protocol-v2 router, exchange v3, perps deposit. It also sets ERC-1155 `setApprovalForAll` on the CTF. It is idempotent and skips anything already approved | `scripts/setup_allowances.py` = read state → print missing → confirm → call the SDK → print tx hashes. Safe to re-run. Drop the perps approval if the SDK allows (we don't trade perps) |
| Wallet types | `EOA` (0), `POLY_PROXY` (1), `POLY_GNOSIS_SAFE` (2), `POLY_1271` (3), plus an SDK "deposit wallet" (gasless, relayed) | Recommend a fresh **EOA**: simplest key custody. Needs a little POL for approval gas |
| Auth | L1 = EIP-712 wallet signature → create/derive API key. L2 = HMAC with key/secret/passphrase | Derive on first LIVE start, cache encrypted (Fernet, key derived from `SESSION_SECRET` + key-file salt) |
| Fees | Per-market `fd = {r: rate, e: exponent}` from `GET /clob-markets/{condition_id}`. Taker fee per share = `r · (p·(1−p))^e`. Builder fees are separate (we use none). Gamma also exposes `feesEnabled`, `feeSchedule`, `feeType` | Edge uses this exact formula per market. We **assume taker fees on every fill**, even for resting limits, until maker fee rules are confirmed |
| Market data | CLOB `/book`, `/books`, `/prices-history`, `/tick-size`, `/neg-risk`. Gamma `/events`, `/markets`. Data API `/positions`, `/trades`, `/v2/approvals` | As expected |
| Geoblock | `GET https://polymarket.com/api/geoblock` (from the brief). Not referenced in SDK | *Unverified* response schema. Treat any non-200 or unparseable reply as **blocked** |

### Open-Meteo

| API | Endpoint | Use |
|---|---|---|
| Ensemble | `ensemble-api.open-meteo.com/v1/ensemble` | Live members: `ecmwf_ifs025_ensemble` (51), `ncep_gefs025` (31). Also available: `ecmwf_aifs025_ensemble`, `icon_seamless_eps`, `gem_global_ensemble`, `ukmo_global_ensemble_20km`, `google_weathernext2_ensemble` |
| Previous Runs | (same host family) `temperature_2m_previous_dayN`, N=1..7, from 2021 | **Per-lead-time** deterministic history. The right input for lead-specific bias fitting |
| Historical Forecast | `historical-forecast-api.open-meteo.com/v1/forecast`, from ~2022 | Stitches the first hours of each run (≈ lead 0). Good as a "best forecast" series. **Not** lead-specific |
| Single Runs | `single-runs-api.open-meteo.com/v1/forecast?run=…` | A specific past run, full horizon. Needed for leakage-free backtests. *Unverified:* whether it serves ensemble models |
| Historical Weather | `archive-api.open-meteo.com/v1/archive` | ERA5 / IFS reanalysis on a grid. **Not station observations**, so not suitable as truth for bias fitting (see §3) |

## 2. Architecture

As specified in the brief. Key decisions:

```
          ┌──────────── worker (APScheduler, one process) ─────────────┐
 Gamma ──►│ registry ─► parser ─► forecast ─► weather_prob ─► edge     │
 CLOB  ──►│   (5 min)   (UNPARSED  (ensemble   (KDE + bias +   │       │
 OM/NWS ─►│              gate)      15 min)     rounding)      ▼       │
 IEM   ──►│                                     signals ◄── sizing      │
          │                                        │      risk.py ◄─┐   │
          │      Telegram bot (async, same loop) ◄─┤        │      │   │
          │                                        ▼        ▼      │   │
          │                        execution: scan | paper | live ─┘   │
          │                        reconcile (startup, every 2 min)    │
          └───────────────────────────────┬────────────────────────────┘
                                          │ SQLite WAL (/data)
          ┌───────── web (FastAPI) ───────┴───┐      caddy (HTTPS)
          │ Jinja2 + HTMX + Alpine + uPlot    │◄──── PUBLIC_DOMAIN
          └───────────────────────────────────┘
```

- **One source of truth for runtime state:** the `runtime_state` table (mode, halt, pause, overrides).
  The web and Telegram write **commands** to a `command` table. The worker is the only process that
  acts on them (mode change, cancel-all, close). This avoids two processes racing on the exchange.
  The web shows the command until the worker acknowledges it (≤ 2 s poll).
- **Kill switch:** the UI writes a `KILL` command *and* touches `/data/KILL`. The worker checks both every
  loop tick (1 s) before any order action.
- **Mode = execution adapter.** `scan` (no-op executor), `paper` (fills simulated against the live book
  snapshot: walk visible depth at or better than our limit, partial fills, taker fees), and `live`. All share
  `risk.py`, sizing and exits. SCAN imports nothing that needs a key.
- **Order idempotency:** `order_intent(id uuid, status=PENDING)` is committed *before* the API call. The
  intent id is stored with the exchange order id on ack. Startup and periodic reconcile match open exchange
  orders and trades to intents. An unknown exchange order is cancelled and alerted. A PENDING intent with no
  exchange order and age > 60 s is marked FAILED, never resent. The property test simulates crashes at every
  step.
- **Python 3.12**, `uv` lockfile, ruff + mypy (strict on `strategy/`, `risk.py`), pytest + hypothesis, respx for
  HTTP fixtures, GitHub Actions CI, `gitleaks` pre-commit hook.
- **Frontend:** Tailwind standalone CLI runs at Docker build time only. JS budget: htmx (~14 KB gz) +
  Alpine (~15 KB) + uPlot (~20 KB), well under 150 KB.

## 3. Probability model (weather)

**Target variable.** The integer the resolution source will display, *not* the true air temperature. Bias
correction is fitted against that same observation series, so systematic gaps (hourly vs. 6-hourly max,
°C→°F display rounding, station siting) are learned.

1. **Parse** → `MarketSpec(city, icao, lat, lon, elevation, tz, local_date, var=max|min, unit, buckets,
   source=WU|NOAA, precision=1)`. Stations come from a curated `stations.yaml` (ICAO, coordinates,
   elevation, tz). An unknown station → `UNPARSED`.
2. **Observations (truth).** Iowa Environmental Mesonet ASOS/METAR archive (`mesonet.agron.iastate.edu`),
   free and global, covering every ICAO above. Reduce to the resolution rule:
   - WU rule: max/min over all reports in the local day, temperature displayed in the market unit
     (METAR whole °C, or tenths from the US T-group, converted then rounded).
   - NOAA hourly rule: max/min over routine hourly reports only.
   For US stations, also cross-check recent days against `api.weather.gov/stations/{icao}/observations`.
3. **Forecast.** Every 15 min, fetch hourly `temperature_2m` members for ECMWF IFS 0.25° ENS and GEFS 0.25°
   at the station point, with `timezone=<station tz>`. Reduce each member to its local-day max/min. Record
   each model's run init time. A run older than `MAX_FORECAST_AGE_MIN` → stale, no trading.
   US only: the NWS point-forecast high/low is a sanity cross-check. Disagreeing with the ensemble median
   by more than 3σ → confidence LOW.
4. **Bias & spread (weekly, per station × model × lead day 1..7 × variable).** From Previous Runs (deterministic
   `temperature_2m_previous_dayN`, reduced to local-day max/min) versus the IEM truth over the last 365 days:
   fit mean error `μ_L`, error SD `σ_L`, and a seasonal term (sin/cos day-of-year) if it improves CV-CRPS.
   Live: member `x_i' = x_i − μ_L`. Spread: scale deviations about the ensemble mean by `k_L`, so predictive
   variance ≈ `σ_L²` on average. `k_L` is initialised from error variance versus logged ensemble variance,
   then refit from our own logged forecasts once ≥ 30 days exist.
5. **Distribution.** Per family, a Gaussian KDE on corrected members (Silverman bandwidth, floored at 0.3 °C).
   Families are blended by `MODEL_WEIGHTS`. P(displayed integer = n) = ∫ over [n−0.5, n+0.5) in the market
   unit. Buckets sum those integer masses, and open tails take the remainder. `assert |Σp − 1| < 1e-9`.
6. **Single-family guard.** Trade only if the edge's sign holds and is ≥ `MIN_EDGE/2` under *each* family
   alone.
7. **Calibration.** Log every (bucket, p_model, lead, market price). After resolution, record the outcome. UI
   shows a reliability diagram and Brier score per city × lead. If the rolling 30-day Brier > `MAX_BRIER`
   with n ≥ 20, the city is auto-disabled and an alert is sent.

## 4. Strategy, execution, risk

Follows the brief. Specifics:

- **Edge** for BUY YES at ask `a`: `p − a − fee(a)`, where `fee(a) = r·(a(1−a))^e`. BUY NO on a bucket uses
  the NO token's own book (not `1 − bid`). Max limit price = the largest tick `x` with
  `p − x − fee(x) ≥ MIN_EDGE`.
- **Sizing:** Kelly for a binary at price `x`: `f* = (p − x')/(1 − x')`, with `x' = x + fee(x)`. Stake =
  `KELLY_FRACTION · f* · bankroll`. Then risk clips, then round down to tick and size, and respect
  `minimumOrderSize`.
- **risk.py** is pure functions over a `RiskState` snapshot. Every executor calls `risk.check(order, state)`
  and gets `Approved(size)` or `Rejected(reason)`. Caps can only *shrink* an order. Hypothesis test: no
  random sequence of orders, fills or price moves can push any cap above its limit.
- **Exits** every `REEVAL_MINUTES`: take profit, edge reversal, otherwise hold. Auto-redeem uses the SDK
  redeem call. The SDK also has an `auto_redeem_operator`, *to evaluate*.

## 5. Backtest

- Markets: resolved weather events from Gamma (6–12 months).
- Prices: CLOB `/prices-history` per token (*unverified* for closed markets and at what fidelity).
  Fill model: the historical price at decision time, plus a conservative half-spread penalty
  (default 2¢), plus real fees from `fd`.
- Forecasts with no lookahead: Single Runs ensembles if available. Otherwise Previous Runs deterministic
  plus the per-lead Gaussian error fitted only on data *before* each decision date (expanding window). The
  report states which forecast path was used. Without real historical ensembles the backtest under-tests the
  KDE step.
- The report gives the bootstrap CI on ROI, and says plainly when n < 200 bets is too small to conclude
  anything.

## 6. CPI (phase 2, `ENABLE_CPI=false`)

Cleveland Fed inflation nowcast (`clevelandfed.org`) plus a Student-t error distribution fitted on historical
nowcast versus BLS first print, by days-before-release. Same pipeline. The module is built and tested on
fixtures only.

## 7. Hosts the runtime needs

`gamma-api.polymarket.com`, `clob.polymarket.com`, `data-api.polymarket.com`, `polymarket.com`,
`ensemble-api.open-meteo.com`, `previous-runs-api.open-meteo.com` (*hostname unverified*),
`single-runs-api.open-meteo.com`, `historical-forecast-api.open-meteo.com`, `archive-api.open-meteo.com`,
`api.weather.gov`, `mesonet.agron.iastate.edu`, `api.telegram.org`, a Polygon RPC, `www.clevelandfed.org`.

## 8. Risks and unknowns

1. Full rules text for the day boundary (local clock vs. standard time) — needs Gamma.
2. How collateral `0xC011…` is acquired from USDC — needs docs.
3. Maker vs. taker fee treatment for resting limit orders — needs docs. Until then, assume taker.
4. Whether `/prices-history` serves resolved markets at useful fidelity — needs CLOB.
5. Geoblock response schema — needs the endpoint.
6. `polymarket-client` is 0.x beta, so minor releases may break. Mitigated by pinning and an adapter layer.
7. Liquidity: mirrors report thin books on weather buckets, so a meaningful share of signals will be
   un-fillable at size. `MIN_LIQUIDITY_USD` and `MAX_BOOK_DEPTH_PCT` will bind often.

## 9. Build order after answers

1. Skeleton, config, DB, CI → 2. Gamma + parser with real fixtures → 3. Forecast + truth + bias → 4. Model +
calibration → 5. Edge, sizing, risk (property tests) → 6. SCAN end to end + signals UI → 7. Telegram →
8. PAPER + reconcile → 9. LIVE adapter + scripts → 10. Backtest → 11. Docker, Caddy, README.
Each phase ends with a report: what was built, what's tested, what's unverified.
