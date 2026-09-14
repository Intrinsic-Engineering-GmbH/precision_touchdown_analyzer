"""The main-wheel contact point and the shadow gap, per frame (design 4.2).

Everything downstream maps the contact pixel through the ground-plane
homography, so it has to be the wheel — a point on the fuselage 0.8 m up is
off by metres through the same mapping, and a point in the *shadow* is off
by whatever the sun decides.

Step one is therefore a silhouette that knows the difference between the
aircraft and its shadow. MOG2's own shadow flag misses a hard sunlit shadow
on grass, so the split is done here, at full resolution inside the tracked
box, against MOG2's background image: a foreground pixel that is a darker
version of the background *with the same chroma* is shadow; anything else
(white fuselage, red markings, the black tyre — which is achromatic, not
green) is aircraft.

Step two takes the belly line of the aircraft mask — the lowest pixel of
each column — maps every point to the ground plane and keeps the one that
lands *closest to the camera*. A point above the ground projects further
away along its ray, so of all belly-line points the wheel, the one nearest
the ground, maps nearest. This is also independent of the image tilt of the
strip, which a plain "lowest pixel" rule is not: over a 6 m fuselage the
ground line here falls by ~10 px, enough to pick the tail skid instead.

Step three reads the shadow: how many rows of dark (tyre, then shadow)
lie under the belly at the wheel before sunlit ground begins. The shadow
of the fuselage is displaced from the fuselage by an amount proportional
to the height of the wheel, so that run shrinks linearly as the wheel comes
down and stops changing at the instant of contact. Where it stops depends
on the sun (the tyre, plus whatever shadow still lies under the wheel on
the ground), which is why the fit downstream looks for the *corner* of the
series, not for zero. It needs no calibration at all and is the most
precise cue there is whenever the sun is out (design 4.2).
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from touchdown_analyzer.analysis.detect import Blob
from touchdown_analyzer.calibration import homography as hg

# Margin around the coarse blob box, full-resolution px, so that a shadow
# hanging below the wheel is inside the region that gets classified.
ROI_MARGIN = 40
# Foreground: summed absolute BGR difference against the background.
FG_DIFF = 45.0
# Shadow: this much darker than the background at most / at least, and with
# chroma (channel proportions) within this L1 distance of the background.
SHADOW_RATIO_MAX = 0.92
SHADOW_RATIO_MIN = 0.20
SHADOW_CHROMA = 0.10

# Fraction of the silhouette width, centred, in which the wheel is looked for.
# The main wheel sits under the wing root; the tail is ~3.5 m off centre on a
# 15 m span, so 0.6 keeps both tail and wingtips out.
CENTRAL_BAND = 0.6
SMOOTH_COLUMNS = 7
# Belly-line points within this much (metres of apparent depth) of the
# lowest one are all "the wheel"; the contact column is their median. The
# belly of a white fuselage is flat to a pixel or two over a hundred
# columns, so a plain argmin would wander along it from frame to frame.
LOW_BAND_M = 0.25
# Columns either side of the wheel used for the shadow extent, and how far
# below the wheel a shadow still counts as its shadow.
GAP_HALF_WIDTH = 10
GAP_MAX_PX = 200

_K3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
_K5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))


@dataclass(slots=True)
class Silhouette:
    """Full-resolution aircraft and shadow masks of one blob."""

    x0: int
    y0: int
    aircraft: np.ndarray  # bool
    shadow: np.ndarray  # bool
    ratio: np.ndarray  # float32, luminance relative to the background

    @property
    def width(self) -> int:
        return int(self.aircraft.shape[1])


@dataclass(slots=True)
class ContactPoint:
    """Where the wheel is in this frame, in image and ground coordinates."""

    u: float  # image x, full-resolution pixels
    v: float  # image y
    world_x: float  # metres along the strip, ground-plane assumption
    world_y: float  # metres across the strip


def extract(frame: np.ndarray, background: np.ndarray, blob: Blob) -> Silhouette | None:
    """Classify the pixels around ``blob`` into aircraft, shadow, background."""
    height, width = frame.shape[:2]
    x0 = max(0, blob.x - ROI_MARGIN)
    y0 = max(0, blob.y - ROI_MARGIN)
    x1 = min(width, blob.x + blob.w + ROI_MARGIN)
    y1 = min(height, blob.y + blob.h + ROI_MARGIN)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None

    roi = frame[y0:y1, x0:x1].astype(np.float32)
    back = background[y0:y1, x0:x1].astype(np.float32)

    foreground = np.abs(roi - back).sum(axis=2) > FG_DIFF
    ratio = roi.mean(axis=2) / (back.mean(axis=2) + 1.0)
    chroma_roi = roi / (roi.sum(axis=2, keepdims=True) + 1.0)
    chroma_back = back / (back.sum(axis=2, keepdims=True) + 1.0)
    chroma_distance = np.abs(chroma_roi - chroma_back).sum(axis=2)
    shadow = (
        foreground
        & (ratio < SHADOW_RATIO_MAX)
        & (ratio > SHADOW_RATIO_MIN)
        & (chroma_distance < SHADOW_CHROMA)
    )
    aircraft = foreground & ~shadow

    aircraft_u8 = cv2.morphologyEx(aircraft.astype(np.uint8), cv2.MORPH_OPEN, _K3)
    aircraft_u8 = cv2.morphologyEx(aircraft_u8, cv2.MORPH_CLOSE, _K5, iterations=2)
    shadow_u8 = cv2.morphologyEx(shadow.astype(np.uint8), cv2.MORPH_OPEN, _K3)
    shadow_u8 = cv2.morphologyEx(shadow_u8, cv2.MORPH_CLOSE, _K5)
    # Keep the aircraft's own components only. The boundary of a shadow is
    # a line of mixed pixels whose chroma matches neither side, and those
    # specks would otherwise put a "belly" at the bottom of the shadow.
    aircraft_u8 = _keep_major(aircraft_u8, blob.x - x0, blob.y - y0, blob.w, blob.h)
    if not aircraft_u8.any():
        return None
    return Silhouette(
        x0=x0, y0=y0, aircraft=aircraft_u8 > 0, shadow=shadow_u8 > 0, ratio=ratio.astype(np.float32)
    )


# A component this much smaller than the largest is debris, not aircraft.
MINOR_FRACTION = 0.05


def _keep_major(mask: np.ndarray, bx: int, by: int, bw: int, bh: int) -> np.ndarray:
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 2:
        return mask
    areas = stats[1:, cv2.CC_STAT_AREA]
    floor = MINOR_FRACTION * areas.max()
    keep = np.zeros(count, dtype=bool)
    for label in range(1, count):
        x, y, w, h, area = stats[label]
        overlaps = x < bx + bw and x + w > bx and y < by + bh and y + h > by
        keep[label] = bool(overlaps and area >= floor)
    return np.where(keep[labels], mask, 0).astype(np.uint8)


def belly_line(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(columns, bottom rows) of a boolean silhouette."""
    columns = np.flatnonzero(mask.any(axis=0))
    if columns.size == 0:
        return columns, columns
    flipped = mask[::-1, columns]
    bottoms = (mask.shape[0] - 1) - flipped.argmax(axis=0)
    return columns, bottoms


def fuselage_columns(mask: np.ndarray) -> np.ndarray | None:
    """The columns under the fuselage body.

    Seen from the side a glider is a thin wing, a thin tail boom, a fin and
    one thick body around the cockpit and wing root — which is where the
    main wheel is. Thickness (foreground pixels per column) separates the
    body from wing and boom. The fin is thick too, so of the thick runs the
    one nearest the middle of the wingspan is taken: the body sits under
    the wing root, the fin out at the end of the boom.
    """
    thickness = mask.sum(axis=0)
    present = np.flatnonzero(thickness)
    if present.size == 0:
        return None
    centre = (present[0] + present[-1]) / 2
    if thickness.size >= SMOOTH_COLUMNS:
        padded = np.pad(thickness, SMOOTH_COLUMNS // 2, mode="edge")
        thickness = np.median(
            np.lib.stride_tricks.sliding_window_view(padded, SMOOTH_COLUMNS), axis=1
        )
    thick = thickness >= THICK_FRACTION * thickness.max()
    runs: list[tuple[int, int]] = []
    start = None
    for i, flag in enumerate(np.append(thick, False)):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((start, i))
            start = None
    if not runs:
        return None
    lo, hi = min(runs, key=lambda r: abs((r[0] + r[1]) / 2 - centre))
    # The main wheel is behind the cockpit, under the wing root, where the
    # fuselage has already begun to taper - often just past the end of the
    # thick run. Widen it; the boom either side is thin and sits higher, so
    # the wheel still wins as the lowest point.
    margin = int(round(BODY_MARGIN * (hi - lo)))
    return np.arange(max(0, lo - margin), min(mask.shape[1], hi + margin))


# Columns at least this fraction of the thickest column count as body, and
# the body run is widened by this fraction of its length on each side.
THICK_FRACTION = 0.4
BODY_MARGIN = 0.4


@dataclass(slots=True)
class Tyre:
    """A black, compact thing along the belly: a tyre, or something like one."""

    u: float  # image x of its centre
    v: float  # image y of its bottom edge - what touches the ground
    width: float
    height: float
    area: float
    darkness: float  # mean ratio, lower is blacker


@dataclass(slots=True)
class BellyProfile:
    """Per-column readings under the fuselage of one frame.

    Everything the track needs later, so the silhouette itself can go: the
    wheel is tracked once per track (see :func:`wheel_track`), which needs
    the profiles of every frame first.
    """

    us: np.ndarray  # image x of each column (pixel centres)
    belly: np.ndarray  # image y of the fairing's lowest pixel edge per column (above the tyre)
    reach: np.ndarray  # rows of dark below the belly (tyre, shadow) before lit ground; nan = none
    gap: np.ndarray  # rows of lit ground between the tyre and its shadow; nan = no tyre / shadow
    darkest: np.ndarray  # darkest ratio around the belly per column
    in_body: np.ndarray  # bool, column lies under the fuselage body
    body_centre: float  # image x of the middle of the fuselage run
    tyres: list[Tyre]  # black compact blobs along the belly, largest first

    @property
    def wheel_u(self) -> float | None:
        """Column of the main tyre: the widest black thing along the belly.

        Rubber reads far darker than any shadow (ratio ~0.1 against ~0.4).
        The widest such blob is the main wheel (a tail wheel is a third the
        size).
        """
        return self.tyres[0].u if self.tyres else None

    @property
    def lowest_u(self) -> float:
        """Column under the body where the aircraft reaches lowest."""
        belly = np.where(self.in_body, self.belly, -np.inf) if self.in_body.any() else self.belly
        return float(self.us[int(np.argmax(belly))])


def profile(silhouette: Silhouette) -> BellyProfile | None:
    """Read the belly, the tyre and the shadow reach of every column."""
    columns, bottoms = belly_line(silhouette.aircraft)
    if columns.size < 3:
        return None
    body = fuselage_columns(silhouette.aircraft)
    if body is None or body.size < 3:
        span_lo, span_hi = columns.min(), columns.max()
        span = span_hi - span_lo
        lo = span_lo + span * (1 - CENTRAL_BAND) / 2
        hi = span_lo + span * (1 + CENTRAL_BAND) / 2
        body = columns[(columns >= lo) & (columns <= hi)]
    in_body = np.isin(columns, body)

    reach = np.full(columns.size, np.nan)
    gap = np.full(columns.size, np.nan)
    darkest = np.ones(columns.size)
    fairing = bottoms.astype(float)
    ratio = silhouette.ratio
    rows = ratio.shape[0]
    for i, (c, bottom) in enumerate(zip(columns, bottoms, strict=True)):
        # Whether the black tyre ended up inside the aircraft mask depends
        # on the frame; the fairing above it is a steady reference, so walk
        # up out of the rubber and measure everything from there.
        b = int(bottom)
        while b > 0 and ratio[b, c] < TYRE_RATIO and bottom - b < TYRE_ABOVE_PX:
            b -= 1
        fairing[i] = b
        r = _column_reach(ratio[b + 1 :, c])
        if r is not None:
            reach[i] = r
        g = _column_gap(ratio[b + 1 :, c])
        if g is not None:
            gap[i] = g
        lo = max(0, int(bottom) - TYRE_ABOVE_PX)
        hi = min(rows, int(bottom) + TYRE_BELOW_PX)
        if hi > lo:
            darkest[i] = float(ratio[lo:hi, c].min())

    body_us = (
        silhouette.x0 + columns[in_body] + 0.5 if in_body.any() else silhouette.x0 + columns + 0.5
    )
    return BellyProfile(
        us=silhouette.x0 + columns + 0.5,
        belly=silhouette.y0 + fairing + 1.0,
        reach=reach,
        gap=gap,
        darkest=darkest,
        in_body=in_body,
        body_centre=float((body_us[0] + body_us[-1]) / 2),
        tyres=find_tyres(silhouette, columns, bottoms),
    )


def find_tyres(silhouette: Silhouette, columns: np.ndarray, bottoms: np.ndarray) -> list[Tyre]:
    """Black compact blobs in a band along the belly line, largest first.

    The band runs from TYRE_ABOVE_PX above the aircraft's lowest pixel to
    TYRE_BELOW_PX below it in every column, so a tyre counts whether the
    mask swallowed it or not, while the dark canopy higher up does not.
    """
    ratio = silhouette.ratio
    rows, width = ratio.shape
    band = np.zeros((rows, width), dtype=bool)
    for c, bottom in zip(columns, bottoms, strict=True):
        lo = max(0, int(bottom) - TYRE_ABOVE_PX)
        hi = min(rows, int(bottom) + TYRE_BELOW_PX + 1)
        band[lo:hi, c] = True
    if not band.any():
        return []
    # The threshold follows the blackest thing on the belly: in sunshine the
    # tyre's core (~0.05) is far below the umbra beneath it (~0.3), and a
    # tight cut keeps the two apart; under an overcast sky nothing is that
    # black, the tyre is merely the darkest thing there, and the cut rises.
    darkest = float(ratio[band].min())
    cut = float(np.clip(darkest + TYRE_CONTRAST, TYRE_CUT_MIN, TYRE_CUT_MAX))
    dark = ((ratio < cut) & band).astype(np.uint8)
    if not dark.any():
        return []
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(dark, connectivity=8)
    found: list[Tyre] = []
    for label in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[label])
        if area < TYRE_MIN_AREA or w > TYRE_MAX_WIDTH or h > TYRE_ABOVE_PX + TYRE_BELOW_PX:
            continue
        member = labels == label
        found.append(
            Tyre(
                u=float(silhouette.x0 + centroids[label][0] + 0.5),
                v=float(silhouette.y0 + y + h),
                width=float(w),
                height=float(h),
                area=float(area),
                darkness=float(ratio[member].mean()),
            )
        )
    found.sort(key=lambda tyre: tyre.area, reverse=True)
    return found[:TYRE_MAX_CANDIDATES]


# The wheel column over a track is a smooth function of time; a frame whose
# tyre reading is further than this from that curve is a mis-read (the
# other wheel of a taildragger, the tug in a shared blob) and is replaced
# by the curve. The curve is used outright where the column it names is not
# in the silhouette at all.
WHEEL_OUTLIER_PX = 40.0
WHEEL_MISSING_PX = 25.0


def _smooth(
    t: np.ndarray, raw: np.ndarray, good: np.ndarray, outlier_px: float
) -> tuple[np.ndarray, np.ndarray]:
    """(fitted curve, kept readings): a low-order polynomial in time through
    the good readings with outliers thrown out."""
    if good.sum() < 3:
        return raw.copy(), good.copy()
    t0 = t - t[0]
    keep = good.copy()
    coef = np.polyfit(t0[keep], raw[keep], 1)
    for _ in range(3):
        order = 2 if keep.sum() >= 8 else 1
        coef = np.polyfit(t0[keep], raw[keep], order)
        residual = raw - np.polyval(coef, t0)
        scale = max(outlier_px, 3 * 1.4826 * float(np.median(np.abs(residual[keep]))))
        refined = good & (np.abs(residual) <= scale)
        if refined.sum() < 3 or np.array_equal(refined, keep):
            break
        keep = refined
    fitted = np.polyval(coef, t0)
    return fitted, keep & (np.abs(raw - fitted) <= outlier_px)


@dataclass(slots=True)
class WheelTrack:
    """The main wheel through a track: one point per frame."""

    u: np.ndarray
    v: np.ndarray  # bottom of the tyre
    seen: np.ndarray  # bool: the tyre itself was found in this frame


def wheel_track(profiles: list[BellyProfile], t: np.ndarray, usable: np.ndarray) -> WheelTrack:
    """Follow the tyre through the track; interpolate where it is hidden.

    The main wheel is a black compact blob that moves smoothly. Frame by
    frame the widest candidate says roughly where it is; a quadratic in
    time through those (outliers out - the other wheel of a taildragger,
    the tug in a shared blob) gives the column everywhere. Then, in every
    frame, the candidate nearest that column *is* the tyre, and the bottom
    of its blob is the contact row; a second smoothed curve through those
    rows gives the row where no candidate exists (wheel fully in the wing's
    shade) and overrules a candidate whose bottom is off the curve (a
    dark fairing seam, a wheel-well shadow). So the point never hops
    between the rubber and the belly above it. With no tyre in the whole
    track the lowest point of the belly stands in, smoothed the same way.
    """
    n = len(profiles)
    widest = np.array([p.wheel_u if p.wheel_u is not None else np.nan for p in profiles])
    raw_u = np.where(np.isnan(widest), [p.lowest_u for p in profiles], widest)
    good_u = usable & ~np.isnan(widest)
    if good_u.sum() < 5:
        good_u = usable.copy()
    fitted_u, keep_u = _smooth(t, raw_u, good_u, WHEEL_OUTLIER_PX)
    column = np.where(keep_u, raw_u, fitted_u)

    # The tyre nearest the column in each frame.
    u = column.copy()
    v = np.full(n, np.nan)
    seen = np.zeros(n, dtype=bool)
    for i, prof in enumerate(profiles):
        near = [tyre for tyre in prof.tyres if abs(tyre.u - column[i]) <= TYRE_CAPTURE_PX]
        if near:
            tyre = min(near, key=lambda ty: abs(ty.u - column[i]))
            u[i], v[i], seen[i] = tyre.u, tyre.v, True
    if seen.sum() >= 3:
        fitted_v, keep_v = _smooth(t, np.where(seen, v, 0.0), seen & usable, TYRE_ROW_OUTLIER_PX)
        v = np.where(keep_v, v, fitted_v)
        seen = keep_v
    else:
        # No tyre to speak of: the belly at the column, a tyre height up,
        # smoothed in time the same way so it cannot hop either.
        belly = np.array(
            [
                prof.belly[int(np.argmin(np.abs(prof.us - column[i])))]
                for i, prof in enumerate(profiles)
            ]
        )
        fitted_v, keep_v = _smooth(t, belly, usable, TYRE_ROW_OUTLIER_PX)
        v = np.where(keep_v, belly, fitted_v)
        seen[:] = False
    return WheelTrack(u=u, v=v, seen=seen)


def locate(
    prof: BellyProfile, u: float, v: float, matrix: np.ndarray | list[list[float]]
) -> tuple[ContactPoint, float | None, float | None] | None:
    """(contact point, tyre gap, shadow reach) of one frame at wheel ``(u, v)``.

    ``None`` when the silhouette has no belly near that column - the point
    the track predicts is not on this frame's aircraft at all.
    """
    i = int(np.argmin(np.abs(prof.us - u)))
    if abs(float(prof.us[i]) - u) > WHEEL_MISSING_PX:
        return None
    world_x, world_y = hg.project(matrix, u, v)
    band = np.abs(prof.us - u) <= GAP_HALF_WIDTH
    need = max(3, int(band.sum()) // 3)

    def median_of(values: np.ndarray) -> float | None:
        valid = values[band][np.isfinite(values[band])]
        return float(np.median(valid)) if valid.size >= need else None

    reach = median_of(prof.reach)
    if reach is not None:
        # Reach was read from the fairing; report it from the tyre's bottom.
        reach = max(0.0, reach - (v - float(np.median(prof.belly[band]))))
    return (
        ContactPoint(u=u, v=v, world_x=world_x, world_y=world_y),
        median_of(prof.gap),
        reach,
    )


# The tyre: darker than this relative to the background, looked for from
# this far above the belly line (a black tyre is inside the aircraft mask)
# to this far below it (a grey one in shade is not).
TYRE_RATIO = 0.28
TYRE_ABOVE_PX = 20
TYRE_BELOW_PX = 12
# A tyre blob: at least this many pixels, at most this wide; the few largest
# per frame are kept as candidates. A candidate within TYRE_CAPTURE_PX of
# the track's column is the wheel; a bottom row further than
# TYRE_ROW_OUTLIER_PX from the smoothed row is not the rubber.
TYRE_MIN_AREA = 12
TYRE_MAX_WIDTH = 60
TYRE_MAX_CANDIDATES = 6
TYRE_CAPTURE_PX = 25.0
TYRE_ROW_OUTLIER_PX = 8.0
# Tyre blobs are cut this much above the blackest pixel on the belly, within
# these bounds (see find_tyres).
TYRE_CONTRAST = 0.12
TYRE_CUT_MIN = 0.18
TYRE_CUT_MAX = 0.45

# Below the belly, luminance relative to the background: sunlit ground is
# brighter than LIT; tyre, umbra and penumbra are all darker. Two lit rows
# in a row end the dark run, so one bright speck of grass cannot.
LIT = 0.82


def _column_reach(ratio: np.ndarray) -> float | None:
    """Rows of dark under the belly of one column before lit ground.

    ``ratio`` starts at the row below the aircraft's lowest pixel. ``None``
    when no lit ground is found within reach - the aircraft is high, or
    something else dark lies under it.
    """
    n = min(len(ratio), GAP_MAX_PX)
    i = 0
    while i < n:
        if ratio[i] > LIT and (i + 1 >= n or ratio[i + 1] > LIT):
            return float(i)
        i += 1
    return None


# Directly under the tyre the ground is in penumbra, not full sun: lit
# enough to tell from the umbra of the fuselage shadow, which is what the
# gap is measured against. The tyre's own bottom edge is blurred over a few
# rows; those are skipped, not counted.
GAP_LIT = 0.60
GAP_BLUR_PX = 4


def _column_gap(ratio: np.ndarray) -> float | None:
    """Rows of lit ground between the bottom of the tyre and its shadow.

    ``ratio`` starts at the row below the fairing. The tyre is the black
    run at the top; ``None`` when there is none in this column, or when no
    shadow follows within reach (nothing to close a gap against). Zero
    when the shadow touches the tyre.
    """
    n = min(len(ratio), GAP_MAX_PX)
    i = 0
    while i < n and ratio[i] < TYRE_RATIO:
        i += 1
    if i == 0 or i > TYRE_ABOVE_PX:
        return None
    # Out of the blurred edge of the rubber, then count the lit rows.
    edge = i
    while i < n and ratio[i] < GAP_LIT and i - edge < GAP_BLUR_PX:
        i += 1
    lit = 0
    while i < n and ratio[i] >= GAP_LIT:
        lit += 1
        i += 1
    if i >= n:
        return None  # never reached shadow
    return float(lit)
