"""Calibration: Brier score, reliability bins, and per-city auto-disable."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

LEAD_BANDS: tuple[tuple[float, float, str], ...] = (
    (0, 24, "0-24h"),
    (24, 48, "24-48h"),
    (48, 72, "48-72h"),
    (72, 1e9, "72h+"),
)
MIN_N_FOR_DISABLE = 20


def lead_band(hours: float) -> str:
    for lo, hi, name in LEAD_BANDS:
        if lo <= hours < hi:
            return name
    return LEAD_BANDS[-1][2]


def brier(pairs: Iterable[tuple[float, int]]) -> tuple[float | None, int]:
    vals = [(p - o) ** 2 for p, o in pairs]
    return (sum(vals) / len(vals), len(vals)) if vals else (None, 0)


@dataclass(frozen=True)
class ReliabilityBin:
    lo: float
    hi: float
    n: int
    mean_p: float
    freq: float


def reliability(pairs: Sequence[tuple[float, int]], bins: int = 10) -> list[ReliabilityBin]:
    out: list[ReliabilityBin] = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        sel = [(p, o) for p, o in pairs if lo <= p < hi or (i == bins - 1 and p == 1.0)]
        if sel:
            out.append(
                ReliabilityBin(lo, hi, len(sel), sum(p for p, _ in sel) / len(sel), sum(o for _, o in sel) / len(sel))
            )
    return out


def should_disable(pairs: Sequence[tuple[float, int]], max_brier: float) -> bool:
    score, n = brier(pairs)
    return score is not None and n >= MIN_N_FOR_DISABLE and score > max_brier
