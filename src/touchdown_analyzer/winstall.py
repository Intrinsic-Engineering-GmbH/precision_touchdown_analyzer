"""Installing and uninstalling on Windows, per user. Stdlib only.

Used by the setup program (``packaging/windows/setup_wizard.py``) to put
the bundle in place, and by the installed program (``--uninstall``) to take
it away again. Per-user - under ``%LOCALAPPDATA%\\Programs`` - so no
administrator rights are needed and Add/Remove Programs still lists it.
Recordings and results live in ``%LOCALAPPDATA%\\PTA``
and are left alone by the uninstaller.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

from touchdown_analyzer import __version__, paths

EXE_NAME = f"{paths.APP_NAME}.exe"
CLI_NAME = "touchdown-analyzer.exe"
PUBLISHER = "Intrinsic Engineering GmbH"
REGISTRY_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{paths.APP_NAME}"


def default_install_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / "Programs" / paths.APP_NAME


def start_menu_dir() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def desktop_dir() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"


def shortcut_paths(desktop: bool, start_menu: bool) -> list[Path]:
    found = []
    if start_menu:
        found.append(start_menu_dir() / f"{paths.APP_TITLE}.lnk")
    if desktop:
        found.append(desktop_dir() / f"{paths.APP_TITLE}.lnk")
    return found


def make_shortcut(link: Path, target: Path, icon: Path | None, description: str) -> None:
    """A .lnk via the Windows Script Host - no pywin32 needed."""
    link.parent.mkdir(parents=True, exist_ok=True)
    icon_line = f"$s.IconLocation = '{icon}'" if icon else ""
    script = (
        "$w = New-Object -ComObject WScript.Shell; "
        f"$s = $w.CreateShortcut('{link}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.WorkingDirectory = '{target.parent}'; "
        f"$s.Description = '{description}'; "
        f"{icon_line}; $s.Save()"
    )
    subprocess.run(  # noqa: S603 - fixed program, quoted paths
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def register(install_dir: Path, size_bytes: int) -> None:
    """The Add/Remove Programs entry, per user."""
    import winreg

    exe = install_dir / EXE_NAME
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
        for name, value in (
            ("DisplayName", paths.APP_TITLE),
            ("DisplayVersion", __version__),
            ("Publisher", PUBLISHER),
            ("InstallLocation", str(install_dir)),
            ("DisplayIcon", str(exe)),
            ("UninstallString", f'"{exe}" --uninstall'),
            ("QuietUninstallString", f'"{exe}" --uninstall --silent'),
        ):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, int(size_bytes // 1024))


def unregister() -> None:
    import winreg

    with contextlib.suppress(OSError):
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY)


def install(
    bundle: Path,
    install_dir: Path,
    *,
    desktop: bool = True,
    start_menu: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
) -> Path:
    """Unpack ``bundle`` (a zip of the app folder) into ``install_dir``.

    An existing installation there is replaced. Returns the main executable.
    """
    install_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle) as archive:
        members = archive.infolist()
        total = len(members)
        for index, member in enumerate(members, 1):
            archive.extract(member, install_dir)
            if progress and (index % 50 == 0 or index == total):
                progress(index, total, member.filename)
    exe = install_dir / EXE_NAME
    size = sum(p.stat().st_size for p in install_dir.rglob("*") if p.is_file())
    icon = install_dir / "icon.ico"
    for link in shortcut_paths(desktop, start_menu):
        make_shortcut(link, exe, icon if icon.is_file() else None, paths.APP_TITLE)
    register(install_dir, size)
    return exe


def uninstall(install_dir: Path | None = None, *, keep_data: bool = True) -> None:
    """Remove shortcuts, the registry entry and the program folder.

    The folder holds the running executable, so its removal is handed to a
    detached command that waits for this process to exit first.
    """
    install_dir = install_dir or paths.program_dir()
    for link in shortcut_paths(desktop=True, start_menu=True):
        link.unlink(missing_ok=True)
    unregister()
    if not keep_data:
        shutil.rmtree(paths.data_home(), ignore_errors=True)
    if install_dir.is_dir():
        # A hidden PowerShell with its own handles: a detached cmd.exe
        # without a console silently does nothing.
        script = f"Start-Sleep -Seconds 3; Remove-Item -LiteralPath '{install_dir}' -Recurse -Force"
        subprocess.Popen(  # noqa: S603 - fixed program, quoted path
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                script,
            ],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def uninstall_interactive(silent: bool) -> int:
    """What ``PTA.exe --uninstall`` does."""
    if not silent:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        keep = messagebox.askyesnocancel(
            paths.APP_TITLE,
            f"Remove {paths.APP_TITLE} from this computer?\n\n"
            f"Yes: remove the program, keep recordings and results in\n{paths.data_home()}\n"
            "No: remove the program and the recordings too\n"
            "Cancel: keep everything",
        )
        root.destroy()
        if keep is None:
            return 1
        uninstall(keep_data=keep)
        return 0
    uninstall(keep_data=True)
    return 0


def is_windows() -> bool:
    return sys.platform == "win32"
