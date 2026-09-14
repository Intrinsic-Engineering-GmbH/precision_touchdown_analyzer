"""Where an installed copy keeps its things. Stdlib only.

Run from a checkout, everything is relative to the current directory
(``data/``, ``config/``, ``.env``) as before. Installed - under Program
Files on Windows, ``/opt`` on Linux - the program directory is read-only,
so the working files live in a per-user (or, for the systemd service, a
per-system) data directory instead, and the program changes into it at
start-up. Bundled tools (ffmpeg next to the executable) are found here too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "PrecisionTouchdownAnalyzer"
APP_TITLE = "Precision Touchdown Analyzer"
ENV_HOME = "TOUCHDOWN_ANALYZER_HOME"


def frozen() -> bool:
    """Running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def program_dir() -> Path:
    """The directory the program was installed to (or the checkout)."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def data_home() -> Path:
    """Where config/, data/ and .env live.

    ``TOUCHDOWN_ANALYZER_HOME`` wins (the systemd unit sets it); then the
    per-user application-data directory when installed; a checkout uses
    its own directory.
    """
    override = os.environ.get(ENV_HOME, "").strip()
    if override:
        return Path(override)
    if not frozen():
        return program_dir()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / APP_NAME
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "precision-touchdown-analyzer"


def enter_data_home() -> Path:
    """Create the data home and make it the working directory."""
    home = data_home()
    for sub in ("config", "data"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    os.chdir(home)
    return home


def bundled_tool(name: str) -> Path | None:
    """A tool shipped next to the program (``tools/ffmpeg.exe``), if any."""
    exe = f"{name}.exe" if sys.platform == "win32" else name
    for base in (program_dir(), Path(getattr(sys, "_MEIPASS", program_dir()))):
        candidate = base / "tools" / exe
        if candidate.is_file():
            return candidate
    return None
