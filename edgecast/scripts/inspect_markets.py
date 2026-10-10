"""Snapshot live and resolved Polymarket weather markets from the Gamma API.

Phase-0 research tool. It records the raw Gamma payloads as test fixtures and
prints a Markdown table (question, buckets, resolution source, unit, rules
excerpt) for docs/market-anatomy.md.

Stdlib only so it runs before the project's dependencies are installed:

    python scripts/inspect_markets.py --n 10 --out tests/fixtures/gamma

Read-only: it calls public Gamma endpoints and never needs a key.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

GAMMA = "https://gamma-api.polymarket.com"
TITLE_RE = re.compile(r"\b(highest|lowest)\s+temperature\s+in\b", re.I)
UNIT_RE = re.compile(r"°\s*([FC])")
STATION_RE = re.compile(r"recorded (?:by \w+ )?at (?:the )?(.+?(?:Airport|Station)[^.,]*)", re.I)
SOURCE_HOST_RE = re.compile(r"https?://([^/\s]+)")


def get(path: str, **params: Any) -> Any:
    url = f"{GAMMA}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "edgecast-research/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def weather_events(closed: bool, want: int) -> list[dict[str, Any]]:
    """Page through weather-tagged events until `want` temperature events are found."""
    found: list[dict[str, Any]] = []
    offset = 0
    while len(found) < want and offset < 2000:
        page = get(
            "/events",
            tag_slug="weather",
            closed=str(closed).lower(),
            limit=100,
            offset=offset,
            order="endDate",
            ascending="false" if closed else "true",
        )
        if not page:
            break
        found += [e for e in page if TITLE_RE.search(e.get("title", ""))]
        offset += 100
    return found[:want]


def summarise(event: dict[str, Any]) -> dict[str, Any]:
    markets = event.get("markets") or []
    rules = event.get("description") or (markets[0].get("description") if markets else "") or ""
    source = event.get("resolutionSource") or (markets[0].get("resolutionSource") if markets else "")
    buckets = [m.get("groupItemTitle") or m.get("question") for m in markets]
    unit = UNIT_RE.search(" ".join(filter(None, buckets)) + rules)
    station = STATION_RE.search(rules)
    host = SOURCE_HOST_RE.search(source or rules)
    return {
        "slug": event.get("slug"),
        "title": event.get("title"),
        "end": event.get("endDate"),
        "neg_risk": event.get("negRisk"),
        "unit": unit.group(1) if unit else "?",
        "station": station.group(1).strip() if station else "?",
        "source_host": host.group(1) if host else "?",
        "source_url": source,
        "buckets": buckets,
        "fees_enabled": [m.get("feesEnabled") for m in markets][:1],
        "rules": rules,
    }


def md_table(rows: list[dict[str, Any]]) -> str:
    out = ["| Title | Unit | Station | Source | Buckets | End (UTC) |", "|---|---|---|---|---|---|"]
    for r in rows:
        buckets = ", ".join(r["buckets"][:3]) + (" … " + r["buckets"][-1] if len(r["buckets"]) > 3 else "")
        out.append(f"| {r['title']} | °{r['unit']} | {r['station']} | {r['source_host']} | {buckets} | {r['end']} |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--out", type=Path, default=Path("tests/fixtures/gamma"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    for label, closed in (("live", False), ("resolved", True)):
        events = weather_events(closed, args.n)
        (args.out / f"weather_events_{label}.json").write_text(json.dumps(events, indent=2))
        rows = [summarise(e) for e in events]
        print(f"\n## {label.title()} ({len(rows)})\n")
        print(md_table(rows))
        for r in rows:
            print(f"\n### {r['title']}\n\nSource: {r['source_url']}\n\n> " + r["rules"].replace("\n", "\n> "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
