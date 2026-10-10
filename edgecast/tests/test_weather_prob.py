from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.forecast.bias import BiasFit, default_sigma, fit, sigma_floor
from app.models.weather_prob import (
    FamilyInput,
    bucket_edges,
    bucket_probabilities,
    family_distribution,
    kde_bucket_probs,
    lead_day_for,
)


def tiles(lo: int, hi: int, width: int = 2) -> list[tuple[float, float]]:
    out = [(-math.inf, lo - 0.5)]
    t = lo
    while t + width - 1 <= hi:
        out.append((t - 0.5, t + width - 0.5))
        t += width
    out.append((t - 0.5, math.inf))
    return out


@settings(max_examples=150, deadline=None)
@given(
    members=st.lists(st.floats(-40, 120, allow_nan=False), min_size=2, max_size=60),
    mu=st.floats(-5, 5),
    sigma=st.floats(0.3, 8),
    lo=st.integers(-20, 90),
    nb=st.integers(1, 12),
    width=st.sampled_from([1, 2]),
    unit=st.sampled_from(["F", "C"]),
)
def test_probabilities_always_sum_to_one(members, mu, sigma, lo, nb, width, unit):
    buckets = tiles(lo, lo + nb * width - 1, width)
    fams = {"ecmwf_ifs": FamilyInput(members, BiasFit(mu, sigma, 100)), "gfs": FamilyInput(members[::-1], None)}
    r = bucket_probabilities(fams, {"ecmwf_ifs": 0.6, "gfs": 0.4}, buckets, unit, 2)
    assert abs(sum(r.probs) - 1.0) < 1e-9
    assert all(0 <= p <= 1 for p in r.probs)
    for v in r.by_family.values():
        assert abs(sum(v) - 1) < 1e-9


def test_mass_concentrates_where_members_are():
    members = [75.0 + d for d in np.linspace(-0.4, 0.4, 51)]
    r = bucket_probabilities(
        {"ecmwf_ifs": FamilyInput(members, BiasFit(0, 1.0, 300))}, {"ecmwf_ifs": 1}, tiles(70, 79), "F", 1
    )
    best = int(np.argmax(r.probs))
    assert tiles(70, 79)[best] == (73.5, 75.5) or tiles(70, 79)[best] == (75.5, 77.5)
    assert r.n_families == 1


def test_bias_correction_shifts_distribution():
    members = [75.0] * 10 + [75.2] * 10
    base = bucket_probabilities({"gfs": FamilyInput(members, BiasFit(0, 1.5, 200))}, {"gfs": 1}, tiles(70, 79), "F", 1)
    warm = bucket_probabilities(
        {"gfs": FamilyInput(members, BiasFit(2.0, 1.5, 200))}, {"gfs": 1}, tiles(70, 79), "F", 1
    )
    assert warm.mean == pytest.approx(base.mean - 2.0, abs=1e-6)


def test_spread_floor_lifts_underdispersed_ensemble():
    x, h = family_distribution([75.0, 75.01, 74.99], BiasFit(0, 3.0, 300), "F", 2)
    total_var = float(np.var(x, ddof=1)) + h * h
    assert total_var == pytest.approx(9.0, rel=1e-3)
    _, h2 = family_distribution([60, 70, 80, 90], BiasFit(0, 1.0, 300), "F", 2)
    assert h2 > sigma_floor("F") * 0.5


def test_weights_renormalise_over_present_families():
    m = [75.0, 76.0, 74.0]
    r = bucket_probabilities(
        {"gfs": FamilyInput(m, None), "icon": FamilyInput([1.0], None)},
        {"gfs": 0.4, "ecmwf_ifs": 0.6, "icon": 0.2},
        tiles(70, 79),
        "F",
        1,
    )
    assert set(r.by_family) == {"gfs"}


def test_errors():
    with pytest.raises(ValueError):
        family_distribution([1.0], None, "F", 1)
    with pytest.raises(ValueError):
        bucket_edges([(-math.inf, 1.5), (2.5, math.inf)])
    with pytest.raises(ValueError):
        bucket_edges([(0.5, 1.5), (1.5, math.inf)])
    with pytest.raises(ValueError):
        bucket_probabilities({}, {"gfs": 1}, tiles(70, 79), "F", 1)


def test_kde_edges_and_lead_day():
    p = kde_bucket_probs(np.array([0.0]), 1.0, [-math.inf, 0.0, math.inf])
    assert p.tolist() == pytest.approx([0.5, 0.5])
    assert lead_day_for(1) == 1 and lead_day_for(25) == 2 and lead_day_for(1000) == 7 and lead_day_for(-5) == 1


def test_bias_fit():
    from datetime import date, timedelta

    days = [date(2026, 1, 1) + timedelta(d) for d in range(200)]
    fc = {d: 70.0 + (i % 7) for i, d in enumerate(days)}
    obs = {d: int(70 + (i % 7) - 1) for i, d in enumerate(days)}
    f = fit(fc, obs, 2, "F")
    assert f.n == 200 and 0.8 < f.mu < 1.0 and f.sigma >= sigma_floor("F")
    small = fit({days[0]: 1.0}, {days[0]: 0}, 3, "C")
    assert small.mu == 0 and small.sigma == pytest.approx(default_sigma(3, "C"))
