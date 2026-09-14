"""Detection, tracking and the contact point, on synthetic frames.

These need OpenCV; without it the whole module is skipped, as the recorder
side of the project must stay installable without it.
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from touchdown_analyzer.analysis import contact as cm  # noqa: E402
from touchdown_analyzer.analysis.detect import Blob, Detector, Tracker  # noqa: E402

W, H = 640, 360


def scene(
    x: int, *, y: int = 220, shadow_dy: int = 30, tyre: bool = True, wheel_dx: int = 10
) -> np.ndarray:
    """A grass frame with a white 'glider' at ``x`` and its shadow below.

    Fuselage: 120 x 24 px. Wing: a thin line across. Tyre: a black 8 x 8 px
    block under the fuselage, ``wheel_dx`` right of the body centre. Shadow:
    dark grass from just under the fuselage (the wing's shadow, as in real
    footage) down to ``shadow_dy + 24`` below it.
    """
    frame = np.full((H, W, 3), (60, 140, 70), dtype=np.uint8)
    noise = np.random.default_rng(abs(x)).integers(-8, 8, size=(H, W, 1))
    frame = np.clip(frame.astype(int) + noise, 0, 255).astype(np.uint8)
    # shadow first, aircraft on top
    cv2.rectangle(frame, (x - 60, y + 26), (x + 60, y + shadow_dy + 24), (28, 60, 32), -1)
    cv2.rectangle(frame, (x - 60, y), (x + 60, y + 24), (235, 235, 235), -1)
    cv2.line(frame, (x - 200, y + 6), (x + 200, y + 6), (230, 230, 230), 3)
    cv2.rectangle(frame, (x - 45, y - 40), (x - 30, y), (235, 235, 235), -1)  # fin
    if tyre:
        cv2.rectangle(frame, (x + wheel_dx - 4, y + 24), (x + wheel_dx + 4, y + 32), (8, 8, 8), -1)
    return frame


def test_detector_finds_one_moving_blob_and_tracker_follows_it() -> None:
    det = Detector(scale=0.5, min_area=300)
    tracker = Tracker()
    # let the background settle on empty frames first
    for _ in range(30):
        det.apply(scene(-500))
    finished = []
    for i in range(40):
        blobs = det.apply(scene(120 + 10 * i))
        _, done = tracker.update(30 + i, blobs)
        finished += done
    # one long track for the glider; debris (a wing tip the opening did not
    # quite remove) may make short-lived extra ones, which the pipeline
    # drops by length and area
    track = max(tracker.active, key=lambda t: len(t.observations))
    assert len(track.observations) >= 30
    assert track.span_px() > 250
    # the object leaves: the track is released after MAX_MISSES empty frames
    for i in range(10):
        _, done = tracker.update(70 + i, det.apply(scene(-500)))
        finished += done
    assert track in finished and not tracker.active


def test_blob_touching_the_edge_is_clipped() -> None:
    blob = Blob(x=0, y=10, w=50, h=20, area=1000, mask=np.ones((10, 25), bool), scale=0.5)
    assert blob.touches_edge(640)
    blob.x = 300
    assert not blob.touches_edge(640)


def _silhouette(x: int, **kw) -> cm.Silhouette:
    det = Detector(scale=0.5, min_area=300)
    for _ in range(30):
        det.apply(scene(-500))
    frame = scene(x, **kw)
    blobs = det.apply(frame)
    assert blobs, "the synthetic glider was not detected"
    sil = cm.extract(frame, det.background(frame.shape), blobs[0])
    assert sil is not None
    return sil


def test_silhouette_separates_aircraft_from_shadow() -> None:
    sil = _silhouette(300)
    # the shadow (y 246..274) is not aircraft; the fuselage (220..244) and
    # the black tyre (244..252) are
    ys, xs = np.nonzero(sil.aircraft)
    assert (sil.y0 + ys).max() <= 253
    ys_s, _ = np.nonzero(sil.shadow)
    assert (sil.y0 + ys_s).min() >= 245


def test_profile_finds_the_tyre_column_and_the_shadow_reach() -> None:
    sil = _silhouette(300, wheel_dx=10)
    prof = cm.profile(sil)
    assert prof is not None
    assert prof.wheel_u == pytest.approx(310, abs=3)
    tyre = prof.tyres[0]
    assert tyre.v == pytest.approx(252, abs=1)  # bottom of the 8 px tyre under y+24
    identity = np.eye(3)
    located = cm.locate(prof, tyre.u, tyre.v, identity)
    assert located is not None
    cp, _gap, reach = located
    assert cp.u == pytest.approx(310, abs=3) and cp.v == pytest.approx(252, abs=1)
    # a column the silhouette does not reach is refused, not approximated
    assert cm.locate(prof, 900.0, 250.0, identity) is None
    # dark from the tyre's bottom (y 252) to the bottom of the shadow (y 274)
    assert reach is not None and 18 <= reach <= 26


def test_wheel_track_follows_the_tyre_and_bridges_frames_without_one() -> None:
    xs = [200, 220, 240, 260, 280, 300, 320, 340]
    profiles = [cm.profile(_silhouette(x, wheel_dx=12, tyre=(i != 4))) for i, x in enumerate(xs)]
    assert all(p is not None for p in profiles)
    t = np.arange(len(xs)) / 60.0
    usable = np.ones(len(xs), dtype=bool)
    wheel = cm.wheel_track(profiles, t, usable)  # type: ignore[arg-type]
    # the frame without a tyre is interpolated onto the line through the others
    assert not wheel.seen[4] and wheel.seen[3] and wheel.seen[5]
    assert wheel.u[4] == pytest.approx(280 + 12, abs=4)
    assert wheel.v[4] == pytest.approx(252, abs=2)
    assert np.all(np.abs(wheel.v - 252) <= 2)  # the row never hops to the belly
    assert np.all(np.diff(wheel.u) > 0)
    # a candidate that is another dark thing, far from the wheel, is ignored
    decoy = cm.Tyre(u=320 - 60, v=240, width=30, height=10, area=300, darkness=0.1)
    profiles[6].tyres.insert(0, decoy)  # type: ignore[union-attr]
    wheel = cm.wheel_track(profiles, t, usable)  # type: ignore[arg-type]
    assert wheel.u[6] == pytest.approx(320 + 12, abs=6)
    assert wheel.v[6] == pytest.approx(252, abs=2)
