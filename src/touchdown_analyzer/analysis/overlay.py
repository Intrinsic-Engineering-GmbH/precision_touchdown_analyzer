"""The proof image: the contact frame with the geometry drawn on it.

A number on its own convinces nobody; the frame with the target line, the
wheel and the measured offset drawn in is what a pilot accepts
(docs/design.md 4.3).
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from touchdown_analyzer.calibration import homography as hg

AMBER = (40, 170, 250)  # BGR
WHITE = (245, 245, 245)
INK = (20, 20, 20)
GREEN = (110, 200, 90)
CYAN = (230, 200, 60)
RED = (80, 80, 240)


def _line_on_ground(
    inverse: np.ndarray | list[list[float]], x_m: float, y_from: float, y_to: float
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    pts = hg.project_many(inverse, np.array([[x_m, y_from], [x_m, y_to]], dtype=float))
    if not np.isfinite(pts).all():
        return None
    (u1, v1), (u2, v2) = pts
    return (int(round(u1)), int(round(v1))), (int(round(u2)), int(round(v2)))


def _label(image: np.ndarray, text: str, origin: tuple[int, int], scale: float = 0.7) -> None:
    font = cv2.FONT_HERSHEY_SIMPLEX
    (w, h), base = cv2.getTextSize(text, font, scale, 2)
    x, y = origin
    cv2.rectangle(image, (x - 6, y - h - 8), (x + w + 6, y + base + 4), INK, -1)
    cv2.putText(image, text, (x, y), font, scale, WHITE, 2, cv2.LINE_AA)


def render(
    frame: np.ndarray,
    *,
    inverse: np.ndarray | list[list[float]],
    contact: tuple[float, float] | None,
    approach: list[tuple[float, float]],
    ground: list[tuple[float, float]],
    headline: str,
    caption: str,
    strip_half_width_m: float = 12.0,
    window_m: float | None = None,
) -> np.ndarray:
    """Draw the target line, the wheel's approach and ground run, the contact.

    ``approach`` is the wheel's path while airborne, ``ground`` its path
    after contact; both are full-frame observations only, so a wing that
    was still outside the picture cannot put a kink in the line.
    """
    image = frame.copy()
    height, width = image.shape[:2]

    # Target line, plus the measurement window edges if asked for.
    for x_m, colour, thick in (
        (0.0, AMBER, 3),
        *(((-window_m, WHITE, 1), (window_m, WHITE, 1)) if window_m else ()),
    ):
        seg = _line_on_ground(inverse, x_m, -strip_half_width_m, strip_half_width_m)
        if seg:
            cv2.line(image, seg[0], seg[1], colour, thick, cv2.LINE_AA)
            if x_m == 0.0:
                _label(
                    image,
                    "TARGET LINE",
                    (min(seg[0][0], seg[1][0]) + 8, min(seg[0][1], seg[1][1]) - 10),
                    0.6,
                )

    # Where the wheel has been: the approach in cyan, the ground run in green.
    def inside(path: list[tuple[float, float]]) -> list[tuple[int, int]]:
        return [
            (int(round(u)), int(round(v))) for u, v in path if 0 <= u < width and 0 <= v < height
        ]

    air, run = inside(approach), inside(ground)
    if air and run:
        run = [air[-1], *run]  # one continuous line through the contact
    for a, b in zip(air, air[1:], strict=False):
        cv2.line(image, a, b, CYAN, 2, cv2.LINE_AA)
    for p in air[::3]:
        cv2.circle(image, p, 3, CYAN, -1, cv2.LINE_AA)
    for a, b in zip(run, run[1:], strict=False):
        cv2.line(image, a, b, GREEN, 2, cv2.LINE_AA)
    if len(air) >= 2:
        # Label the start of the approach, off the line.
        x, y = air[0]
        _label(image, "APPROACH", (min(max(x, 8), width - 160), max(y - 26, 30)), 0.55)

    if contact is not None:
        u, v = int(round(contact[0])), int(round(contact[1]))
        cv2.circle(image, (u, v), 22, AMBER, 3, cv2.LINE_AA)
        cv2.line(image, (u - 34, v), (u + 34, v), AMBER, 2, cv2.LINE_AA)
        cv2.line(image, (u, v - 34), (u, v + 34), AMBER, 2, cv2.LINE_AA)

    _label(image, headline, (24, 48), 1.1)
    _label(image, caption, (24, 84), 0.55)
    return image


def read_frame(video: Path, frame_index: int) -> np.ndarray | None:
    """One frame of a segment, decoded exactly (from the preceding keyframe)."""
    cap = cv2.VideoCapture(str(video))
    try:
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = cap.read()
        if not ok:
            return None
        got = int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1
        if got != frame_index:
            # Seek landed elsewhere; walk from the start, which is always exact.
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            for _ in range(frame_index + 1):
                ok, frame = cap.read()
                if not ok:
                    return None
        return frame
    finally:
        cap.release()


def save(image: np.ndarray, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return path
