# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller: the setup program, one file, with the application zipped inside.

    pyinstaller packaging/windows/setup.spec --distpath dist --workpath build/work-setup
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]
SRC = ROOT / "src"
OUT = ROOT / "packaging" / "out"
sys.path.insert(0, str(SRC))

from touchdown_analyzer import __version__  # noqa: E402

wizard = Analysis(
    [str(ROOT / "packaging" / "windows" / "setup_wizard.py")],
    pathex=[str(SRC)],
    datas=[(str(ROOT / "build" / "app.zip"), ".")],
    hiddenimports=["touchdown_analyzer.winstall", "touchdown_analyzer.paths"],
    excludes=["numpy", "cv2", "fastapi", "uvicorn", "matplotlib", "PIL", "pytest"],
)
pyz = PYZ(wizard.pure)
EXE(
    pyz,
    wizard.scripts,
    wizard.binaries,
    wizard.datas,
    name=f"PTA-Setup-{__version__}",
    icon=str(OUT / "icon.ico"),
    console=False,
    upx=False,
)
