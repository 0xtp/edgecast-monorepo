"""Bucket probabilities for a daily temperature extreme.

Pipeline per model family:
  1. bias-correct each member: x_i' = x_i - mu_L (lead-specific mean error vs. the resolution source);
  2. Gaussian kernel density on the corrected members. The kernel bandwidth h is the largest of
     Silverman's rule, a floor, and whatever is needed to lift the total predictive variance
     (ensemble variance + h^2) up to the historical error variance sigma_L^2. Raw ensembles are
     under-dispersive at a single point, so this floor keeps the distribution honest;
  3. integrate over each bucket's continuous interval [lo - 0.5, hi + 0.5) (whole-degree display rounding).
Families are blended by MODEL_WEIGHTS (renormalised over the families present).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from app.forecast.bias import BiasFit, default_sigma, sigma_floor

SUM_TOL = 1e-9


@dataclass(frozen=True)
class FamilyInput:
    members: Sequence[float]  # local-day max or min per member, market unit
    bias: BiasFit | None


@dataclass
class BucketProbs:
    probs: list[float]  # blended, one per bucket (sums to 1)
    by_family: dict[str, list[float]] = field(default_factory=dict)
    family_stats: dict[str, dict[str, float]] = field(default_factory=dict)
    mean: float = math.nan
    sd: float = math.nan

    @property
    def n_families(self) -> int:
        return len(self.by_family)


_vec_erf = np.vectorize(math.erf, otypes=[float])


def _norm_cdf(z: np.ndarray) -> np.ndarray:
    return np.asarray(0.5 * (1.0 + _vec_erf(z / math.sqrt(2.0))), dtype=float)


def kde_bucket_probs(members: np.ndarray, h: float, edges: Sequence[float]) -> np.ndarray:
    """P(X in [e_k, e_{k+1})) for a Gaussian KDE. `edges` are sorted and start/end with -inf/+inf."""
    e = np.asarray(edges, dtype=float)
    finite = np.isfinite(e)
    cdf = np.empty((len(e), len(members)))
    cdf[~finite & (e < 0)] = 0.0
    cdf[~finite & (e > 0)] = 1.0
    if finite.any():
        z = (e[finite][:, None] - members[None, :]) / h
        cdf[finite] = _norm_cdf(z)
    p = np.diff(cdf.mean(axis=1))
    return np.clip(p, 0.0, 1.0)


def family_distribution(
    members: Sequence[float], bias: BiasFit | None, unit: str, lead_day: int
) -> tuple[np.ndarray, float]:
    x = np.asarray([m for m in members if m is not None and math.isfinite(m)], dtype=float)
    if len(x) < 2:
        raise ValueError("need at least two ensemble members")
    mu = bias.mu if bias else 0.0
    sigma = bias.sigma if bias else default_sigma(lead_day, unit)
    x = x - mu
    s = float(x.std(ddof=1))
    h_silverman = 1.06 * s * len(x) ** (-1 / 5) if s > 0 else 0.0
    floor = sigma_floor(unit) * 0.5
    h2 = max(h_silverman**2, floor**2, sigma**2 - s**2)
    return x, math.sqrt(h2)


def bucket_edges(buckets: Sequence[tuple[float, float]]) -> list[float]:
    """Continuous [lo, hi) intervals that tile the line -> the sorted edge list."""
    edges = [buckets[0][0]]
    for i, (lo, hi) in enumerate(buckets):
        if i and not math.isclose(lo, edges[-1]):
            raise ValueError("buckets must be contiguous")
        edges.append(hi)
    if edges[0] != -math.inf or edges[-1] != math.inf:
        raise ValueError("buckets must cover the whole line (open tails)")
    return edges


def bucket_probabilities(
    families: Mapping[str, FamilyInput],
    weights: Mapping[str, float],
    buckets: Sequence[tuple[float, float]],
    unit: str,
    lead_day: int,
) -> BucketProbs:
    edges = bucket_edges(buckets)
    by_family: dict[str, list[float]] = {}
    stats: dict[str, dict[str, float]] = {}
    total_w = 0.0
    blended = np.zeros(len(buckets))
    all_mean, all_var, all_w = 0.0, 0.0, 0.0
    for name, inp in families.items():
        w = float(weights.get(name, 0.0))
        if w <= 0 or len(inp.members) < 2:
            continue
        x, h = family_distribution(inp.members, inp.bias, unit, lead_day)
        p = kde_bucket_probs(x, h, edges)
        p = p / p.sum()
        by_family[name] = p.tolist()
        m, v = float(x.mean()), float(x.var(ddof=1) + h * h)
        stats[name] = {"n": float(len(x)), "mean": m, "sd": math.sqrt(v), "h": h}
        blended += w * p
        total_w += w
        all_mean += w * m
        all_var += w * (v + m * m)
        all_w += w
    if total_w == 0:
        raise ValueError("no usable model family")
    blended /= total_w
    total = float(blended.sum())
    assert abs(total - 1.0) < SUM_TOL, f"bucket probabilities sum to {total}"
    mean = all_mean / all_w
    sd = math.sqrt(max(all_var / all_w - mean * mean, 0.0))
    return BucketProbs(blended.tolist(), by_family, stats, mean, sd)


def lead_day_for(hours_to_end_of_day: float) -> int:
    """Lead day (1..7) used to look up the bias table."""
    return int(min(7, max(1, math.ceil(hours_to_end_of_day / 24.0))))
