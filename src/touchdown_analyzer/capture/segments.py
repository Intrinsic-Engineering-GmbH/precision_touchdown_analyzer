"""The raw segment index.

This is what turns a folder of mp4 files into something the analysis side can
address: given a touchdown instant, which file, which frame, which PTS.

Two clocks are deliberately kept apart:

* **Absolute** time comes from the segment filename (recorder wall clock, NTP
  synced). Good to roughly a segment boundary, which is ample for matching a
  landing against an OGN track at ~1 Hz.
* **Relative** time inside a segment comes from the camera's own PTS. This is
  the regular, jitter-free clock the sub-frame touchdown fit needs.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from touchdown_analyzer.capture import ffmpeg as ff
from touchdown_analyzer.capture.recorder import MANIFEST_NAME

log = logging.getLogger(__name__)

INDEX_NAME = "segments.jsonl"
SEGMENT_GLOB = "*.mp4"
FILENAME_FORMAT = "%Y-%m-%d_%H-%M-%S"

# A cut lands on the next keyframe, so segment starts drift from the nominal
# grid by up to one GOP. Anything beyond this is a real recording gap.
GAP_TOLERANCE_S = 1.5


@dataclass(slots=True)
class Segment:
    """One recorded segment, located in absolute time."""

    name: str
    start_utc: str
    duration_s: float
    nb_frames: int | None
    fps: float | None
    size_bytes: int
    sha256: str | None = None

    @property
    def start(self) -> datetime:
        return datetime.fromisoformat(self.start_utc)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration_s)

    def contains(self, when: datetime) -> bool:
        return self.start <= when < self.end


@dataclass(slots=True)
class Location:
    """Where an instant sits inside the recording."""

    segment: Segment
    offset_s: float
    frame_index: int | None


@dataclass(slots=True)
class IndexReport:
    """Continuity summary; the honest quality number for a session."""

    session: str
    segments: int
    first_utc: str | None
    last_utc: str | None
    recorded_s: float
    span_s: float
    gaps: list[dict]
    overlaps: list[dict]
    fps_outliers: list[str]

    @property
    def coverage(self) -> float:
        return self.recorded_s / self.span_s if self.span_s > 0 else 0.0


def _utc_offset(session_dir: Path) -> timezone:
    """Read the recorder's local UTC offset from the session manifest.

    Falling back to UTC silently would put every timestamp in the index out by
    the local offset - hours, in most of Europe - which quietly breaks OGN
    matching and puts the wrong time in every clip name. So each failure is
    logged, and the read is BOM-tolerant: a session.json written by a Windows
    tool (PowerShell's ``Set-Content -Encoding utf8``, say) starts with a BOM
    that plain utf-8 JSON parsing rejects.
    """
    manifest_path = session_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        log.warning(
            "no %s in %s; assuming segment names are UTC, which is wrong if the "
            "recorder ran in a different timezone",
            MANIFEST_NAME,
            session_dir,
        )
        return UTC
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        return timezone(timedelta(seconds=int(manifest["utc_offset_seconds"])))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        log.warning(
            "could not read utc_offset_seconds from %s (%s); assuming UTC, so "
            "timestamps may be out by the recorder's local offset",
            manifest_path,
            exc,
        )
        return UTC


def parse_start(name: str, tz: timezone) -> datetime | None:
    """Turn ``2026-07-18_14-32-07.mp4`` into an absolute instant."""
    try:
        naive = datetime.strptime(Path(name).stem, FILENAME_FORMAT)
    except ValueError:
        return None
    return naive.replace(tzinfo=tz).astimezone(UTC)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_index(
    session_dir: Path,
    ffprobe: str,
    *,
    hash_files: bool = False,
) -> list[Segment]:
    """ffprobe every segment in the session and write ``segments.jsonl``."""
    tz = _utc_offset(session_dir)
    segments: list[Segment] = []

    for path in sorted(session_dir.glob(SEGMENT_GLOB)):
        start = parse_start(path.name, tz)
        if start is None:
            continue
        try:
            info = ff.probe_stream(str(path), ffprobe, timeout=60.0)
        except ff.ProbeError:
            # A truncated final segment (recorder killed rather than stopped)
            # is expected; keep it out of the index rather than trusting it.
            continue
        segments.append(
            Segment(
                name=path.name,
                start_utc=start.isoformat(),
                duration_s=info.duration_s or 0.0,
                nb_frames=info.nb_frames,
                fps=info.avg_fps or info.nominal_fps,
                size_bytes=path.stat().st_size,
                sha256=_sha256(path) if hash_files else None,
            )
        )

    write_index(session_dir, segments)
    return segments


def write_index(session_dir: Path, segments: list[Segment]) -> Path:
    path = session_dir / INDEX_NAME
    with open(path, "w", encoding="utf-8") as fh:
        for segment in segments:
            fh.write(json.dumps(asdict(segment)) + "\n")
    return path


def load_index(session_dir: Path) -> list[Segment]:
    path = session_dir / INDEX_NAME
    if not path.is_file():
        raise FileNotFoundError(f"no segment index at {path}; run `touchdown-analyzer index` first")
    segments = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                segments.append(Segment(**json.loads(line)))
    return sorted(segments, key=lambda s: s.start)


def spans(segments: list[Segment]) -> list[tuple[Segment, datetime, datetime]]:
    """Reconcile each segment's two clocks into one wall-clock extent.

    ``start`` comes from the filename (recorder wall clock) while
    ``duration_s`` comes from the camera's own timestamps, and the two can
    disagree — the camera clock drifting over a long day, or a stream joined
    mid-flight. Where a segment would run past the start of the next one, the
    next filename wins: it is what the recorder actually did. A segment that
    stops short keeps its duration, so genuine gaps stay visible.
    """
    ordered = sorted(segments, key=lambda s: s.start)
    out = []
    for index, segment in enumerate(ordered):
        end = segment.end
        if index + 1 < len(ordered):
            end = min(end, ordered[index + 1].start)
        out.append((segment, segment.start, max(end, segment.start)))
    return out


def resolve(segments: list[Segment], when: datetime) -> Location | None:
    """Locate an absolute instant inside the recording."""
    if when.tzinfo is None:
        raise ValueError("`when` must be timezone-aware")
    for segment, start, end in spans(segments):
        if start <= when < end:
            offset = (when - start).total_seconds()
            frame = int(offset * segment.fps) if segment.fps else None
            return Location(segment=segment, offset_s=offset, frame_index=frame)
    return None


def window(segments: list[Segment], start: datetime, end: datetime) -> list[Segment]:
    """Every segment overlapping ``[start, end)`` — the clip cutter's input."""
    return [s for s, s_start, s_end in spans(segments) if s_end > start and s_start < end]


def report(
    session: str, segments: list[Segment], *, target_fps: float | None = None
) -> IndexReport:
    """Summarise continuity and frame-rate consistency across a session."""
    if not segments:
        return IndexReport(session, 0, None, None, 0.0, 0.0, [], [], [])

    ordered = sorted(segments, key=lambda s: s.start)
    gaps = []
    overlaps = []
    for current, following in zip(ordered, ordered[1:], strict=False):
        delta = (following.start - current.end).total_seconds()
        if delta > GAP_TOLERANCE_S:
            gaps.append(
                {
                    "after": current.name,
                    "before": following.name,
                    "start_utc": current.end.isoformat(),
                    "seconds": delta,
                }
            )
        elif delta < -GAP_TOLERANCE_S:
            # Content outlives the next filename: the camera and recorder
            # clocks disagree. Worth surfacing - it grows with clock drift.
            overlaps.append(
                {
                    "after": current.name,
                    "before": following.name,
                    "seconds": -delta,
                }
            )

    outliers = []
    if target_fps:
        for segment in ordered:
            if segment.fps and abs(segment.fps - target_fps) > 1.0:
                outliers.append(f"{segment.name} ({segment.fps:.1f} fps)")

    extents = spans(ordered)
    last_end = extents[-1][2]
    return IndexReport(
        session=session,
        segments=len(ordered),
        first_utc=ordered[0].start_utc,
        last_utc=last_end.isoformat(),
        # Clamped, so overlapping segments cannot report >100% coverage.
        recorded_s=sum((end - start).total_seconds() for _, start, end in extents),
        span_s=(last_end - ordered[0].start).total_seconds(),
        gaps=gaps,
        overlaps=overlaps,
        fps_outliers=outliers,
    )
