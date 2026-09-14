"""From a track of contact points to the contact instant (docs/design.md 4.2).

Two independent cues, each a fit on a per-frame series, each giving the
touchdown at sub-frame resolution:

**Shadow reach** — the rows of dark under the wheel before sunlit ground:
tyre, then the shadow of the fuselage, which is displaced from the aircraft
in proportion to the wheel's height. It shrinks linearly as the wheel comes
down and stops changing at the instant of contact; where it settles depends
on the sun, so the fit looks for the corner, not for zero. It needs no
calibration and no assumption about the ground, so when the sun is out it
is the primary cue.

**Apparent depth** — each frame's wheel pixel mapped through the homography
*as if* it were on the ground. While the wheel is airborne that assumption
fails in a specific way: a point above the ground projects further from the
camera than it is, by ``height / tan(elevation)``, metres per decimetre of
height at the few degrees a mast gives. So the across-strip coordinate
``Y(t)`` falls steadily until contact and then follows the ground run, whose
own trend is a straight line (the aircraft's heading against the calibration
axis, plus whatever the survey got wrong). ``Y(t)`` is therefore two lines
meeting at the touchdown. The fit here has a free slope on both legs, which
keeps a mediocre calibration from breaking it — but it also means a landing
(steep leg, then shallow) and a take-off (shallow, then steep) look alike:
the shadow, when there is one, settles that; without it the leg closer to
level is taken as the ground.

Both cues, when available, are compared, and a disagreement is flagged for
the judge rather than averaged away.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Frames needed on each side of the corner for the depth fit to say anything.
MIN_SIDE = 4
# Sub-frame grid for the corner search, in fractions of a frame interval.
SUBSTEPS = 8
# The airborne leg must change the apparent Y by at least this much (metres)
# relative to the ground leg, and by this many ground-run RMS residuals.
MIN_DROP_M = 1.0
MIN_DROP_SIGMAS = 4.0
# A hinge has to beat the best single line by this factor in RMS.
MIN_IMPROVEMENT = 0.8

# Shadow reach: the airborne leg must change it by at least this many px;
# a series flatter than this carries no timing information.
GAP_MIN_CHANGE_PX = 5.0
GAP_FLAT_PX = 4.0
# Frames needed on each side of the corner.
GAP_SIDE = 4
# Cues within this many frames are simply reported together; further
# apart than this they still bracket the contact and are averaged, beyond
# BRACKET_FRAMES they contradict each other.
AGREE_FRAMES = 4
BRACKET_FRAMES = 30
# Apparent-Y drift of a ground run this large can still be the aircraft's
# heading against the survey axis plus calibration error; only a steeper
# single-line trend is read as height changing.
DRIFT_MAX_MPS = 3.0


@dataclass(slots=True)
class Sample:
    """One frame's contact point, in time and on the ground plane."""

    index: int
    t: float  # seconds, pipeline clock
    world_x: float
    world_y: float  # signed so that higher above ground = larger
    u: float
    v: float
    gap_px: float | None = None  # lit ground between tyre and shadow
    reach_px: float | None = None  # dark under the belly before lit ground


@dataclass(slots=True)
class HingeFit:
    """The two-line explanation of Y(t), or the single line that beat it."""

    model: str  # hinge | line | level | none
    tc: float | None
    y0: float
    slope_before: float  # m/s of apparent Y on the first leg
    slope_after: float
    rms: float
    rms_line: float
    rms_level: float
    n_before: int
    n_after: int
    notes: list[str] = field(default_factory=list)

    @property
    def convex(self) -> bool:
        return self.slope_after > self.slope_before

    @property
    def ground_is_after(self) -> bool:
        """Which leg looks like the ground run: the one closer to level."""
        return abs(self.slope_after) <= abs(self.slope_before)


@dataclass(slots=True)
class GapFit:
    """What the shadow said."""

    model: str  # landing | departure | descending | climbing | flat | none
    tc: float | None
    slope_px_s: float  # of the airborne leg near the corner, px/s
    rms_px: float
    n_air: int
    n_ground: int
    notes: list[str] = field(default_factory=list)
    series: str = ""  # gap | reach


def _lstsq(design: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float]:
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    residual = y - design @ coef
    return coef, float(np.sum(residual**2))


# --------------------------------------------------------------------------
# apparent depth
# --------------------------------------------------------------------------


def fit_hinge(samples: list[Sample]) -> HingeFit:
    t_abs = np.array([s.t for s in samples], dtype=float)
    y = np.array([s.world_y for s in samples], dtype=float)
    n = len(t_abs)
    if n < 2:
        return HingeFit("none", None, float(y.mean()) if n else 0.0, 0, 0, 0, 0, 0, n, 0)
    t0 = t_abs[0]
    t = t_abs - t0

    level = float(y.mean())
    rms_level = float(np.sqrt(np.mean((y - level) ** 2)))
    line_coef, line_sse = _lstsq(np.column_stack([np.ones(n), t]), y)
    rms_line = float(np.sqrt(line_sse / n))

    best: tuple[float, np.ndarray, float] | None = None
    if n >= 2 * MIN_SIDE:
        step = float(np.median(np.diff(t))) / SUBSTEPS
        lo, hi = t[MIN_SIDE - 1], t[n - MIN_SIDE]
        for tc in np.arange(lo, hi + step / 2, step):
            d = t - tc
            design = np.column_stack([np.ones(n), np.minimum(d, 0.0), np.maximum(d, 0.0)])
            coef, sse = _lstsq(design, y)
            if best is None or sse < best[2]:
                best = (float(tc), coef, sse)

    if best is not None:
        tc, coef, sse = best
        rms = float(np.sqrt(sse / n))
        before = t < tc
        fit = HingeFit(
            model="hinge",
            tc=tc + t0,
            y0=float(coef[0]),
            slope_before=float(coef[1]),
            slope_after=float(coef[2]),
            rms=rms,
            rms_line=rms_line,
            rms_level=rms_level,
            n_before=int(before.sum()),
            n_after=int((~before).sum()),
        )
        # The airborne leg has to be a real departure from the ground leg.
        ground_slope = fit.slope_after if fit.ground_is_after else fit.slope_before
        air_slope = fit.slope_before if fit.ground_is_after else fit.slope_after
        air_span = (tc - t[0]) if fit.ground_is_after else (t[-1] - tc)
        drop = abs(air_slope - ground_slope) * air_span
        ground = ~before if fit.ground_is_after else before
        pred = coef[0] + coef[1] * np.minimum(t - tc, 0) + coef[2] * np.maximum(t - tc, 0)
        rms_ground = float(np.sqrt(np.mean((y - pred)[ground] ** 2))) if ground.any() else 0.0
        if drop < MIN_DROP_M:
            fit.notes.append(f"apparent drop only {drop:.2f} m")
        elif rms_ground > 0 and drop < MIN_DROP_SIGMAS * rms_ground:
            fit.notes.append(
                f"drop {drop:.2f} m is under {MIN_DROP_SIGMAS:.0f} sigma of the ground run"
            )
        elif rms > MIN_IMPROVEMENT * rms_line:
            fit.notes.append("a single line explains Y(t) about as well")
        else:
            return fit

    if rms_line < MIN_IMPROVEMENT * rms_level and abs(line_coef[1]) * (t[-1] - t[0]) >= MIN_DROP_M:
        return HingeFit(
            "line",
            None,
            float(line_coef[0]),
            float(line_coef[1]),
            float(line_coef[1]),
            rms_line,
            rms_line,
            rms_level,
            n,
            0,
        )
    return HingeFit("level", None, level, 0.0, 0.0, rms_level, rms_line, rms_level, n, 0)


# --------------------------------------------------------------------------
# shadow gap
# --------------------------------------------------------------------------


def _median3(values: np.ndarray) -> np.ndarray:
    if len(values) < 3:
        return values
    padded = np.pad(values, 1, mode="edge")
    smoothed: np.ndarray = np.median(np.lib.stride_tricks.sliding_window_view(padded, 3), axis=1)
    return smoothed


def _floor_hinge(
    t: np.ndarray, g: np.ndarray, *, rising: bool
) -> tuple[float, float, float, float] | None:
    """Best ``g = g0 + s * leg`` with a free floor; (tc, g0, s, sse), s > 0.

    ``leg`` is ``max(0, tc - t)`` for a landing (high, then flat) and
    ``max(0, t - tc)`` for a departure (flat, then rising).
    """
    n = len(t)
    if n < 2 * GAP_SIDE:
        return None
    step = float(np.median(np.diff(t))) / SUBSTEPS
    best: tuple[float, float, float, float] | None = None
    for tc in np.arange(t[GAP_SIDE - 1], t[n - GAP_SIDE] + step / 2, step):
        leg = np.maximum(0.0, (t - tc) if rising else (tc - t))
        coef, sse = _lstsq(np.column_stack([np.ones(n), leg]), g)
        if coef[1] <= 0:
            continue
        if best is None or sse < best[3]:
            best = (float(tc), float(coef[0]), float(coef[1]), sse)
    return best


def fit_shadow(samples: list[Sample]) -> GapFit:
    """The shadow's verdict: the tyre gap where it speaks, the reach otherwise.

    The gap (lit ground under the tyre) closes exactly at contact and is
    the precise cue; the reach (all the dark under the belly) is a coarser
    height proxy that also carries the wing's shadow, which goes on moving
    while the aircraft settles after contact - so it only decides when the
    gap cannot.
    """
    fine = fit_gap(samples, "gap")
    fine.series = "gap"
    if fine.model in ("landing", "departure"):
        return fine
    coarse = fit_gap(samples, "reach")
    coarse.series = "reach"
    if coarse.model in ("landing", "departure", "descending", "climbing"):
        if fine.model != "none":
            coarse.notes.append(f"tyre gap series was {fine.model}")
        return coarse
    return fine if fine.model != "none" else coarse


def fit_gap(samples: list[Sample], series: str = "gap") -> GapFit:
    """Explain one shadow series over time: a corner, a trend, or neither."""
    attr = "gap_px" if series == "gap" else "reach_px"
    have = [s for s in samples if getattr(s, attr) is not None]
    if len(have) < 2 * GAP_SIDE:
        return GapFit("none", None, 0.0, 0.0, 0, 0, [f"too few frames with a {series}"], series)
    t = np.array([s.t for s in have])
    g = _median3(np.array([getattr(s, attr) for s in have], dtype=float))
    n = len(t)

    spread = float(np.percentile(g, 90) - np.percentile(g, 10))
    if spread < GAP_FLAT_PX:
        # Flat means constant height, and where the series settles depends on
        # the sun, so flat alone cannot say "on the ground": the depth cue decides.
        return GapFit("flat", None, 0.0, float(g.std()), 0, n)

    line_coef, line_sse = _lstsq(np.column_stack([np.ones(n), t - t[0]]), g)
    rms_line = float(np.sqrt(line_sse / n))

    candidates: list[tuple[str, float, float, float, float]] = []
    for model, rising in (("landing", False), ("departure", True)):
        found = _floor_hinge(t, g, rising=rising)
        if found is None:
            continue
        tc, g0, slope, sse = found
        span = (t[-1] - tc) if rising else (tc - t[0])
        if slope * span < GAP_MIN_CHANGE_PX:
            continue
        candidates.append((model, tc, g0, slope, sse))

    if candidates:
        model, tc, g0, slope, sse = min(candidates, key=lambda c: c[4])
        rms = float(np.sqrt(sse / n))
        if rms <= MIN_IMPROVEMENT * rms_line:
            air = (t > tc) if model == "departure" else (t < tc)
            return GapFit(
                model,
                tc,
                -slope if model == "landing" else slope,
                rms,
                int(air.sum()),
                int((~air).sum()),
            )

    # No corner worth the name: one straight line - if it is one. A series
    # that flickers between two values has a large residual and no trend.
    slope = float(line_coef[1])
    change = slope * (t[-1] - t[0])
    if rms_line < 0.5 * abs(change):
        if change <= -GAP_MIN_CHANGE_PX:
            return GapFit("descending", None, slope, rms_line, n, 0)
        if change >= GAP_MIN_CHANGE_PX:
            return GapFit("climbing", None, slope, rms_line, n, 0)
    return GapFit("flat", None, 0.0, rms_line, 0, n)


# --------------------------------------------------------------------------
# the estimate
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ContactEstimate:
    outcome: str  # measured | short | long | on_ground | departure | airborne | unknown
    contact_t: float | None
    world_x: float | None  # calibration frame, before the direction sign
    world_y: float | None  # apparent-Y sign convention of the samples
    velocity_mps: float
    uncertainty_m: float
    bound_x: float | None
    method: str  # shadow | depth | none
    hinge: HingeFit
    gap: GapFit
    flags: list[str] = field(default_factory=list)


def _x_at(samples: list[Sample], tc: float, *, after: bool) -> tuple[float, float, float, int]:
    """Along-strip position at ``tc`` from the ground-run leg; (x, v, rms, n)."""
    pts = [s for s in samples if (s.t >= tc if after else s.t <= tc)]
    if len(pts) < 3:
        pts = samples
    t = np.array([s.t for s in pts]) - tc
    x = np.array([s.world_x for s in pts])
    coef, sse = _lstsq(np.column_stack([np.ones_like(t), t]), x)
    return float(coef[0]), float(coef[1]), float(np.sqrt(sse / len(pts))), len(pts)


def _frames(dt_s: float, fps: float) -> float:
    return abs(dt_s) * fps


def estimate(
    samples: list[Sample],
    *,
    entered_clipped: bool,
    exited_clipped: bool,
    fps: float = 60.0,
    calibration_residual_m: float = 0.0,
) -> ContactEstimate:
    hinge = fit_hinge(samples)
    gap = fit_shadow(samples)
    if len(samples) < 2 * MIN_SIDE:
        return ContactEstimate(
            "unknown", None, None, None, 0.0, 0.0, None, "none", hinge, gap, ["track too short"]
        )
    velocity = _x_at(samples, samples[0].t, after=False)[1]
    first, last = samples[0], samples[-1]
    flags: list[str] = []

    # -- decide what happened ------------------------------------------
    outcome = "unknown"
    tc: float | None = None
    method = "none"
    y_ground: float | None = None

    spread_t = 0.0
    if gap.model in ("landing", "departure"):
        method = "shadow"
        tc = gap.tc
        outcome = "measured" if gap.model == "landing" else "departure"
        if hinge.model == "hinge":
            y_ground = hinge.y0
        depth_agrees = (
            hinge.model == "hinge"
            and hinge.tc is not None
            and tc is not None
            and hinge.convex
            and hinge.ground_is_after == (gap.model == "landing")
        )
        if depth_agrees:
            assert hinge.tc is not None and tc is not None
            off = _frames(hinge.tc - tc, fps)
            if off <= BRACKET_FRAMES:
                # A flat flare has no sharp corner: the depth cue breaks
                # early (the descent eases off before contact) and the
                # shadow cue late (the wing unloads after it). Between them
                # is the best estimate, and their spread the honest error.
                method = "shadow+depth"
                spread_t = abs(hinge.tc - tc) / 2
                tc = (hinge.tc + tc) / 2
                if off > AGREE_FRAMES:
                    flags.append(f"shadow and depth cues {off:.0f} frames apart; taken between")
            else:
                flags.append(f"depth fit puts contact {off:.0f} frames away from the shadow")
    elif gap.model in ("descending", "climbing"):
        method = "shadow"
        outcome = "long" if gap.model == "descending" else "departure"
        flags.append(
            "still descending when it left the window (shadow)"
            if gap.model == "descending"
            else "climbing away for the whole track (shadow)"
        )
    elif hinge.model == "hinge" and hinge.tc is not None and hinge.convex:
        method = "depth"
        tc = hinge.tc
        y_ground = hinge.y0
        outcome = "measured" if hinge.ground_is_after else "departure"
        flags.append("no shadow: landing / take-off decided from the depth trend alone")
    elif hinge.model == "line" and abs(hinge.slope_before) > DRIFT_MAX_MPS:
        method = "depth"
        if hinge.slope_before < 0:
            outcome = "long"
            flags.append("still descending when it left the window (depth trend)")
        else:
            outcome = "departure"
            flags.append("climbing for the whole track (depth trend)")
    elif hinge.model in ("level", "line", "hinge"):
        # Level, or a drift-sized trend, or a corner bending the wrong way:
        # no descent to be seen. A wheel skimming the grass a hand's width
        # up and a wheel rolling on it look the same to this cue, so the
        # instant is left to the judge rather than guessed - a glider that
        # came in low and touched down in view must not be filed as
        # "landed before the window".
        method = "depth"
        outcome = "unseen"
        y_ground = hinge.y0
        flags.append(
            "no descent seen: rolling for the whole track, or skimming too low to tell "
            "- scrub to the contact frame and use it"
            if hinge.model != "hinge"
            else "depth trend bends the wrong way - no clean descent; check the frames"
        )

    # -- the number ------------------------------------------------------
    if outcome == "measured" and tc is not None:
        x, v, rms_x, n_x = _x_at(samples, tc, after=True)
        if method == "shadow":
            sigma_t = (
                (gap.rms_px / abs(gap.slope_px_s) / np.sqrt(max(1, gap.n_air)))
                if gap.slope_px_s
                else 1 / fps
            )
        else:
            drop_rate = abs(hinge.slope_before - hinge.slope_after)
            sigma_t = (
                (hinge.rms / drop_rate / np.sqrt(max(1, hinge.n_before))) if drop_rate else 1 / fps
            )
        sigma_t = max(sigma_t, spread_t, 0.25 / fps)
        sigma = float(
            np.sqrt(
                (abs(v) * sigma_t) ** 2 + (rms_x / np.sqrt(n_x)) ** 2 + calibration_residual_m**2
            )
        )
        if tc - first.t < 6 / fps:
            flags.append("touched down almost as it came into view")
        if last.t - tc < 4 / fps:
            flags.append("left the window right after touching down")
        return ContactEstimate(
            "measured", tc, x, y_ground, v, sigma, None, method, hinge, gap, flags
        )

    if outcome == "departure":
        return ContactEstimate(
            "departure", tc, None, y_ground, velocity, 0.0, None, method, hinge, gap, flags
        )
    if outcome == "long":
        return ContactEstimate(
            "long", None, None, None, velocity, 0.0, last.world_x, method, hinge, gap, flags
        )
    if outcome == "short":
        flags.append("already rolling when it entered the window")
        return ContactEstimate(
            "short", None, None, y_ground, velocity, 0.0, first.world_x, method, hinge, gap, flags
        )
    if outcome == "on_ground":
        return ContactEstimate(
            "on_ground",
            None,
            None,
            y_ground,
            velocity,
            0.0,
            first.world_x,
            method,
            hinge,
            gap,
            flags,
        )
    if outcome == "airborne":
        return ContactEstimate(
            "airborne", None, None, None, velocity, 0.0, None, method, hinge, gap, flags
        )
    if outcome == "unseen":
        return ContactEstimate(
            "unseen", None, None, y_ground, velocity, 0.0, first.world_x, method, hinge, gap, flags
        )
    return ContactEstimate(
        "unknown",
        None,
        None,
        None,
        velocity,
        0.0,
        None,
        method,
        hinge,
        gap,
        flags or ["could not interpret the track"],
    )
