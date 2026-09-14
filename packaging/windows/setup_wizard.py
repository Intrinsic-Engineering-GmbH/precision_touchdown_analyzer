"""The Windows setup program: a small wizard around winstall.install().

Built as a single executable (packaging/windows/setup.spec) that carries
the application folder as app.zip. Installs per user - no administrator
rights - and registers itself in Add/Remove Programs.

    PTA-Setup.exe            the wizard
    PTA-Setup.exe /S         silent, default folder
    PTA-Setup.exe /S /D=C:\\path   silent, that folder
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from touchdown_analyzer import __version__, paths, winstall


def bundle_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "app.zip"


def silent_install(argv: list[str]) -> int:
    dest = winstall.default_install_dir()
    for arg in argv:
        if arg.upper().startswith("/D="):
            dest = Path(arg[3:].strip('"'))
    winstall.install(bundle_path(), dest, desktop=True, start_menu=True)
    return 0


def wizard() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"{paths.APP_TITLE} {__version__} - Setup")
    root.resizable(False, False)
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass

    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=paths.APP_TITLE, font=("Segoe UI", 14, "bold")).grid(
        row=0, column=0, columnspan=3, sticky="w"
    )
    ttk.Label(
        frame,
        text="Video-based landing measurement for glider spot-landing competitions.\n"
        "The program will be installed for the current user; no administrator rights are needed.",
        wraplength=460,
        justify="left",
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 14))

    ttk.Label(frame, text="Install to").grid(row=2, column=0, sticky="w")
    dest_var = tk.StringVar(value=str(winstall.default_install_dir()))
    entry = ttk.Entry(frame, textvariable=dest_var, width=52)
    entry.grid(row=2, column=1, sticky="ew", padx=6)

    def browse() -> None:
        chosen = filedialog.askdirectory(initialdir=dest_var.get(), mustexist=False)
        if chosen:
            dest_var.set(str(Path(chosen) / paths.APP_TITLE))

    ttk.Button(frame, text="Browse\u2026", command=browse).grid(row=2, column=2)

    desktop_var = tk.BooleanVar(value=True)
    menu_var = tk.BooleanVar(value=True)
    launch_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(frame, text="Desktop shortcut", variable=desktop_var).grid(
        row=3, column=0, columnspan=3, sticky="w", pady=(12, 0)
    )
    ttk.Checkbutton(frame, text="Start menu entry", variable=menu_var).grid(
        row=4, column=0, columnspan=3, sticky="w"
    )
    ttk.Checkbutton(frame, text="Open the control window when finished", variable=launch_var).grid(
        row=5, column=0, columnspan=3, sticky="w"
    )

    progress = ttk.Progressbar(frame, mode="determinate", length=460)
    progress.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(16, 2))
    status = ttk.Label(frame, text="", foreground="#6b7480")
    status.grid(row=7, column=0, columnspan=3, sticky="w")

    buttons = ttk.Frame(frame)
    buttons.grid(row=8, column=0, columnspan=3, sticky="e", pady=(14, 0))
    install_btn = ttk.Button(buttons, text="Install")
    cancel_btn = ttk.Button(buttons, text="Cancel", command=root.destroy)
    install_btn.pack(side="left", padx=(0, 6))
    cancel_btn.pack(side="left")

    def on_progress(done: int, total: int, name: str) -> None:
        root.after(
            0, lambda: (progress.configure(maximum=total, value=done), status.configure(text=name))
        )

    def finish(exe: Path | None, error: str) -> None:
        if error:
            messagebox.showerror(paths.APP_TITLE, f"Installation failed:\n{error}")
            install_btn.configure(state="normal")
            cancel_btn.configure(state="normal")
            return
        status.configure(text=f"Installed to {exe.parent if exe else ''}")
        if launch_var.get() and exe is not None:
            subprocess.Popen([str(exe)], cwd=str(exe.parent))  # noqa: S603 - the program just installed
        messagebox.showinfo(paths.APP_TITLE, "Installation complete.")
        root.destroy()

    def run_install() -> None:
        install_btn.configure(state="disabled")
        cancel_btn.configure(state="disabled")
        dest = Path(dest_var.get().strip())

        def work() -> None:
            try:
                exe = winstall.install(
                    bundle_path(),
                    dest,
                    desktop=desktop_var.get(),
                    start_menu=menu_var.get(),
                    progress=on_progress,
                )
                root.after(0, finish, exe, "")
            except Exception as exc:  # noqa: BLE001 - shown to the user
                root.after(0, finish, None, str(exc))

        threading.Thread(target=work, daemon=True).start()

    install_btn.configure(command=run_install)
    root.mainloop()
    return 0


def main() -> int:
    if any(a.upper() == "/S" for a in sys.argv[1:]):
        return silent_install(sys.argv[1:])
    return wizard()


if __name__ == "__main__":
    raise SystemExit(main())
