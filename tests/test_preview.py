from __future__ import annotations

import subprocess
import sys
import time

import pytest

from touchdown_analyzer.capture import ffmpeg as ff
from touchdown_analyzer.capture import preview

# Two tiny "JPEGs": a start marker, a body, an end marker.
JPEG_A = b"\xff\xd8" + b"A" * 20 + b"\xff\xd9"
JPEG_B = b"\xff\xd8" + b"B" * 30 + b"\xff\xd9"


def _emitter(payload: bytes, *, delay: float = 0.0) -> list[str]:
    """A stand-in for ffmpeg that writes ``payload`` to stdout and exits."""
    code = (
        "import sys,time,binascii;"
        f"time.sleep({delay});"
        f"sys.stdout.buffer.write(binascii.unhexlify('{payload.hex()}'));sys.stdout.flush()"
    )
    return [sys.executable, "-c", code]


def test_frames_are_split_on_jpeg_markers() -> None:
    stream = preview.Preview(_emitter(JPEG_A + JPEG_B))
    parts = list(stream.frames())
    assert len(parts) >= 1
    # The last part carries the last frame; earlier ones may be coalesced by
    # latest-wins, which is the intended behaviour for a viewfinder.
    assert JPEG_B in parts[-1]
    assert parts[-1].startswith(f"--{preview.BOUNDARY}\r\nContent-Type: image/jpeg".encode())
    assert f"Content-Length: {len(JPEG_B)}".encode() in parts[-1]


def test_partial_frame_at_end_is_dropped() -> None:
    truncated = JPEG_A + b"\xff\xd8" + b"C" * 10  # no end marker
    parts = list(preview.Preview(_emitter(truncated)).frames())
    assert all(b"CCCC" not in part for part in parts)


def test_garbage_before_first_marker_is_skipped() -> None:
    parts = list(preview.Preview(_emitter(b"noise" + JPEG_A)).frames())
    assert parts and JPEG_A in parts[-1]


def test_reader_stops_when_the_process_ends() -> None:
    stream = preview.Preview(_emitter(JPEG_A))
    list(stream.frames())
    stream._reader.join(2.0)
    assert not stream.alive


def test_idle_consumer_kills_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """A preview nobody is watching must not keep the camera session open."""
    monkeypatch.setattr(preview, "IDLE_TIMEOUT_S", 0.3)
    # A process that would run for a long time, emitting nothing.
    stream = preview.Preview([sys.executable, "-c", "import time; time.sleep(30)"])
    stream._reader.join(5.0)
    assert not stream.alive
    assert stream._proc.poll() is not None


def test_open_preview_fails_fast_without_video() -> None:
    with pytest.raises(preview.PreviewError):
        _open(_emitter(b""))


def _open(cmd: list[str]) -> preview.Preview:
    """Mirror open_preview's first-frame wait against an arbitrary command."""
    stream = preview.Preview(cmd)
    deadline = time.monotonic() + 2.0
    with stream._cond:
        while stream._seq == 0 and stream.alive and time.monotonic() < deadline:
            stream._cond.wait(0.1)
    if stream._seq == 0:
        stream.close()
        raise preview.PreviewError("no video")
    return stream


def test_command_for_a_file_plays_in_real_time_and_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ff, "major_version", lambda _: 9)
    cmd = preview.build_command("C:/clips/day.mp4", "ffmpeg", fps=8, width=960)
    assert "-re" in cmd and "-stream_loop" in cmd
    assert "-rtsp_transport" not in cmd
    assert "fps=8,scale=960:-2" in cmd


def test_command_for_rtsp_uses_transport_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ff, "major_version", lambda _: 9)
    cmd = preview.build_command("rtsp://cam/x", "ffmpeg", rtsp_transport="udp")
    assert cmd[cmd.index("-rtsp_transport") + 1] == "udp"
    assert "-timeout" in cmd
    assert "-re" not in cmd
    assert cmd[-1] == "pipe:1"


def test_missing_ffmpeg_is_a_preview_error() -> None:
    with pytest.raises(preview.PreviewError, match="could not start"):
        preview.Preview(["definitely-not-a-real-binary-xyz"])


def test_real_ffmpeg_if_available(tmp_path) -> None:
    """Smoke test against a synthetic source when ffmpeg is on PATH."""
    try:
        ffmpeg = ff.find_tool("ffmpeg", None)
    except ff.FfmpegNotFound:
        pytest.skip("ffmpeg not installed")
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=10",
            "-t",
            "2",
            "-pix_fmt",
            "yuv420p",
            str(clip),
        ],
        check=True,
    )
    stream = preview.open_preview(str(clip), ffmpeg, fps=5, width=160, first_frame_timeout=15)
    first = next(stream.frames())
    stream.close()
    assert b"Content-Type: image/jpeg" in first
    assert b"\xff\xd8" in first and first.rstrip(b"\r\n").endswith(b"\xff\xd9")
