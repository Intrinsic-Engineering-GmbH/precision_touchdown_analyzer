"""The pure parts of installing and packaging: paths, the .ico writer,
the Debian archive layout, the Windows shortcut locations."""

from __future__ import annotations

import importlib.util
import io
import struct
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

from touchdown_analyzer import paths, winstall

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses (3.14) look the module up by name
    spec.loader.exec_module(module)
    return module


# --- paths -------------------------------------------------------------


def test_checkout_uses_its_own_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.ENV_HOME, raising=False)
    assert not paths.frozen()
    assert paths.program_dir() == ROOT
    assert paths.data_home() == ROOT


def test_home_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "home"))
    assert paths.data_home() == tmp_path / "home"
    cwd = Path.cwd()
    try:
        home = paths.enter_data_home()
        assert Path.cwd() == home
        assert (home / "config").is_dir() and (home / "data").is_dir()
    finally:
        import os

        os.chdir(cwd)


def test_frozen_data_home_is_per_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(paths.ENV_HOME, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "x.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.program_dir() == tmp_path / "app"
    home = paths.data_home()
    if sys.platform == "win32":
        assert home == tmp_path / "local" / paths.APP_NAME
    else:
        assert home == tmp_path / "xdg" / "pta"


def test_installed_data_home_comes_from_the_program_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(paths.ENV_HOME, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "x.exe"))
    (tmp_path / "app").mkdir()
    assert paths.configured_home() is None
    (tmp_path / "app" / paths.HOME_FILE).write_text(f"{tmp_path / 'store'}\n", encoding="utf-8")
    assert paths.configured_home() == tmp_path / "store"
    assert paths.data_home() == tmp_path / "store"
    # the environment still wins over the installer's choice
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "env"))
    assert paths.data_home() == tmp_path / "env"
    # an empty file counts as "not configured"
    monkeypatch.delenv(paths.ENV_HOME)
    (tmp_path / "app" / paths.HOME_FILE).write_text(" \n", encoding="utf-8")
    assert paths.configured_home() is None


def test_bundled_tool_next_to_program(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "x.exe"))
    assert paths.bundled_tool("ffmpeg") is None
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / exe).write_bytes(b"")
    assert paths.bundled_tool("ffmpeg") == tmp_path / "tools" / exe


# --- Windows install bookkeeping ---------------------------------------


def test_shortcut_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "me"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "programdata"))
    monkeypatch.setenv("PUBLIC", str(tmp_path / "public"))
    both = winstall.shortcut_paths(desktop=True, start_menu=True)
    assert [p.name for p in both] == [f"{paths.APP_TITLE}.lnk"] * 2
    assert (
        both[0].parent == tmp_path / "roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    )
    assert both[1].parent == tmp_path / "me" / "Desktop"
    assert winstall.shortcut_paths(desktop=False, start_menu=False) == []
    machine = winstall.shortcut_paths(desktop=True, start_menu=True, scope="machine")
    assert (
        machine[0].parent
        == tmp_path / "programdata" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    )
    assert machine[1].parent == tmp_path / "public" / "Desktop"


def test_default_folders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "Program Files"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "Users" / "me"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert winstall.default_install_dir() == tmp_path / "Program Files" / "PTA"
    assert winstall.default_data_dir() == tmp_path / "Users" / "me" / "PTA"
    assert winstall.user_install_dir() == tmp_path / "local" / "Programs" / "PTA"


def test_writable_probes_the_nearest_existing_ancestor(tmp_path: Path) -> None:
    assert winstall.writable(tmp_path / "new" / "deeper")
    (tmp_path / "file").write_text("x")
    assert not winstall.writable(tmp_path / "file")


def test_setup_options_round_trip_through_the_silent_command_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "pf"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "me"))
    setup = _load("setup_wizard", "packaging/windows/setup_wizard.py")
    defaults = setup.parse([])
    assert not defaults.silent
    assert defaults.install_dir == tmp_path / "pf" / "PTA"
    assert defaults.data_dir == tmp_path / "me" / "PTA"
    assert defaults.desktop and defaults.start_menu
    chosen = setup.Options(
        tmp_path / "Program Files" / "PTA",
        tmp_path / "Recordings" / "PTA",
        desktop=False,
        start_menu=True,
        silent=True,
        log=tmp_path / "setup.log",
    )
    assert setup.parse(chosen.argv()) == chosen
    # quoting as the shell may deliver it, case-insensitive switches
    parsed = setup.parse(["/s", f'/d="{tmp_path / "x"}"', "/nomenu"])
    assert parsed.silent and parsed.install_dir == tmp_path / "x" and not parsed.start_menu


# --- the icon ------------------------------------------------------------


def test_ico_has_one_entry_per_size(tmp_path: Path) -> None:
    pytest.importorskip("cv2")
    icon = _load("make_icon", "packaging/make_icon.py")
    images = [icon.render(s) for s in (16, 32, 64)]
    assert all(img.shape == (s, s, 4) for img, s in zip(images, (16, 32, 64), strict=True))
    out = tmp_path / "icon.ico"
    icon.write_ico(out, images)
    raw = out.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", raw[:6])
    assert (reserved, kind, count) == (0, 1, 3)
    for i in range(count):
        w, h, _colours, _res, planes, bpp, size, offset = struct.unpack(
            "<BBBBHHII", raw[6 + 16 * i : 22 + 16 * i]
        )
        assert (w, h, planes, bpp) == ((16, 32, 64)[i], (16, 32, 64)[i], 1, 32)
        assert raw[offset : offset + 8] == b"\x89PNG\r\n\x1a\n"
        assert offset + size <= len(raw)


# --- the Debian archive ---------------------------------------------------


def _ar_members(raw: bytes) -> dict[str, bytes]:
    assert raw.startswith(b"!<arch>\n")
    pos, members = 8, {}
    while pos < len(raw):
        header = raw[pos : pos + 60]
        assert header[58:60] == b"`\n"
        size = int(header[48:58].decode().strip())
        members[header[:16].decode().strip()] = raw[pos + 60 : pos + 60 + size]
        pos += 60 + size + (size % 2)
    return members


def test_ar_member_is_padded_to_even_length() -> None:
    deb = _load("build_deb", "packaging/debian/build_deb.py")
    odd = deb.ar_member("odd", b"abc")
    even = deb.ar_member("even", b"abcd")
    assert len(odd) == 60 + 4 and len(even) == 60 + 4
    members = _ar_members(b"!<arch>\n" + odd + even)
    assert members == {"odd": b"abc", "even": b"abcd"}


def test_tree_round_trip_with_md5sums() -> None:
    deb = _load("build_deb", "packaging/debian/build_deb.py")
    tree = deb.Tree()
    tree.add("/opt/x/bin/run", b"#!/bin/sh\n", 0o755)
    tree.add("/opt/x/wheels/a.whl", b"wheel")
    tree.add("/usr/share/doc/x/copyright", b"(c)")
    with tarfile.open(fileobj=io.BytesIO(tree.close())) as tar:
        members = {m.name: m for m in tar.getmembers()}
    # every directory appears once, before its files, root-owned
    assert list(members)[:3] == ["./opt", "./opt/x", "./opt/x/bin"]
    assert members["./opt/x/bin/run"].mode == 0o755
    assert members["./opt/x/wheels/a.whl"].mode == 0o644
    assert all(m.uname == "root" for m in members.values())
    assert sum(1 for m in members.values() if m.isdir()) == 8
    assert tree.size == len(b"#!/bin/sh\n") + 5 + 3
    assert [line.split("  ")[1] for line in tree.md5] == [
        "opt/x/bin/run",
        "opt/x/wheels/a.whl",
        "usr/share/doc/x/copyright",
    ]


def test_control_tar_holds_the_maintainer_scripts() -> None:
    deb = _load("build_deb", "packaging/debian/build_deb.py")
    with tarfile.open(fileobj=io.BytesIO(deb.control_tar(1234, "md5  a\n"))) as tar:
        members = {m.name: m for m in tar.getmembers()}
        control = tar.extractfile("./control").read().decode()  # type: ignore[union-attr]
        templates = tar.extractfile("./templates").read().decode()  # type: ignore[union-attr]
    assert set(members) == {
        "./control",
        "./md5sums",
        "./templates",
        "./config",
        "./postinst",
        "./prerm",
        "./postrm",
    }
    assert all(members[f"./{s}"].mode == 0o755 for s in ("config", "postinst", "prerm", "postrm"))
    assert "debconf" in control
    assert f"Package: {deb.PACKAGE}" in control
    assert "Installed-Size: 1234" in control
    assert "Architecture: amd64" in control
    assert f"Template: {deb.PACKAGE}/data_dir" in templates
    assert f"Default: {deb.DATA_DIR}" in templates


def test_maintainer_scripts_agree_on_the_defaults_file() -> None:
    deb = _load("build_deb", "packaging/debian/build_deb.py")
    for script in (deb.CONFIG, deb.POSTINST, deb.POSTRM, deb.SERVE, deb.WRAPPER):
        assert deb.DEFAULTS_FILE in script
    assert f"EnvironmentFile=-{deb.DEFAULTS_FILE}" in deb.SERVICE
    assert f"ExecStart={deb.PREFIX}/bin/serve" in deb.SERVICE
    assert "WorkingDirectory" not in deb.SERVICE  # the serve wrapper changes directory
    # the f-strings must have produced plain shell, not leftover braces
    assert "${TOUCHDOWN_ANALYZER_HOME:=" + deb.DATA_DIR + "}" in deb.SERVE
    assert "{{" not in deb.SERVE and "{{" not in deb.POSTINST
