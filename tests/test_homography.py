from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from touchdown_analyzer.calibration import homography as hg


def _synthetic_camera(distance_m: float = 75.0, mast_m: float = 8.0) -> np.ndarray:
    """The installed geometry as a world -> image homography.

    AXIS P1485-LE at the wide end (HFOV 29 deg, 1920x1080), side-on to the
    strip at 75 m, 8 m up, aimed at the target line. Used to generate exact
    correspondences, so the solver can be checked against a known answer.

    World is (x along the strip, y lateral); the camera sits at (0, -distance,
    mast) and looks at the origin, so the target line lands at image centre.
    """
    slant = np.hypot(distance_m, mast_m)
    sin_tilt, cos_tilt = mast_m / slant, distance_m / slant

    focal = 1920 / (2 * np.tan(np.radians(29.0) / 2))
    intrinsics = np.array([[focal, 0, 960.0], [0, focal, 540.0], [0, 0, 1]])

    # [r1 | r2 | t] for the ground plane z = 0, camera axes X right, Y down,
    # Z forward. Columns dropped for z = 0, which is what makes it a 3x3.
    plane = np.array(
        [
            [1.0, 0.0, 0.0],
            [0.0, -sin_tilt, mast_m * cos_tilt - distance_m * sin_tilt],
            [0.0, cos_tilt, distance_m * cos_tilt + mast_m * sin_tilt],
        ]
    )
    world_to_image = intrinsics @ plane
    return world_to_image / world_to_image[2, 2]


def _markers_from(world_to_image: np.ndarray, points: list[tuple[float, float]]) -> list[hg.Marker]:
    image = hg.project_many(world_to_image, np.array(points, dtype=float))
    return [
        hg.Marker(image_x=float(px), image_y=float(py), world_x=wx, world_y=wy)
        for (px, py), (wx, wy) in zip(image, points, strict=True)
    ]


# The layout in the README: a pair on each strip edge at -15, 0 and +15 m.
THREE_PAIRS = [
    (-15.0, 10.0),
    (-15.0, -10.0),
    (0.0, 10.0),
    (0.0, -10.0),
    (15.0, 10.0),
    (15.0, -10.0),
]


# --------------------------------------------------------------------------
# degeneracy guards - the reason this module exists
# --------------------------------------------------------------------------


def test_markers_along_the_axis_alone_are_refused() -> None:
    """-15 / 0 / +15 on the centreline is collinear: no unique homography."""
    collinear = [
        hg.Marker(image_x=400 + i * 300, image_y=700, world_x=x, world_y=0.0)
        for i, x in enumerate((-15.0, -5.0, 0.0, 15.0))
    ]
    problem = hg.check_geometry(collinear)
    assert "one line on the ground" in problem

    with pytest.raises(hg.CalibrationError, match="one line"):
        hg.solve(collinear)


def test_three_pairs_are_accepted() -> None:
    markers = _markers_from(_synthetic_camera(), THREE_PAIRS)
    assert hg.check_geometry(markers) == ""


def test_too_few_markers() -> None:
    markers = _markers_from(_synthetic_camera(), THREE_PAIRS[:3])
    assert "at least 4" in hg.check_geometry(markers)


def test_duplicate_ground_coordinates() -> None:
    markers = _markers_from(_synthetic_camera(), THREE_PAIRS)
    markers[1].world_x, markers[1].world_y = markers[0].world_x, markers[0].world_y
    assert "same ground coordinates" in hg.check_geometry(markers)


def test_duplicate_clicks() -> None:
    markers = _markers_from(_synthetic_camera(), THREE_PAIRS)
    markers[1].image_x, markers[1].image_y = markers[0].image_x, markers[0].image_y
    assert "same place in the image" in hg.check_geometry(markers)


def test_collinear_clicks_are_refused() -> None:
    """Ground spread is fine but every click landed on one image row."""
    markers = [
        hg.Marker(image_x=300.0 + i * 250, image_y=700.0, world_x=x, world_y=y)
        for i, (x, y) in enumerate(THREE_PAIRS)
    ]
    assert "one line in the image" in hg.check_geometry(markers)


# --------------------------------------------------------------------------
# solving
# --------------------------------------------------------------------------


def test_exact_correspondences_are_recovered() -> None:
    camera = _synthetic_camera()
    calibration = hg.solve(_markers_from(camera, THREE_PAIRS))

    assert calibration.residual_m < 1e-6
    assert calibration.acceptable


def test_points_between_the_markers_map_correctly() -> None:
    """The fit must interpolate, not just reproduce the six clicked points."""
    camera = _synthetic_camera()
    calibration = hg.solve(_markers_from(camera, THREE_PAIRS))

    for truth in [(7.3, 2.5), (-11.0, -6.0), (0.0, 0.0), (14.0, 9.0)]:
        px, py = hg.project(camera, *truth)
        recovered = hg.project(calibration.matrix, px, py)
        assert recovered == pytest.approx(truth, abs=1e-4)


def test_inverse_round_trips() -> None:
    camera = _synthetic_camera()
    calibration = hg.solve(_markers_from(camera, THREE_PAIRS))

    px, py = hg.project(calibration.inverse, 12.0, -4.0)
    assert hg.project(calibration.matrix, px, py) == pytest.approx((12.0, -4.0), abs=1e-6)


def test_a_misclicked_marker_shows_up_as_its_own_residual() -> None:
    """One bad click must be visible, not smeared across the whole fit."""
    camera = _synthetic_camera()
    markers = _markers_from(camera, THREE_PAIRS)
    markers[2].image_x += 25.0  # ~0.5 m at the target line

    calibration = hg.solve(markers)
    worst = calibration.per_marker_m.index(max(calibration.per_marker_m))
    assert worst == 2
    assert calibration.worst_m > 4 * min(calibration.per_marker_m)


def _noisy(camera: np.ndarray, seed: int, click_px: float, survey_m: float) -> hg.Calibration:
    rng = np.random.default_rng(seed)
    markers = _markers_from(camera, THREE_PAIRS)
    for marker in markers:
        marker.world_x += float(rng.normal(0, survey_m))
        marker.world_y += float(rng.normal(0, survey_m))
        marker.image_x += float(rng.normal(0, click_px))
        marker.image_y += float(rng.normal(0, click_px))
    return hg.solve(markers)


def test_a_sloppy_tape_survey_still_passes_m1() -> None:
    """5 cm of survey error is tolerable; it is not the binding constraint."""
    camera = _synthetic_camera()
    worst = max(_noisy(camera, seed, 0.0, 0.05).residual_m for seed in range(50))
    assert worst < hg.TARGET_RESIDUAL_M


def test_careful_clicking_passes_m1() -> None:
    """Half-pixel clicking - what the zoom magnifier is for - clears 0.1 m."""
    camera = _synthetic_camera()
    residuals = [_noisy(camera, seed, 0.5, 0.03).residual_m for seed in range(50)]
    assert float(np.mean(residuals)) < hg.TARGET_RESIDUAL_M


def test_click_error_dominates_survey_error() -> None:
    """The reason the calibration page magnifies instead of clicking raw pixels.

    At an 8 m mast the ground is seen at ~6 deg, so one vertical pixel is
    ~0.19 m of depth. Sloppy clicking is therefore far more damaging than a
    sloppy tape measure, and 2 px of click error alone busts the M1 target.
    """
    camera = _synthetic_camera()
    sloppy_tape = float(np.mean([_noisy(camera, s, 0.0, 0.05).residual_m for s in range(50)]))
    sloppy_clicks = float(np.mean([_noisy(camera, s, 2.0, 0.0).residual_m for s in range(50)]))

    assert sloppy_tape < hg.TARGET_RESIDUAL_M
    assert sloppy_clicks > hg.TARGET_RESIDUAL_M
    assert sloppy_clicks > 3 * sloppy_tape


def test_scale_is_right_at_the_target_line() -> None:
    """One pixel near the target line should be about 20 mm (design 2.1)."""
    camera = _synthetic_camera()
    calibration = hg.solve(_markers_from(camera, THREE_PAIRS))

    origin = hg.project(calibration.inverse, 0.0, 0.0)
    one_metre = hg.project(calibration.inverse, 1.0, 0.0)
    px_per_m = abs(one_metre[0] - origin[0])
    assert 40 < px_per_m < 60


# --------------------------------------------------------------------------
# layout helper and persistence
# --------------------------------------------------------------------------


def test_standard_layout_is_three_pairs_and_solvable() -> None:
    layout = hg.standard_markers(half_length_m=15.0, half_width_m=10.0)
    assert len(layout) == 6
    assert sorted({m.world_x for m in layout}) == [-15.0, 0.0, 15.0]
    assert sorted({m.world_y for m in layout}) == [-10.0, 10.0]

    # Ground geometry alone must already be non-degenerate.
    world = np.array([[m.world_x, m.world_y] for m in layout])
    assert hg._perpendicular_spread(world) >= hg.MIN_WORLD_SPREAD_M


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    camera = _synthetic_camera()
    calibration = hg.solve(
        _markers_from(camera, THREE_PAIRS), image_size=(1920, 1080), source="rtsp://cam"
    )
    path = hg.save(calibration, tmp_path / "config" / "calibration.json")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["acceptable"] is True

    reloaded = hg.load(path)
    assert reloaded.residual_m == pytest.approx(calibration.residual_m)
    assert len(reloaded.markers) == 6
    assert reloaded.image_size == (1920, 1080)
    assert hg.project(reloaded.matrix, 960, 700) == pytest.approx(
        hg.project(calibration.matrix, 960, 700)
    )


def test_unacceptable_calibration_is_flagged() -> None:
    camera = _synthetic_camera()
    markers = _markers_from(camera, THREE_PAIRS)
    markers[0].image_x += 200.0  # a badly misplaced click

    calibration = hg.solve(markers)
    assert not calibration.acceptable
