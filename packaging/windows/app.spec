# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller: the application folder - control window, CLI, server, UI pages.

Built by packaging/windows/build.ps1 from the repository root:

    pyinstaller packaging/windows/app.spec --distpath build/dist --workpath build/work

Produces build/dist/PrecisionTouchdownAnalyzer/ with two executables that
share one set of libraries: PrecisionTouchdownAnalyzer.exe (windowed: the
control window; with arguments it runs the CLI, which is how it starts its
own server) and touchdown-analyzer.exe (a console for terminal use).
ffmpeg.exe / ffprobe.exe are bundled from vendor/ffmpeg/ when present.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]
SRC = ROOT / "src"
OUT = ROOT / "packaging" / "out"
sys.path.insert(0, str(SRC))

datas = [
    (str(SRC / "touchdown_analyzer" / "control" / "static"), "touchdown_analyzer/control/static"),
    (str(OUT / "icon.png"), "."),
    (str(OUT / "icon.ico"), "."),
]
for tool in ("ffmpeg.exe", "ffprobe.exe"):
    candidate = ROOT / "vendor" / "ffmpeg" / tool
    if candidate.is_file():
        datas.append((str(candidate), "tools"))

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("touchdown_analyzer")
    + ["cv2", "numpy", "fastapi", "pydantic", "anyio", "starlette", "h11", "click"]
)

gui = Analysis(
    [str(ROOT / "packaging" / "windows" / "entry_gui.py")],
    pathex=[str(SRC)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "PIL", "IPython", "pytest"],
    noarchive=False,
)
cli = Analysis(
    [str(ROOT / "packaging" / "windows" / "entry_cli.py")],
    pathex=[str(SRC)],
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "PIL", "IPython", "pytest"],
    noarchive=False,
)
MERGE((gui, "entry_gui", "PrecisionTouchdownAnalyzer"), (cli, "entry_cli", "touchdown-analyzer"))

gui_pyz = PYZ(gui.pure)
cli_pyz = PYZ(cli.pure)

gui_exe = EXE(
    gui_pyz,
    gui.scripts,
    [],
    exclude_binaries=True,
    name="PrecisionTouchdownAnalyzer",
    icon=str(OUT / "icon.ico"),
    console=False,
    disable_windowed_traceback=False,
    upx=False,
)
cli_exe = EXE(
    cli_pyz,
    cli.scripts,
    [],
    exclude_binaries=True,
    name="touchdown-analyzer",
    icon=str(OUT / "icon.ico"),
    console=True,
    upx=False,
)
COLLECT(
    gui_exe,
    gui.binaries,
    gui.datas,
    cli_exe,
    cli.binaries,
    cli.datas,
    name="PrecisionTouchdownAnalyzer",
)
