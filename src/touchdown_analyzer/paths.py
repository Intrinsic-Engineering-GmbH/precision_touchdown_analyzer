"""Where an installed copy keeps its things. Stdlib only.

Run from a checkout, everything is relative to the current directory
(``data/``, ``config/``, ``.env``) as before. Installed - under Program
Files on Windows, ``/opt`` on Linux - the program directory is read-only,
so the working files live in a data directory instead, and the program
changes into it at start-up. The Windows installer asks for that directory
and records it in ``data-home.txt`` next to the executable; the Debian
package asks through debconf and writes ``/etc/default/pta``. Bundled tools
(ffmpeg next to the executable) are found here too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "PTA"  # short: folder, executable and registry names
APP_TITLE = "Precision Touchdown Analyzer"
ENV_HOME = "TOUCHDOWN_ANALYZER_HOME"
HOME_FILE = "data-home.txt"  # in the program directory: the installer's choice of data directory


def frozen() -> bool:
    """Running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def program_dir() -> Path:
    """The directory the program was installed to (or the checkout)."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def configured_home() -> Path | None:
    """The data directory the installer recorded next to the program, if any."""
    try:
        text = (program_dir() / HOME_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(text) if text else None


def data_home() -> Path:
    """Where config/, data/ and .env live.

    ``TOUCHDOWN_ANALYZER_HOME`` wins (the systemd unit sets it); then the
    directory chosen at installation (``data-home.txt`` next to the
    program); then the per-user application-data directory when installed;
    a checkout uses its own directory.
    """
    override = os.environ.get(ENV_HOME, "").strip()
    if override:
        return Path(override)
    if not frozen():
        return program_dir()
    configured = configured_home()
    if configured is not None:
        return configured
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / APP_NAME
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "pta"


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
