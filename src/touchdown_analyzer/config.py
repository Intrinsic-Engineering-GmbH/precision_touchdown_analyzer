"""Configuration objects for capture.

Deliberately stdlib-only. The recorder runs unattended at the airfield for a
whole flying day, so it carries no dependency that can fail to import. Richer
validation (pydantic) belongs on the analysis side, which is free to pull in
the heavy stack.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

# Camera targets from docs/design.md 2.1 / 2.4. The probe checks against these.
TARGET_WIDTH = 1920
TARGET_HEIGHT = 1080
TARGET_FPS = 60.0
TARGET_GOP = 60

SEGMENT_SECONDS = 10
MIN_FREE_GB = 20.0

_CREDENTIALS = re.compile(r"://[^/@]*@")


def redact(source: str) -> str:
    """Strip ``user:pass@`` from a URL so it is safe to log or serialise."""
    return _CREDENTIALS.sub("://<redacted>@", source)


def is_network_source(source: str) -> bool:
    return source.startswith(("rtsp://", "rtsps://", "http://", "https://", "udp://", "rtp://"))


@dataclass(slots=True)
class RecorderConfig:
    """Everything the segment recorder needs for one session."""

    source: str
    session: str
    root: Path = Path("data/raw")
    segment_seconds: int = SEGMENT_SECONDS
    target_fps: float = TARGET_FPS
    rtsp_transport: str = "tcp"
    min_free_gb: float = MIN_FREE_GB
    duration_s: float = 0.0  # 0 = record until interrupted
    initial_backoff_s: float = 2.0
    max_backoff_s: float = 60.0
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    @property
    def session_dir(self) -> Path:
        return self.root / self.session

    @property
    def segment_pattern(self) -> Path:
        # ffmpeg -strftime uses LOCAL time; session.json records the UTC offset
        # so segments.py can turn these names back into absolute instants.
        return self.session_dir / "%Y-%m-%d_%H-%M-%S.mp4"

    @property
    def safe_source(self) -> str:
        return redact(self.source)

    def with_tools(self, ffmpeg: str, ffprobe: str) -> RecorderConfig:
        return replace(self, ffmpeg=ffmpeg, ffprobe=ffprobe)
