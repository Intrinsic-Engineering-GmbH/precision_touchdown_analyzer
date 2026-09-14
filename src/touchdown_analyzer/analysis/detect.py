"""Foreground blobs and a small tracker (docs/design.md 3.1).

The camera is fixed, so background subtraction is enough to *find* an
aircraft: MOG2 on a downscaled frame, morphology, contours, size filter. What
comes out is a list of :class:`Blob` per frame, each carrying its silhouette
so the contact-point extraction can work on it without a second pass.

Tracking is nearest-neighbour association against a constant-velocity
prediction. A glider crosses the ~40 m window in about 1.5 s and rarely
shares it with anything else, so SORT-style Kalman machinery would be more
code than problem. The tracker keeps one :class:`Track` per moving object
and hands it back once the object is gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

# Detection resolution. Half of 1080p keeps a glider silhouette ~200 px wide,
# ample for finding it, at a quarter of the cost.
DEFAULT_SCALE = 0.5

# MOG2 settings. A short history (2 s at 60 fps) means the background adapts
# to lighting changes quickly; a landing is over well before it can absorb an
# aircraft. MOG2's own shadow flag is not used: it misses a hard sunlit
# shadow on grass, so shadows are split off later at full resolution
# (contact.py) and here the shadow simply stays part of the blob.
MOG_HISTORY = 120
MOG_VAR_THRESHOLD = 24

# Blob filters, in full-resolution pixels.
MIN_BLOB_AREA = 600  # birds and grass flicker are far smaller
MAX_MISSES = 5  # frames a track survives without a detection
GATE_PX = 220  # max distance between prediction and detection at 60 fps


@dataclass(slots=True)
class Blob:
    """One foreground region in one frame, in full-resolution coordinates."""

    x: int
    y: int
    w: int
    h: int
    area: float
    mask: np.ndarray  # bool (h_s, w_s) silhouette at detection scale
    scale: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def touches_edge(self, width: int, margin: int = 2) -> bool:
        """Part of the aircraft is outside the frame; its geometry is unreliable."""
        return self.x <= margin or self.x + self.w >= width - margin


class Detector:
    """MOG2 background subtraction returning the moving blobs of each frame."""

    def __init__(
        self,
        *,
        scale: float = DEFAULT_SCALE,
        min_area: float = MIN_BLOB_AREA,
        history: int = MOG_HISTORY,
        var_threshold: float = MOG_VAR_THRESHOLD,
    ) -> None:
        self.scale = scale
        self.min_area = min_area
        self._mog = cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=var_threshold, detectShadows=False
        )
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self.frames_seen = 0
        self._background: np.ndarray | None = None
        self._background_frame = -1

    def background(self, shape: tuple[int, ...]) -> np.ndarray:
        """The background model at full resolution, for the current frame."""
        if self._background is None or self._background_frame != self.frames_seen:
            small = self._mog.getBackgroundImage()
            height, width = shape[:2]
            self._background = cv2.resize(small, (width, height), interpolation=cv2.INTER_LINEAR)
            self._background_frame = self.frames_seen
        return self._background

    def apply(self, frame: np.ndarray) -> list[Blob]:
        """Blobs of one BGR frame, largest first."""
        if self.scale != 1.0:
            small = cv2.resize(
                frame, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA
            )
        else:
            small = frame
        mask = self._mog.apply(small)
        self.frames_seen += 1

        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel)
        # Two closings bridge the thin wing and the fuselage into one region.
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._kernel, iterations=2)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        inv = 1.0 / self.scale
        blobs: list[Blob] = []
        for contour in contours:
            area = cv2.contourArea(contour) * inv * inv
            if area < self.min_area:
                continue
            xs, ys, ws, hs = cv2.boundingRect(contour)
            roi = mask[ys : ys + hs, xs : xs + ws] > 0
            blobs.append(
                Blob(
                    x=int(round(xs * inv)),
                    y=int(round(ys * inv)),
                    w=int(round(ws * inv)),
                    h=int(round(hs * inv)),
                    area=float(area),
                    mask=roi,
                    scale=self.scale,
                )
            )
        blobs.sort(key=lambda b: b.area, reverse=True)
        return blobs


@dataclass(slots=True)
class Observation:
    """A blob assigned to a track at a given frame.

    The per-frame measurements are filled in by the pipeline right after
    association, while the frame is still in hand; the blob's coarse mask is
    then no longer needed.
    """

    index: int  # absolute frame counter of the pipeline
    blob: Blob
    clipped: bool = False
    profile: object | None = None  # contact.BellyProfile, once measured


@dataclass(slots=True)
class Track:
    """One moving object across consecutive frames."""

    id: int
    observations: list[Observation] = field(default_factory=list)
    misses: int = 0

    @property
    def last(self) -> Observation:
        return self.observations[-1]

    @property
    def first_index(self) -> int:
        return self.observations[0].index

    @property
    def last_index(self) -> int:
        return self.observations[-1].index

    @property
    def peak_area(self) -> float:
        return max(o.blob.area for o in self.observations)

    def velocity(self) -> tuple[float, float]:
        """Pixels per frame over the last few observations."""
        if len(self.observations) < 2:
            return 0.0, 0.0
        recent = self.observations[-4:]
        first, last = recent[0], recent[-1]
        frames = max(1, last.index - first.index)
        return (
            (last.blob.cx - first.blob.cx) / frames,
            (last.blob.cy - first.blob.cy) / frames,
        )

    def predict(self, index: int) -> tuple[float, float]:
        vx, vy = self.velocity()
        ahead = index - self.last.index
        return self.last.blob.cx + vx * ahead, self.last.blob.cy + vy * ahead

    def span_px(self) -> float:
        """Horizontal distance travelled, the cheapest 'is it moving' test."""
        xs = [o.blob.cx for o in self.observations]
        return max(xs) - min(xs)


class Tracker:
    """Associates blobs frame to frame and releases finished tracks."""

    def __init__(self, *, gate_px: float = GATE_PX, max_misses: int = MAX_MISSES) -> None:
        self.gate_px = gate_px
        self.max_misses = max_misses
        self.active: list[Track] = []
        self._next_id = 1

    def update(self, index: int, blobs: list[Blob]) -> tuple[list[Track], list[Track]]:
        """Feed one frame's blobs.

        Returns ``(updated, finished)``: the tracks that received a blob this
        frame (their ``last`` observation is new) and the ones that ended.
        """
        updated: list[Track] = []
        unmatched = list(blobs)
        # Greedy: the longest-lived track chooses first, which keeps an
        # established aircraft from being stolen by a new bird nearby.
        for track in sorted(self.active, key=lambda t: len(t.observations), reverse=True):
            px, py = track.predict(index)
            best, best_d = None, self.gate_px
            for blob in unmatched:
                d = float(np.hypot(blob.cx - px, blob.cy - py))
                if d < best_d:
                    best, best_d = blob, d
            if best is None:
                track.misses += 1
                continue
            unmatched.remove(best)
            track.observations.append(Observation(index, best))
            track.misses = 0
            updated.append(track)

        for blob in unmatched:
            track = Track(id=self._next_id, observations=[Observation(index, blob)])
            self._next_id += 1
            self.active.append(track)
            updated.append(track)

        finished = [t for t in self.active if t.misses > self.max_misses]
        self.active = [t for t in self.active if t.misses <= self.max_misses]
        return updated, finished

    def flush(self) -> list[Track]:
        """End every track, e.g. at the end of the last segment."""
        finished, self.active = self.active, []
        return finished
