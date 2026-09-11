"""Image plane to ground plane, from clicked survey markers.

World frame (docs/design.md 4.1): origin on the target line at the strip
centre, ``x`` along the strip and positive beyond the line in the landing
direction, ``y`` lateral and positive to the right seen from behind.

Solved by normalised DLT rather than ``cv2.findHomography(..., RANSAC)``.
RANSAC exists to reject outliers among hundreds of automatic matches; here a
person clicks six markers, so there are no outliers to reject and least
squares over all of them is both simpler and better. A misclick shows up as a
large residual on that one marker, which is more useful than having RANSAC
quietly discard it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

MIN_MARKERS = 4

# A homography needs points spread in two directions. These are the minimum
# spreads perpendicular to the dominant axis before the fit is trustworthy.
MIN_WORLD_SPREAD_M = 1.0
MIN_IMAGE_SPREAD_PX = 5.0

# M1 is done when the fit is this good (docs/design.md 7).
TARGET_RESIDUAL_M = 0.10


class CalibrationError(ValueError):
    """The marker set cannot produce a trustworthy homography."""


@dataclass(slots=True)
class Marker:
    """One surveyed point, clicked in the image."""

    image_x: float
    image_y: float
    world_x: float
    world_y: float
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Calibration:
    """A solved mapping plus the numbers that say whether to trust it."""

    matrix: list[list[float]]  # image -> world
    inverse: list[list[float]]  # world -> image, for drawing overlays
    markers: list[Marker]
    residual_m: float  # RMS, the headline quality number
    worst_m: float
    per_marker_m: list[float]
    image_size: tuple[int, int] | None = None
    source: str = ""
    created_utc: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    notes: str = ""

    @property
    def acceptable(self) -> bool:
        return self.residual_m <= TARGET_RESIDUAL_M

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["acceptable"] = self.acceptable
        payload["target_residual_m"] = TARGET_RESIDUAL_M
        return payload


def _perpendicular_spread(points: np.ndarray) -> float:
    """RMS spread across the *narrow* axis of a point set.

    Zero means every point sits on one straight line, which is the degenerate
    case a homography cannot be solved from.
    """
    if len(points) < 2:
        return 0.0
    centred = points - points.mean(axis=0)
    singular = np.linalg.svd(centred, compute_uv=False)
    smallest = singular[1] if len(singular) > 1 else 0.0
    return float(smallest / np.sqrt(len(points)))


def check_geometry(markers: list[Marker]) -> str:
    """Why this marker set cannot be solved, or ``""`` if it can.

    The collinear case is the one worth guarding: markers laid out only along
    the strip axis (say at -15, 0 and +15 m) all lie on one line, and a
    homography fitted to them is not merely inaccurate but undetermined.
    """
    if len(markers) < MIN_MARKERS:
        return (
            f"{len(markers)} marker(s): a homography needs at least {MIN_MARKERS}. "
            "Add markers on both edges of the strip."
        )

    world = np.array([[m.world_x, m.world_y] for m in markers], dtype=float)
    image = np.array([[m.image_x, m.image_y] for m in markers], dtype=float)

    if len({(m.world_x, m.world_y) for m in markers}) < len(markers):
        return "two markers have the same ground coordinates"
    if len({(round(m.image_x, 1), round(m.image_y, 1)) for m in markers}) < len(markers):
        return "two markers were clicked at the same place in the image"

    world_spread = _perpendicular_spread(world)
    if world_spread < MIN_WORLD_SPREAD_M:
        return (
            f"the markers lie on one line on the ground (spread across it is only "
            f"{world_spread:.2f} m). A homography needs points spread in two "
            "directions - markers at -15/0/+15 m along the strip axis are not "
            "enough on their own. Put a pair on each edge of the strip."
        )

    image_spread = _perpendicular_spread(image)
    if image_spread < MIN_IMAGE_SPREAD_PX:
        return (
            f"the clicked points lie on one line in the image (spread across it is "
            f"only {image_spread:.1f} px). Check the clicks, and that the camera "
            "really sees the strip at an angle rather than edge-on."
        )

    return ""


def _normalise(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Hartley normalisation: centroid to origin, mean distance sqrt(2).

    Without it the DLT design matrix is badly conditioned, because pixel
    coordinates run to ~1900 while world coordinates run to ~15.
    """
    centroid = points.mean(axis=0)
    centred = points - centroid
    mean_distance = float(np.sqrt((centred**2).sum(axis=1)).mean())
    scale = np.sqrt(2) / mean_distance if mean_distance > 0 else 1.0

    transform = np.array(
        [[scale, 0, -scale * centroid[0]], [0, scale, -scale * centroid[1]], [0, 0, 1]]
    )
    return centred * scale, transform


def solve(
    markers: list[Marker],
    *,
    image_size: tuple[int, int] | None = None,
    source: str = "",
    notes: str = "",
) -> Calibration:
    """Fit the image -> ground homography and score it.

    Raises :class:`CalibrationError` when the markers are degenerate, rather
    than returning a plausible-looking matrix fitted to nothing.
    """
    problem = check_geometry(markers)
    if problem:
        raise CalibrationError(problem)

    image = np.array([[m.image_x, m.image_y] for m in markers], dtype=float)
    world = np.array([[m.world_x, m.world_y] for m in markers], dtype=float)

    image_n, t_image = _normalise(image)
    world_n, t_world = _normalise(world)

    rows = []
    for (x, y), (wx, wy) in zip(image_n, world_n, strict=True):
        rows.append([-x, -y, -1, 0, 0, 0, x * wx, y * wx, wx])
        rows.append([0, 0, 0, -x, -y, -1, x * wy, y * wy, wy])

    _, _, vt = np.linalg.svd(np.array(rows, dtype=float))
    h_normalised = vt[-1].reshape(3, 3)

    # Undo the normalisation: world = T_world^-1 @ H_n @ T_image @ image
    matrix = np.linalg.inv(t_world) @ h_normalised @ t_image
    if abs(matrix[2, 2]) < 1e-12:
        raise CalibrationError("the fit is degenerate; check the marker coordinates")
    matrix = matrix / matrix[2, 2]

    try:
        inverse = np.linalg.inv(matrix)
    except np.linalg.LinAlgError as exc:
        raise CalibrationError("the fit is not invertible; check the marker coordinates") from exc
    inverse = inverse / inverse[2, 2]

    projected = project_many(matrix, image)
    per_marker = np.sqrt(((projected - world) ** 2).sum(axis=1))

    return Calibration(
        matrix=matrix.tolist(),
        inverse=inverse.tolist(),
        markers=list(markers),
        residual_m=float(np.sqrt((per_marker**2).mean())),
        worst_m=float(per_marker.max()),
        per_marker_m=[float(value) for value in per_marker],
        image_size=image_size,
        source=source,
        notes=notes,
    )


def project_many(matrix: np.ndarray | list[list[float]], points: np.ndarray) -> np.ndarray:
    """Map an (N, 2) array through a homography."""
    m = np.asarray(matrix, dtype=float)
    homogeneous = np.hstack([np.asarray(points, dtype=float), np.ones((len(points), 1))])
    mapped = homogeneous @ m.T
    w = mapped[:, 2:3]
    # A point on the horizon maps to infinity; keep it finite rather than
    # raising, so one bad click cannot take the whole overlay down.
    w = np.where(np.abs(w) < 1e-12, np.nan, w)
    result: np.ndarray = mapped[:, :2] / w
    return result


def project(matrix: np.ndarray | list[list[float]], x: float, y: float) -> tuple[float, float]:
    """Map a single point: image px -> ground metres, or the reverse."""
    result = project_many(matrix, np.array([[x, y]], dtype=float))[0]
    return float(result[0]), float(result[1])


def standard_markers(half_length_m: float = 15.0, half_width_m: float = 10.0) -> list[Marker]:
    """The layout this tool expects: three pairs, one on each strip edge.

    Image coordinates are left at zero - they are filled in by clicking. Only
    the ground coordinates are the survey's business, and they are what makes
    the set non-degenerate: a pair on each edge gives the lateral spread that
    markers along the axis alone cannot.
    """
    layout = []
    for x, name in ((-half_length_m, "short"), (0.0, "target"), (half_length_m, "long")):
        for sign, side in ((+1, "far"), (-1, "near")):
            layout.append(
                Marker(
                    image_x=0.0,
                    image_y=0.0,
                    world_x=x,
                    world_y=sign * half_width_m,
                    label=f"{name} {side}",
                )
            )
    return layout


def save(calibration: Calibration, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(calibration.as_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def load(path: Path) -> Calibration:
    # utf-8-sig: tolerate a BOM, which a hand-edited file may pick up.
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    payload.pop("acceptable", None)
    payload.pop("target_residual_m", None)
    markers = [Marker(**m) for m in payload.pop("markers", [])]
    size = payload.pop("image_size", None)
    image_size = (int(size[0]), int(size[1])) if size else None
    return Calibration(markers=markers, image_size=image_size, **payload)
