# Edgecast

A self-hosted bot and dashboard that trades Polymarket prediction markets where an outside forecast gives a
measurable probability edge. Phase 1 is daily high/low temperature markets (one bucket market per whole-degree
range, per city per day). Phase 2 (CPI) is built but disabled.

```
Gamma ─► parser ─► Open-Meteo ensembles + station bias ─► bucket probabilities ─► edge after fees
                                                                                    │
                      SCAN: signals only  ·  PAPER: simulated fills  ·  LIVE: real limit orders
                                                                                    │
                    risk.py gate ─► execution (idempotent intents, reconcile) ─► exits ─► redemption
```

> **Read this first.** This software can lose money. Run SCAN, then PAPER, then LIVE with the smallest caps.
> Nothing here is financial advice.

## What is verified, and what is not

The build environment could not reach Polymarket, Open-Meteo, IEM or NWS (network policy). Everything was
built against the **official SDK source** (`polymarket-client` 0.13.0) and the **Open-Meteo docs source**, and
tested against synthetic fixtures in the documented shapes. Before trusting it with money, run these checks on
your server:

| Item | Status | How to verify |
|---|---|---|
| Parser on real markets | Tested on synthetic events built from observed patterns | `python scripts/inspect_markets.py --n 10 --out tests/fixtures/gamma` then `pytest tests/test_parser.py` (parses every captured event), and eyeball the table it prints |
| Day boundary (local clock vs standard time) | Assumed local clock day | Read the rules of a few markets in the captured fixtures |
| Fee endpoint shape (`/clob-markets/{id}` → `fd.r`, `fd.e`) | From SDK source | Markets show a fee on the Signals page; unknown fee = never traded |
| Geoblock response (`{"blocked": bool}`) | Assumed; any other shape counts as **blocked** | `curl -s https://polymarket.com/api/geoblock` |
| `previous-runs-api.open-meteo.com` hostname | Inferred from docs | Settings → Health shows API errors per host |
| Price history for resolved markets | Unverified | Run the backtest; "no price at decision time" counts appear in the report |
| How USDC becomes the collateral token `0xC011…2DFB` | Unverified | Deposit through polymarket.com once, or follow the current docs |
| LIVE order flow | Exercised against a fake exchange only | First LIVE session: minimum caps, watch History and the exchange UI side by side |

## Quick start (local, SCAN mode, no key needed)

```bash
uv venv -p 3.12 && uv pip install -e ".[live]" hypothesis pytest pytest-asyncio pytest-cov respx ruff mypy
cp .env.example .env
# set at least: SESSION_SECRET, UI_PASSWORD_HASH, DATABASE_URL=sqlite:///./data/edgecast.db, DATA_DIR=./data
python -m app.tools.hash_password            # paste the printed line into .env
python -c "import secrets;print(secrets.token_urlsafe(32))"   # SESSION_SECRET
python -m app.worker &                        # scheduler + Telegram
uvicorn --factory app.web.main:create_app --port 8000
```

Open http://localhost:8000. The worker syncs markets within seconds, fits station bias on first start (one
year of history per station; a few minutes), and produces signals after the first forecast pull (~40 s).

## Modes

| Mode | Orders | Key needed | P&L track |
|---|---|---|---|
| `SCAN` | none: signals with link, BUY YES/NO, model % vs market %, edge after fees, max limit price, suggested stake, confidence | no | Manual (your fills, logged with "I took this" or `/took`) |
| `PAPER` | simulated against the **live order book**: taker fills only, walking visible levels at or under the limit, never re-consuming the same snapshot, real fees | no | Bot (PAPER book) |
| `LIVE` | real limit orders through the official SDK | yes | Bot (LIVE book) |

- `MODE` in `.env` is only the startup default. The current mode is stored in the DB and wins after the first
  switch, unless `MODE_FORCE_ENV=true`.
- Any mode can switch to any other at runtime (dashboard segmented control, or `/scan` `/paper` `/live`),
  including SCAN → LIVE directly.
- **Into LIVE** requires `LIVE_CONFIRM=I_ACCEPT_LOSSES` in env, a key, a clear geoblock check, and typing
  `CONFIRM LIVE` (plus an inline confirm on Telegram). `REQUIRE_PAPER_BEFORE_LIVE=true` additionally blocks
  until PAPER has run `PAPER_MIN_DAYS`; otherwise you only get a warning.
- **Out of LIVE** is instant: new orders stop, and you choose: keep or cancel open orders, hold or close open
  positions.

## Safety

- **One risk gate.** Every order passes `app/risk.py`, which can only shrink or reject it. It enforces max stake
  per order, max exposure per market, per city per day, and total (resting orders count). A property test
  throws random order sequences at it.
- **Automatic halt.** A daily realised loss ≥ `MAX_DAILY_LOSS_USD` or a drawdown ≥ `MAX_DRAWDOWN_PCT` from the
  equity peak halts new entries; exits still run. You get warnings at 50% and 80% of the limit. Resume in the
  UI or with `/resume`.
- **Kill switch.** The dashboard button, `/kill`, or `touch /data/KILL` cancels all open orders and halts.
  Resume removes the file.
- **Pause.** `/pause` or the button stops entries; exits continue.
- **Geoblock.** Checked at start and hourly. Blocked or unknown means LIVE orders are refused, a red banner
  shows, and an alert is sent.
- **Execution rules.** Limit orders only, never more than `MAX_SLIPPAGE_CENTS` above the ask, and never more
  than `MAX_BOOK_DEPTH_PCT` of visible depth. Markets are skipped if the spread is above `MAX_SPREAD_CENTS` or
  liquidity is below `MIN_LIQUIDITY_USD`.
- **Fees.** Each market's fee parameters come from the CLOB (`fee = rate · (p(1−p))^exponent` per share) and
  enter every edge, limit price and Kelly size. Unknown fee = not traded.
- **Sanity.** Stale forecasts (> `MAX_FORECAST_AGE_MIN`), edges above `SANITY_MAX_EDGE`, a single model family
  carrying the edge, and unparsed markets are all skipped, and the reason is shown.
- **Idempotency.** An order intent row is committed *before* anything is sent. In LIVE the order is signed and
  its salt stored, then posted. If the outcome is unknown, the intent stays PENDING and reconciliation (at
  startup, before anything else, and every 2 min) adopts the matching exchange order or trade, or marks it
  FAILED. It never re-sends. Unknown exchange orders are cancelled and alerted.
- **Secrets.** The key comes only from env or a file, and is never logged, shown, or sent to the UI or
  Telegram. Derived CLOB API credentials are cached encrypted (Fernet, keyed from `SESSION_SECRET`). `.env` is
  git-ignored, and gitleaks runs in pre-commit and CI.

## Wallet setup (LIVE only)

Use a **brand-new wallet that only this bot uses**. Fund it with the bankroll plus a little POL for gas. Never
use your main wallet.

```bash
# 1. Create a key (prints address + key; store the key in a password manager, then in .env or a file)
python -c "from eth_account import Account; a=Account.create(); print(a.address); print(a.key.hex())"

# Recommended: keep it in a file readable only by you, and point to it
install -m 600 /dev/null secrets/poly.key && $EDITOR secrets/poly.key   # paste the 0x… key
# .env: POLY_PRIVATE_KEY_FILE=/run/secrets/poly.key  (mount it read-only), leave POLY_PRIVATE_KEY empty

# 2. Fund the address on Polygon:
#    - a little POL for gas (approvals are on-chain transactions for a plain EOA)
#    - the trading collateral. Per the official SDK, Polymarket's collateral token is
#      0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB (6 decimals), not USDC.e. Check docs.polymarket.com
#      for the current way to obtain it before funding.

# 3. Grant the exchange allowances the current contracts need (re-runnable; skips what is already set)
python scripts/setup_allowances.py
#    lists missing approvals → asks for confirmation → prints each tx hash → re-run shows "Nothing to do"

# 4. Later: sweep profits to your main wallet (WITHDRAW_TO_ADDRESS in .env; you must retype the address)
python scripts/withdraw.py --keep 200     # or --amount 50, or --all
```

With Docker, run the scripts inside the worker image:
`docker compose run --rm worker python scripts/setup_allowances.py`.

`POLY_FUNDER_ADDRESS` is only for trading through a Polymarket proxy or safe wallet; leave it empty for a plain
EOA. The SDK detects the wallet type, and `POLY_SIGNATURE_TYPE` is an optional consistency check. On first LIVE
start the bot derives CLOB API credentials from the key (L1 auth) and caches them encrypted.

## The probability model

1. **Parse** each event into city, station (ICAO from the resolution-source URL, cross-checked against the
   station named in the rules and `app/markets/stations.yaml`), local date, high/low, unit, and contiguous
   whole-degree buckets with open tails. Anything ambiguous is stored `UNPARSED` and never traded (see the Model
   page). Patterns are documented in `docs/market-anatomy.md`.
2. **Forecast:** Open-Meteo ensemble members (ECMWF IFS 0.25° ENS, 51 members; GEFS 0.25°, 31 members) at the
   station, in the market's unit, reduced to the local-day max/min. For US stations, the NWS point forecast is
   a cross-check: disagreement > 3σ forces LOW confidence.
3. **Station bias (weekly):** Open-Meteo Previous Runs (lead-specific, 1–7 days) against **station
   observations** from the IEM METAR archive, reduced with the market's own rule (Wunderground = all reports;
   NOAA = routine hourly reports; whole-degree display). The fit is per station × model × variable × lead:
   mean error (shrunk toward 0) and error SD.
4. **Distribution:** bias-corrected members, Gaussian KDE with bandwidth lifted so the predictive variance is at
   least the historical error variance (raw ensembles are under-dispersed at a point). Families are blended by
   `MODEL_WEIGHTS` and integrated over `[lo−0.5, hi+0.5)` per bucket. The sum to 1 is asserted.
5. **Calibration:** every prediction is scored after resolution. The Model page shows the reliability diagram and
   Brier score by city × lead. A city whose 30-day Brier exceeds `MAX_BRIER` (n ≥ 20) is auto-disabled.

## Strategy

- Edge = `p_model − ask − fee(ask)` for BUY YES; BUY NO is evaluated on the NO token's own book. Trade if edge ≥
  `MIN_EDGE` **and** every model family alone shows at least half that edge.
- Limit = min(highest tick that still clears `MIN_EDGE`, ask + slippage).
- Size = `KELLY_FRACTION` × Kelly (with fees) × current bankroll, then clipped by depth and every risk cap, and
  rounded down.
- Entries only between `ENTRY_MIN_HOURS` and `ENTRY_MAX_HOURS` before the observation day ends, and not within
  `NO_NEW_ENTRY_HOURS_BEFORE_CLOSE` of trading close.
- Exits every `REEVAL_MINUTES`: take profit when bid ≥ p_model − `TP_BUFFER`; edge reversal when a new forecast
  puts p_model below the bid by `EXIT_EDGE`; otherwise hold. Every exit records its reason. Winning LIVE
  positions are redeemed automatically.

## Risk settings

Defaults are in `.env.example`, with a comment on every line. These keys can be changed at runtime from the
Settings page or `/set` (validated, audited, shown as "runtime" with a reset-to-env button):

`MIN_EDGE`, `KELLY_FRACTION`, `MAX_STAKE_PER_ORDER_USD`, `MAX_DAILY_LOSS_USD`, `MAX_TOTAL_EXPOSURE_USD`,
`MAX_EXPOSURE_PER_MARKET_USD`, `MAX_EXPOSURE_PER_CITY_DAY_USD`, `CITY_ALLOWLIST`, `CITY_BLOCKLIST`,
`TELEGRAM_NOTIFY_*`.

Risk limits can never exceed the env `HARD_MAX_*` ceilings. Nothing at runtime can reveal the key, change the
wallet, or withdraw.

## Telegram

1. Message **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Message **@userinfobot** to get your numeric user ID. Put it in `TELEGRAM_ALLOWED_USER_IDS` and
   `TELEGRAM_CHAT_ID`.
3. Set `TELEGRAM_ENABLED=true` and restart the worker. Send `/status` to your bot.

Everyone not on the allow list is ignored, and the attempt is logged. `/live`, `/kill`, `/closeall`, `/close` and
risk `/set` need an inline confirm that expires in 60 s, and `/live` also needs the typed phrase. Every command
is in the Audit log.

Commands: `/status` `/opps [n]` `/positions` `/manual` `/history [n]` `/scan` `/paper` `/live` `/pause`
`/resume` `/kill` `/closeall` `/close <id>` `/took <signal_id> <price> <usd>` `/set <key> <value>` `/config`
`/help`.

Push alerts:
- signals (with Took it / Ignore buttons);
- orders, exits, resolutions;
- risk events;
- health (geoblock, stale data, job failures, worker heartbeat lost);
- a daily summary at `TELEGRAM_DAILY_SUMMARY_TIME`.

`TELEGRAM_QUIET_HOURS` mutes everything except risk and health.

## Deployment

One small VPS (2 vCPU / 2–4 GB) in a non-restricted region. Polymarket's docs name AWS **eu-west-1 (Ireland)** as
the closest non-restricted region to their servers. **Verify from the server before funding anything:**

```bash
curl -s https://polymarket.com/api/geoblock        # must not say blocked
```

```bash
# Ubuntu 24.04
curl -fsSL https://get.docker.com | sh
git clone <your fork> edgecast && cd edgecast
cp .env.example .env && $EDITOR .env      # SESSION_SECRET, UI_PASSWORD_HASH (single-quoted), PUBLIC_DOMAIN, TZ, …
# point PUBLIC_DOMAIN's DNS A record at the server, open ports 80/443
docker compose up -d                       # worker → web (waits for a healthy worker) → caddy (HTTPS)
docker compose logs -f worker
```

Then open `https://PUBLIC_DOMAIN` and log in.

Suggested run order (not enforced unless `REQUIRE_PAPER_BEFORE_LIVE=true`):
1. **SCAN** for 1 week: check that signals look sane and parsing is right on the Model page.
2. **PAPER** for 2–4 weeks.
3. Review the backtest and paper results together.
4. **LIVE** with the minimum caps.

SCAN can stay on permanently as a manual-trading signal feed.

**Operations:**
- **Backups:** nightly SQLite backup (online backup API) to `/data/backups`, 14 kept. Optional S3 upload with
  `BACKUP_S3_URI` needs the `aws` CLI.
- **Logs:** rotated (`/data/logs`, plus Docker's json-file rotation).
- **Health:** `/healthz` reports DB and worker-heartbeat age. Compose healthchecks restart unhealthy
  containers. The web process sends a Telegram alert if the worker heartbeat stops for 10 minutes.
- **Postgres:** set `DATABASE_URL=postgresql+psycopg://…` and `pip install ".[postgres]"`.

## Backtest

```bash
python -m backtest.runner --months 9                 # or: docker compose run --rm worker python -m backtest.runner
```

The backtest replays resolved weather markets without lookahead:
- Previous-runs forecasts issued at least 6 h before each decision.
- Bias fitted only on days already resolved.
- CLOB price history plus a spread penalty, and today's fees.

Reports (HTML + JSON) appear on the Backtests page. They cover bets, hit rate, ROI with a bootstrap CI, max
drawdown, a Sharpe-like ratio, P&L by city and lead, and model vs market Brier. Every report lists its
limitations (deterministic + Gaussian instead of the ensemble KDE, no order-book replay, hold to resolution) and
says plainly when the sample is too small to conclude anything (< 200 bets or < 60 days). **No backtest report
ships in this repo**: the build environment had no access to the data.

## Development

```bash
ruff check . && ruff format --check .
mypy app/strategy app/risk.py                      # strict
pytest --cov=app && python scripts/coverage_gate.py  # ≥85% on parser, models, strategy, risk, execution
pre-commit install                                  # gitleaks + ruff
# CSS (only when templates change; Docker builds it automatically):
tailwindcss -c tailwind.config.js -i app/web/assets/input.css -o app/web/static/css/app.css --minify
```

Tests include:
- property tests: bucket probabilities sum to 1; risk caps hold under random order sequences; restart
  reconciliation never duplicates under random crash points; the ledger reconciles exactly;
- a PAPER end-to-end integration test;
- a compressed 24-hour PAPER soak (288 cycles) that must reconcile to the micro-dollar.

Layout: `app/` (config, db, polymarket, markets, forecast, models, strategy, execution, services, web, worker),
`backtest/`, `scripts/`, `tests/`, `docs/`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Red "Geoblocked" banner | The server's IP is in a restricted region. Move the VPS; the bot refuses LIVE orders until it clears |
| "Geoblock status unknown" | The worker hasn't checked yet, or the endpoint changed shape; check `docker compose logs worker` |
| No signals | Model page: are markets parsed? Settings → Health: forecast pull age and API errors. Also check the entry window and `CITY_ALLOWLIST` |
| Many UNPARSED markets | Polymarket changed wording or a new station appeared; add it to `app/markets/stations.yaml` and capture fixtures |
| "fee rate unknown" | The CLOB fee lookup failed or the market reports no fee data while `feesEnabled` is true; the market is skipped on purpose |
| Login says UI_PASSWORD_HASH not set | Run `python -m app.tools.hash_password` and keep the single quotes in `.env` (compose expands `$`) |
| Halted after restart | Check the Audit log: a KILL file, daily loss/drawdown breach, or a failed startup reconcile. Fix, then Resume |
| LIVE: "needs the 'live' extra" | `pip install -e ".[live]"` (the Docker image includes it) |
| Telegram silent | `/status` from an allowed ID; Settings → Health → Telegram; quiet hours; `TELEGRAM_NOTIFY_*` toggles |
