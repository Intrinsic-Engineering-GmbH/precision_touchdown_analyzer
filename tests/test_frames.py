from __future__ import annotations

from pathlib import Path

import pytest

from touchdown_analyzer.capture import ffmpeg as ff
from touchdown_analyzer.capture import frames


def _window(tmp_path: Path, first: int = 300, count: int = 60) -> frames.Window:
    paths = [tmp_path / f"{i:05d}.jpg" for i in range(1, count + 1)]
    for path in paths:
        path.write_bytes(b"x")
    return frames.Window(directory=tmp_path, first_frame=first, paths=paths, fps=60.0)


def test_window_maps_frame_numbers_to_files(tmp_path: Path) -> None:
    window = _window(tmp_path)
    assert window.path_for(300) == window.paths[0]
    assert window.path_for(330) == window.paths[30]
    assert window.path_for(359) == window.paths[-1]


def test_window_rejects_frames_outside_it(tmp_path: Path) -> None:
    window = _window(tmp_path)
    assert window.path_for(299) is None
    assert window.path_for(360) is None
    assert window.path_for(-1) is None


def test_window_reports_frame_time(tmp_path: Path) -> None:
    window = _window(tmp_path)
    assert window.time_of(330) == pytest.approx(5.5)
    assert window.time_of(0) == 0.0


def test_window_without_fps_does_not_divide_by_zero(tmp_path: Path) -> None:
    window = _window(tmp_path)
    window.fps = 0.0
    assert window.time_of(330) == 0.0


def test_extract_window_rejects_an_empty_count(tmp_path: Path) -> None:
    with pytest.raises(frames.FrameError, match="at least 1"):
        frames.extract_window(
            tmp_path / "v.mp4", "ffmpeg", tmp_path / "o", first_frame=0, count=0, fps=60
        )


@pytest.mark.parametrize(("major", "expected"), [(9, "-fps_mode"), (6, "-fps_mode"), (4, "-vsync")])
def test_frame_sync_flag_follows_the_ffmpeg_version(
    monkeypatch: pytest.MonkeyPatch, major: int, expected: str
) -> None:
    """-vsync was removed in ffmpeg 9; passing it there aborts the run."""
    monkeypatch.setattr(ff, "major_version", lambda _: major)
    assert ff.frame_sync_flags("ffmpeg")[0] == expected


def test_frame_sync_flag_defaults_to_the_modern_option(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ff, "major_version", lambda _: None)
    assert ff.frame_sync_flags("ffmpeg") == ["-fps_mode", "passthrough"]
