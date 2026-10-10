"""Per-station, per-lead bias and error spread, fitted against the resolution-source observations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

MIN_SAMPLES = 30
SHRINK_N = 30  # shrink the mean error toward 0 by n / (n + SHRINK_N)


@dataclass(frozen=True)
class BiasFit:
    mu: float  # mean(forecast - observed), already shrunk
    sigma: float  # error standard deviation
    n: int


def default_sigma(lead_day: int, unit: str) -> float:
    """Prior error SD for a daily extreme when we have no history (°F; scaled for °C)."""
    sd_f = 1.8 + 0.5 * max(1, lead_day)
    return sd_f if unit == "F" else sd_f * 5 / 9


def sigma_floor(unit: str) -> float:
    return 1.0 if unit == "F" else 0.55


def fit(forecast: dict[date, float], observed: dict[date, int], lead_day: int, unit: str) -> BiasFit:
    errors = [forecast[d] - observed[d] for d in forecast.keys() & observed.keys()]
    n = len(errors)
    if n < MIN_SAMPLES:
        return BiasFit(0.0, default_sigma(lead_day, unit), n)
    mean = sum(errors) / n
    var = sum((e - mean) ** 2 for e in errors) / (n - 1)
    mu = mean * n / (n + SHRINK_N)
    # Robustness: blend the sample SD with the prior for modest n, and never go below the floor.
    w = n / (n + SHRINK_N)
    sigma = math.sqrt(w * var + (1 - w) * default_sigma(lead_day, unit) ** 2)
    return BiasFit(mu, max(sigma, sigma_floor(unit)), n)
