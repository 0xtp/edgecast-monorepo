"""Phase 2 (ENABLE_CPI=false by default): price CPI buckets from the Cleveland Fed nowcast.

Model: actual = nowcast + error, error ~ Student-t(df) scaled by a sigma that depends on how many days
before the release the nowcast was taken. Sigma/df are fitted from historical nowcast-vs-first-print errors
(`fit_errors`). Buckets are in percentage points with the market's own rounding (usually one decimal).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from app.http import Http

CLEVELAND_FED_NOWCAST = "https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting"


@dataclass(frozen=True)
class CpiErrorModel:
    sigma_by_days: dict[int, float]  # days-before-release bucket -> scale (pp)
    df: float = 5.0

    def sigma(self, days_before: int) -> float:
        keys = sorted(self.sigma_by_days)
        for k in keys:
            if days_before <= k:
                return self.sigma_by_days[k]
        return self.sigma_by_days[keys[-1]]


DEFAULT_MODEL = CpiErrorModel({3: 0.06, 10: 0.08, 20: 0.10, 40: 0.13})


def _t_cdf(x: float, df: float) -> float:
    """Student-t CDF via the regularised incomplete beta function (continued fraction)."""
    if math.isinf(x):
        return 0.0 if x < 0 else 1.0
    t = df / (df + x * x)
    ib = _betainc(df / 2, 0.5, t)
    return 1 - 0.5 * ib if x > 0 else 0.5 * ib


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b + lbeta)
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1 - front * _betacf(b, a, 1 - x) / b


def _betacf(a: float, b: float, x: float) -> float:
    tiny, qab, qap, qam = 1e-300, a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + aa / c if abs(1 + aa / c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + aa / c if abs(1 + aa / c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1) < 3e-14:
            break
    return h


def bucket_probabilities(
    nowcast: float,
    days_before: int,
    buckets: Sequence[tuple[float, float]],
    model: CpiErrorModel = DEFAULT_MODEL,
    step: float = 0.1,
) -> list[float]:
    """`buckets` are inclusive displayed values (lo, hi) with +-inf tails; display rounds to `step`."""
    s = model.sigma(days_before)
    probs = []
    for lo, hi in buckets:
        a = -math.inf if math.isinf(lo) else lo - step / 2
        b = math.inf if math.isinf(hi) else hi + step / 2
        probs.append(_t_cdf((b - nowcast) / s, model.df) - _t_cdf((a - nowcast) / s, model.df))
    total = sum(probs)
    probs = [p / total for p in probs]
    assert abs(sum(probs) - 1) < 1e-9
    return probs


def fit_errors(samples: Sequence[tuple[int, float, float]], df: float = 5.0) -> CpiErrorModel:
    """samples: (days_before, nowcast, actual). Robust scale per days bucket (MAD * 1.4826)."""
    out: dict[int, float] = {}
    for k in sorted(DEFAULT_MODEL.sigma_by_days):
        errs = sorted(abs(a - n) for d, n, a in samples if d <= k)
        if len(errs) >= 8:
            out[k] = max(0.03, 1.4826 * errs[len(errs) // 2])
        else:
            out[k] = DEFAULT_MODEL.sigma_by_days[k]
    return CpiErrorModel(out, df)


class ClevelandFed:  # pragma: no cover - network; phase 2
    """Fetch the latest month-over-month CPI nowcast. The page layout is not guaranteed; verify before use."""

    def __init__(self, http: Http | None = None):
        self.http = http or Http("clevelandfed", "https://www.clevelandfed.org")

    def latest_cpi_mom(self) -> float:
        raise NotImplementedError(
            "Cleveland Fed nowcast scraping is not wired up: the host was unreachable during the build, so "
            "the data format could not be verified. Keep ENABLE_CPI=false."
        )
