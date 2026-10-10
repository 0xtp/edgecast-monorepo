# Polymarket weather market anatomy

Status: **provisional, not yet verified against the Gamma API.**

The Phase 0 brief asks for 10 live and 10 resolved weather markets pulled from the Gamma API.
The build container's network policy blocks `gamma-api.polymarket.com`, `clob.polymarket.com`,
`polymarket.com` and `docs.polymarket.com`. That table could not be produced from the API. The rows below
come from web-search snippets of Polymarket market pages and third-party mirrors (polyguana.com,
lines.com) taken on 2026-10-10. Treat each row as a lead to confirm. Do not treat any of them as a fixture.

To generate the real table and save the raw payloads as test fixtures, once the hosts are reachable:

```bash
python scripts/inspect_markets.py --n 10 --out tests/fixtures/gamma > docs/market-anatomy.generated.md
```

The parser's unit tests will be built from those fixtures, not from this page.

## What was observed

| # | Market (as titled) | Type | Unit | Station (ICAO) | Resolution source | Bucket style | Evidence |
|---|---|---|---|---|---|---|---|
| 1 | Highest temperature in London on May 12, 2026 | high | °C | London City Airport (EGLC) | Wunderground daily history | 1 °C points, open tails | search snippet, polymarket.com |
| 2 | Highest temperature in London on May 19, 2026 | high | °C | EGLC | Wunderground | "be 21°C" (single degree) | mirror (polyguana) |
| 3 | Highest temperature in London (2025 series) | high | °F (whole degrees) | EGLC | Wunderground | — | search snippet, rules text quoted |
| 4 | Highest temperature in Houston on October 9, 2026 | high | °F | William P. Hobby Airport (KHOU) | **NOAA** (not Wunderground) | — | search snippet, rules text quoted |
| 5 | Highest temperature in NYC on July 27, 2026 | high | °F | LaGuardia (KLGA) | Wunderground KLGA history | 2 °F ranges, e.g. `78-79°F` | URL slug + snippet |
| 6 | Highest temperature in NYC on September 18, 2026 | high | °F | KLGA | NOAA | resolved to `80-81°F` | lines.com |
| 7 | Highest temperature in NYC on September 28, 2026 | high | °F | KLGA | **NOAA KLGA timeseries (hourly)** | 2 °F ranges | search snippet |
| 8 | Highest temperature in NYC (2025 series) | high | °F | KLGA | Wunderground | open tail `76°F or higher` | search snippet |
| 9 | Lowest temperature in Paris (2026) | low | °C | Paris–Le Bourget (LFPB) | Wunderground | 1 °C | mirror (polyguana) |
| 10 | Lowest temperature in Tokyo / London / NYC / Seoul (2026) | low | mixed | RJTT Haneda, EGLC, KLGA, RKSI Incheon | Wunderground | — | mirror (polyguana) |

Not observed yet: any market's `clobTokenIds`, `negRisk` flag, `feesEnabled`/`feeSchedule`, tick size, `endDate`
versus local-day boundary, or full rules text. All of these need the API.

## Patterns (provisional)

1. **Structure.** Each city-date is one *event* with one binary market per bucket ("Will the highest
   temperature in NYC be 78-79°F on July 27?"). The URL slug is
   `highest-temperature-in-<city>-on-<month>-<day>[-<year>]/<...>-<lo>-<hi>f`. Buckets are mutually exclusive
   and exhaustive. This is almost certainly a neg-risk event. **Verify `negRisk`**, because it changes which
   exchange contract signs the order.
2. **Two variables.** `highest temperature` (daily max) and `lowest temperature` (daily min).
3. **Units vary by city.** US cities use °F. London, Paris, Tokyo and Seoul use °C. London changed from °F
   (2025) to °C (2026). The unit must be read from each market. It cannot be inferred from the city.
4. **Bucket width.** °F markets use 2-degree ranges (`78-79°F`, inclusive both ends in whole degrees).
   °C markets use single degrees (`21°C`). Both have open tails (`76°F or higher`, `X or below`).
5. **Precision and rounding.** Rules say the source "measures temperatures to whole degrees" and that this
   precision is used. The value used is the integer the source displays. We do not round a decimal ourselves.
   The model therefore predicts a distribution over *displayed integers*.
6. **Resolution source is per market and has changed over time.**
   - *Wunderground daily history page* for the station: "highest temperature recorded for all times on this
     day … once information is finalized". It cannot resolve Yes until the day's data is final. Later
     revisions are ignored.
   - *NOAA station timeseries* (Houston KHOU, NYC KLGA from roughly September 2026): the basis is hourly
     observations. It resolves once the first data point for the following date is published, or by 23:59 ET
     the next day. Revisions count until then.
   - Consequence: "the high" under a NOAA-hourly rule is the max of reported observations. It can sit
     0.5–1.5 °F below the true instantaneous max, which a 6-hour METAR max group would catch. The bias model
     is fitted per station against **the same observation type the market resolves on**, so this gap is
     learned rather than ignored (see PLAN.md).
7. **Station ≠ city centre.** NYC resolves on LaGuardia, not Central Park (Kalshi uses Central Park).
   London resolves on London City, not Heathrow. Forecasts must be pulled at the station's coordinates.
8. **Day boundary.** Implied to be the station's local calendar day. Not confirmed: whether the day follows
   local standard time all year (as the NWS climate report does) or local clock time. Wunderground's history
   page uses local clock time. **Open question, needs full rules text.**
9. **Editorial noise.** Some event pages and mirrors carry AI-written commentary that cites other stations.
   The parser reads only the rules/description field and `resolutionSource`. Ambiguity → `UNPARSED`.

## Parser contract (derived from the above)

A market is tradeable only if every field below is extracted with high confidence. Otherwise it is
`UNPARSED`.

| Field | Source | Failure → UNPARSED when |
|---|---|---|
| city, variable (high/low), local date | title + rules | title regex fails or date ambiguous |
| station name → ICAO, lat/lon | rules text, mapped through a curated `stations.yaml` | station not in the curated table |
| resolution source kind | `resolutionSource` URL host + rules | not Wunderground or NOAA |
| unit | bucket labels + rules | labels disagree with rules |
| bucket edges `[lo, hi]` incl. open tails | `groupItemTitle` per market | gaps/overlaps across the event's buckets |
| precision | rules ("whole degrees") | any other precision |
| finalisation rule | rules | unrecognised wording |
