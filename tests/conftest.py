from __future__ import annotations

from pathlib import Path

import pytest

from touchdown_analyzer import config


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every test away from the real .env and CAMERA_URL.

    The saved camera URL carries a password and lives in the repo's .env;
    a test that resolved a blank source would otherwise read it, and one that
    remembered a source would overwrite it.
    """
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.delenv(config.SOURCE_KEY, raising=False)
    return env_file
