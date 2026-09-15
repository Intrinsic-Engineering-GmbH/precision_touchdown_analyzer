"""Installing and uninstalling on Windows. Stdlib only.

Used by the setup program (``packaging/windows/setup_wizard.py``) to put
the bundle in place, and by the installed program (``--uninstall``) to take
it away again.

Two scopes, decided by whether the process is elevated:

* **machine** (the default: ``%ProgramFiles%\\PTA``, data suggested in
  ``%USERPROFILE%\\PTA``) - Add/Remove Programs entry under HKLM, shortcuts
  in the all-users Start menu and on the public desktop, the data
  directory granted to the Users group. Needs administrator rights: the
  setup program and the uninstaller relaunch themselves through UAC when
  the target directory is not writable.
* **user** (a folder under the user's profile, no administrator rights) -
  HKCU entry, the user's own Start menu and desktop.

The chosen data directory (recordings, results, configuration) is written
to ``data-home.txt`` next to the executable, where ``paths.data_home``
reads it. The uninstaller leaves the data alone unless told otherwise.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from touchdown_analyzer import __version__, paths

EXE_NAME = f"{paths.APP_NAME}.exe"
CLI_NAME = "touchdown-analyzer.exe"
PUBLISHER = "Intrinsic Engineering GmbH"
REGISTRY_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{paths.APP_NAME}"
USERS_SID = "*S-1-5-32-545"  # the built-in Users group, independent of the display language
ERROR_CANCELLED = 1223

Scope = Literal["machine", "user"]


# --- where things go --------------------------------------------------------


def default_install_dir() -> Path:
    base = Path(os.environ.get("PROGRAMFILES") or r"C:\Program Files")
    return base / paths.APP_NAME


def default_data_dir() -> Path:
    """``C:\\Users\\<name>\\PTA`` of the person running the setup: easy to
    find in Explorer. The wizard resolves it before elevating, so it is
    that person's folder even when a different account approves UAC."""
    base = Path(os.environ.get("USERPROFILE") or Path.home())
    return base / paths.APP_NAME


def user_install_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / "Programs" / paths.APP_NAME


def start_menu_dir(scope: Scope) -> Path:
    if scope == "machine":
        base = Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData")
    else:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def desktop_dir(scope: Scope) -> Path:
    if scope == "machine":
        return Path(os.environ.get("PUBLIC") or r"C:\Users\Public") / "Desktop"
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"


def shortcut_paths(desktop: bool, start_menu: bool, scope: Scope = "user") -> list[Path]:
    found = []
    if start_menu:
        found.append(start_menu_dir(scope) / f"{paths.APP_TITLE}.lnk")
    if desktop:
        found.append(desktop_dir(scope) / f"{paths.APP_TITLE}.lnk")
    return found


# --- rights ------------------------------------------------------------------


def is_windows() -> bool:
    return sys.platform == "win32"


def is_admin() -> bool:
    """Is this process elevated?"""
    if not is_windows():
        return False
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def writable(path: Path) -> bool:
    """Can this process create files in ``path`` (or, if it does not exist
    yet, in its nearest existing ancestor)?"""
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            return False
        probe = probe.parent
    if not probe.is_dir():
        return False
    try:
        with tempfile.NamedTemporaryFile(dir=probe, prefix=".pta-"):
            pass
    except OSError:
        return False
    return True


def current_scope() -> Scope:
    """Machine-wide when elevated; per-user otherwise."""
    return "machine" if is_admin() else "user"


def run_elevated(args: list[str]) -> int:
    """Run this program again with administrator rights (the UAC prompt),
    wait for it, return its exit code.

    ``ShellExecuteEx`` with the ``runas`` verb is the only supported way
    to trigger UAC from an unelevated process; it hands back a process
    handle to wait on.
    """
    import ctypes
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        )

    see_mask_nocloseprocess = 0x40
    program = sys.executable
    params = list(args) if paths.frozen() else [sys.argv[0], *args]
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = see_mask_nocloseprocess
    info.lpVerb = "runas"
    info.lpFile = program
    info.lpParameters = subprocess.list2cmdline(params)
    info.lpDirectory = str(Path(program).parent)
    info.nShow = 1  # SW_SHOWNORMAL
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == ERROR_CANCELLED:
            raise PermissionError("Administrator rights were not granted.")
        raise OSError(f"could not start the elevated process (error {error})")
    kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(info.hProcess)
    return int(code.value)


# --- shortcuts, registry ----------------------------------------------------


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


def _hive(scope: Scope) -> int:
    import winreg

    return winreg.HKEY_LOCAL_MACHINE if scope == "machine" else winreg.HKEY_CURRENT_USER


def register(install_dir: Path, data_dir: Path, size_bytes: int, scope: Scope) -> None:
    """The Add/Remove Programs entry."""
    import winreg

    exe = install_dir / EXE_NAME
    with winreg.CreateKey(_hive(scope), REGISTRY_KEY) as key:
        for name, value in (
            ("DisplayName", paths.APP_TITLE),
            ("DisplayVersion", __version__),
            ("Publisher", PUBLISHER),
            ("InstallLocation", str(install_dir)),
            ("DataDirectory", str(data_dir)),
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

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        with contextlib.suppress(OSError):
            winreg.DeleteKey(hive, REGISTRY_KEY)


def grant_users(directory: Path) -> None:
    """Let every account on the machine write below ``directory``
    (Program Data is created read-only for non-administrators)."""
    subprocess.run(  # noqa: S603 - fixed program, quoted path
        ["icacls", str(directory), "/grant", f"{USERS_SID}:(OI)(CI)M", "/Q"],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


# --- install / uninstall ------------------------------------------------------


def install(
    bundle: Path,
    install_dir: Path,
    data_dir: Path,
    *,
    desktop: bool = True,
    start_menu: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
) -> Path:
    """Unpack ``bundle`` (a zip of the app folder) into ``install_dir``,
    prepare ``data_dir`` and record it. An existing installation there is
    replaced. Returns the main executable.
    """
    install_dir = install_dir.resolve()
    data_dir = data_dir.resolve()
    if data_dir == install_dir or install_dir in data_dir.parents:
        raise ValueError("The data folder must not be inside the program folder.")
    scope = current_scope()
    install_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle) as archive:
        members = archive.infolist()
        total = len(members)
        for index, member in enumerate(members, 1):
            archive.extract(member, install_dir)
            if progress and (index % 50 == 0 or index == total):
                progress(index, total, member.filename)
    (install_dir / paths.HOME_FILE).write_text(str(data_dir) + "\n", encoding="utf-8")
    for sub in ("config", "data", "logs"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    if scope == "machine":
        grant_users(data_dir)
    exe = install_dir / EXE_NAME
    size = sum(p.stat().st_size for p in install_dir.rglob("*") if p.is_file())
    icon = install_dir / "icon.ico"
    for link in shortcut_paths(desktop, start_menu, scope):
        make_shortcut(link, exe, icon if icon.is_file() else None, paths.APP_TITLE)
    register(install_dir, data_dir, size, scope)
    return exe


def _remove_later(directories: list[Path]) -> None:
    """Delete directories once the programs running from them have exited.

    A hidden PowerShell with its own handles: a detached cmd.exe without a
    console silently does nothing. Started from a neutral directory - the
    program's own working directory is the data directory, and Windows
    refuses to delete any process's working directory.
    """
    removals = "; ".join(
        f"Remove-Item -LiteralPath '{d}' -Recurse -Force -ErrorAction SilentlyContinue"
        for d in directories
    )
    subprocess.Popen(  # noqa: S603 - fixed program, quoted paths
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-Command",
            f"Start-Sleep -Seconds 4; {removals}",
        ],
        cwd=os.environ.get("SYSTEMROOT") or "C:\\",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def uninstall(install_dir: Path | None = None, *, keep_data: bool = True) -> None:
    """Remove shortcuts, the registry entry and the program folder - and,
    on request, the data directory.

    The program folder holds the running executable (and the data
    directory its log), so their removal is handed to a detached command
    that waits for this process to exit first.
    """
    install_dir = install_dir or paths.program_dir()
    data_dir = paths.data_home()
    for scope in ("machine", "user"):
        for link in shortcut_paths(desktop=True, start_menu=True, scope=scope):
            with contextlib.suppress(OSError):
                link.unlink(missing_ok=True)
    unregister()
    doomed = [install_dir] if install_dir.is_dir() else []
    if not keep_data and data_dir.is_dir() and data_dir not in install_dir.parents:
        doomed.append(data_dir)
    if doomed:
        _remove_later(doomed)


def uninstall_main(args: list[str]) -> int:
    """What ``PTA.exe --uninstall [--silent] [--purge]`` does.

    Asks (unless silent), then removes - through UAC when the program
    folder is not writable, which is the case under Program Files.
    """
    silent = "--silent" in args
    keep = "--purge" not in args
    if not silent:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        answer = messagebox.askyesnocancel(
            paths.APP_TITLE,
            f"Remove {paths.APP_TITLE} from this computer?\n\n"
            f"Yes: remove the program, keep recordings and results in\n{paths.data_home()}\n"
            "No: remove the program and the recordings too\n"
            "Cancel: keep everything",
        )
        root.destroy()
        if answer is None:
            return 1
        keep = answer
    install_dir = paths.program_dir()
    if not is_admin() and not writable(install_dir):
        try:
            return run_elevated(["--uninstall", "--silent", *([] if keep else ["--purge"])])
        except PermissionError:
            return 1
    uninstall(install_dir, keep_data=keep)
    return 0
