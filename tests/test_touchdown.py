"""The fits that turn a track into a contact instant, on synthetic tracks."""

from __future__ import annotations

import numpy as np
import pytest

from touchdown_analyzer.analysis import touchdown as td

FPS = 60.0


def track(
    n: int,
    *,
    tc: float | None,
    drift: float = 0.0,
    sink: float = 6.0,
    x_speed: float = 24.0,
    reach: tuple[float, float] | None = None,
    noise: float = 0.05,
    seed: int = 1,
) -> list[td.Sample]:
    """A glider crossing the window, touching down at ``tc`` seconds.

    Apparent Y falls at ``sink`` m/s of apparent depth until contact and then
    follows ``drift``; the shadow reach falls to a floor at the same instant.
    """
    rng = np.random.default_rng(seed)
    samples = []
    for i in range(n):
        t = i / FPS
        air = tc is not None and t < tc
        y = 3.0 + drift * t + (sink * (tc - t) if air else 0.0) + rng.normal(0, noise)
        gap = None
        if reach is not None:
            floor, rate = reach
            gap = floor + (rate * (tc - t) if air else 0.0) + rng.normal(0, 1.0)
        samples.append(
            td.Sample(
                index=i,
                t=t,
                world_x=-20 + x_speed * t + rng.normal(0, 0.1),
                world_y=y,
                u=100 + 25 * i,
                v=730.0,
                reach_px=gap,
            )
        )
    return samples


def test_depth_hinge_finds_the_corner() -> None:
    fit = td.fit_hinge(track(90, tc=0.8))
    assert fit.model == "hinge"
    assert fit.tc == pytest.approx(0.8, abs=0.03)
    assert fit.convex and fit.ground_is_after


def test_depth_hinge_tolerates_ground_drift() -> None:
    """A mediocre calibration makes the ground run slope; the corner survives."""
    fit = td.fit_hinge(track(90, tc=0.7, drift=1.5))
    assert fit.model == "hinge"
    assert fit.tc == pytest.approx(0.7, abs=0.03)
    assert fit.slope_after == pytest.approx(1.5, abs=0.4)


def test_level_track_is_not_a_landing() -> None:
    fit = td.fit_hinge(track(60, tc=None))
    assert fit.model in ("level", "line")
    # low and level throughout: rolling, or skimming - not the estimator's call
    est = td.estimate(track(60, tc=None), entered_clipped=True, exited_clipped=True)
    assert est.outcome == "unseen"


def test_shadow_reach_gives_the_instant_and_wins() -> None:
    samples = track(90, tc=0.75, reach=(10.0, 60.0))
    est = td.estimate(samples, entered_clipped=False, exited_clipped=True)
    assert est.outcome == "measured"
    assert est.method in ("shadow", "shadow+depth")
    assert est.contact_t == pytest.approx(0.75, abs=0.04)
    # x at contact: -20 + 24 * 0.75
    assert est.world_x == pytest.approx(-2.0, abs=0.5)
    assert est.uncertainty_m < 1.0


def test_still_descending_at_the_edge_is_long() -> None:
    samples = track(40, tc=2.0, reach=(10.0, 60.0))  # contact well after the last frame
    est = td.estimate(samples, entered_clipped=False, exited_clipped=True)
    assert est.outcome == "long"
    assert est.bound_x == pytest.approx(samples[-1].world_x)


def test_departure_is_recognised_from_the_shadow() -> None:
    samples = track(90, tc=None, reach=(10.0, 0.0))
    # ground run, then the reach grows: lift-off at 0.7 s
    for s in samples:
        if s.t > 0.7:
            s.reach_px = 10.0 + 60.0 * (s.t - 0.7)
            s.world_y = 3.0 + 6.0 * (s.t - 0.7)
    est = td.estimate(samples, entered_clipped=True, exited_clipped=True)
    assert est.outcome == "departure"
    assert est.contact_t == pytest.approx(0.7, abs=0.05)


def test_disagreeing_cues_are_bracketed_and_flagged() -> None:
    samples = track(120, tc=1.0)
    # the shadow corner 15 frames later than the depth corner, as a settling wing does
    for s in samples:
        s.reach_px = 10.0 + max(0.0, 60.0 * (1.25 - s.t))
    est = td.estimate(samples, entered_clipped=False, exited_clipped=True)
    assert est.outcome == "measured"
    assert est.method == "shadow+depth"
    assert 1.0 < (est.contact_t or 0) < 1.25
    assert any("apart" in f for f in est.flags)


def test_short_track_is_unknown() -> None:
    est = td.estimate(track(5, tc=0.05), entered_clipped=False, exited_clipped=False)
    assert est.outcome == "unknown"
