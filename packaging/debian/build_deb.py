"""Build pta_<version>_amd64.deb - from any OS.

A .deb is an ``ar`` archive of ``debian-binary``, ``control.tar.gz`` and
``data.tar.gz``; both are written here with the standard library, so the
package can be built on the Windows machine the project is developed on.
Only *installing* it needs a Debian or Ubuntu box.

What the package does on that box (see the maintainer scripts below):

* ships the project wheel and every dependency wheel for Python 3.12 and
  3.13 (Ubuntu 24.04 / Debian 13) under /opt/pta/wheels
* postinst creates a virtual environment from the system python3 and
  installs those wheels offline - no PyPI access needed at the field
* asks, through debconf, where recordings, results and configuration
  go (default /var/lib/pta; ``dpkg-reconfigure pta`` to change it) and
  writes the answer to /etc/default/pta as TOUCHDOWN_ANALYZER_HOME
* installs a systemd unit (pta.service, disabled by default) that serves
  on 0.0.0.0:8080 from that directory as the system user ``pta``, and a
  desktop entry for the control window; the user who ran the install is
  added to group ``pta`` so the control window can use the same directory
* depends on ffmpeg, python3 (>= 3.12), python3-venv, python3-tk, debconf

    python packaging/debian/build_deb.py            # -> dist/*.deb
    python packaging/debian/build_deb.py --no-download   # reuse build/wheels
"""

from __future__ import annotations

import argparse
import hashlib
import io
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from touchdown_analyzer import __version__  # noqa: E402

PACKAGE = "pta"
PREFIX = f"/opt/{PACKAGE}"
DATA_DIR = f"/var/lib/{PACKAGE}"
PYTHONS = ("3.12", "3.13")
# manylinux2014 (glibc 2.17) wheels run on every Debian/Ubuntu still in
# support; asking for newer tags only doubles the package.
PLATFORMS = ("manylinux2014_x86_64",)

CONTROL = f"""Package: {PACKAGE}
Version: {__version__}
Section: video
Priority: optional
Architecture: amd64
Depends: python3 (>= 3.12), python3-venv, python3-tk, ffmpeg, debconf (>= 0.5) | debconf-2.0
Recommends: ntp | chrony
Installed-Size: {{size_kb}}
Maintainer: Intrinsic Engineering GmbH <f.m.schaad@gmail.com>
Homepage: https://github.com/intrinsic-engineering/touchdown_analyzer
Description: Precision Touchdown Analyzer - glider spot-landing measurement
 Measures where a glider's main wheel first touched the ground, in metres
 from the target line, from one fixed camera: a continuous recorder,
 automatic touchdown detection with an overlay image and a clip per landing,
 OGN identification, a judge's review page and club scoring rules.
 .
 The web server runs as the pta systemd service (disabled by
 default; enable it with systemctl enable --now pta) or from
 the "Precision Touchdown Analyzer" desktop entry. The data directory
 is asked at installation and kept in /etc/default/pta.
"""

DEFAULTS_FILE = f"/etc/default/{PACKAGE}"

TEMPLATES = f"""Template: {PACKAGE}/data_dir
Type: string
Default: {DATA_DIR}
Description: Directory for recordings, results and configuration:
 The Precision Touchdown Analyzer keeps the raw video segments it records,
 the measured landings with their clips, and its configuration below this
 directory. Raw video is large; choose a disk with room for it.
 .
 The directory is created if it does not exist and handed to the system
 user "pta", which runs the web server. Change it later with
 "dpkg-reconfigure {PACKAGE}".

Template: {PACKAGE}/data_dir_invalid
Type: error
Description: The data directory must be an absolute path
 Enter a full path starting with "/", for example {DATA_DIR}.
"""

# The debconf config script: runs before unpacking (apt) and again on
# dpkg-reconfigure. A value already in /etc/default/pta (hand-edited or
# from a previous run) is what the question starts from.
CONFIG = f"""#!/bin/sh
set -e
. /usr/share/debconf/confmodule

if [ -r "{DEFAULTS_FILE}" ]; then
  TOUCHDOWN_ANALYZER_HOME=
  . "{DEFAULTS_FILE}"
  if [ -n "$TOUCHDOWN_ANALYZER_HOME" ]; then
    db_set {PACKAGE}/data_dir "$TOUCHDOWN_ANALYZER_HOME"
  fi
fi

attempts=0
while [ $attempts -lt 3 ]; do
  attempts=$((attempts + 1))
  db_input high {PACKAGE}/data_dir || true
  db_go || true
  db_get {PACKAGE}/data_dir
  case "$RET" in
    /*) break ;;
  esac
  db_input high {PACKAGE}/data_dir_invalid || true
  db_go || true
  db_fset {PACKAGE}/data_dir seen false
done
exit 0
"""

POSTINST = f"""#!/bin/sh
set -e
. /usr/share/debconf/confmodule
PREFIX="{PREFIX}"

case "$1" in
  configure)
    db_get {PACKAGE}/data_dir || true
    DATA="$RET"
    case "$DATA" in
      /*) ;;
      *) echo "{PACKAGE}: data directory '$DATA' is not absolute, using {DATA_DIR}" >&2
         DATA="{DATA_DIR}" ;;
    esac
    DATA=$(printf '%s' "$DATA" | sed 's:/*$::')
    [ -n "$DATA" ] || DATA="{DATA_DIR}"

    if ! getent passwd pta >/dev/null; then
      adduser --system --group --home "$DATA" --no-create-home --quiet pta
    fi
    # Group-writable with the setgid bit: the service (user pta) and the
    # people in group pta (the control window) share one directory.
    mkdir -p "$DATA/config" "$DATA/data" "$DATA/logs"
    chown pta:pta "$DATA" "$DATA/config" "$DATA/data" "$DATA/logs"
    chmod 2775 "$DATA" "$DATA/config" "$DATA/data" "$DATA/logs"
    if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != root ] && ! id -nG "$SUDO_USER" | grep -qw pta; then
      adduser --quiet "$SUDO_USER" pta || true
      echo "{PACKAGE}: $SUDO_USER added to group pta (log out and in again for it to take effect)"
    fi

    # What the service and the control window read. Not a conffile: it
    # is generated from the debconf answer, and the config script reads
    # it back, so a hand edit survives an upgrade too.
    cat > "{DEFAULTS_FILE}" <<EOF
# Precision Touchdown Analyzer - written by the {PACKAGE} package.
# Change it with: dpkg-reconfigure {PACKAGE}
TOUCHDOWN_ANALYZER_HOME=$DATA
EOF

    # A virtual environment from the system python, filled from the wheels
    # shipped in the package - no network needed.
    if [ ! -x "$PREFIX/venv/bin/python" ]; then
      python3 -m venv "$PREFIX/venv"
    fi
    "$PREFIX/venv/bin/python" -m pip install --quiet --no-index --find-links "$PREFIX/wheels" \\
        --upgrade "touchdown_analyzer[ui,analysis]" \\
      || {{ echo "pta: offline wheel install failed (python $(python3 --version 2>&1)); trying PyPI" >&2;
           "$PREFIX/venv/bin/python" -m pip install --quiet --find-links "$PREFIX/wheels" --upgrade "touchdown_analyzer[ui,analysis]"; }}
    ln -sf "$PREFIX/venv/bin/touchdown-analyzer" /usr/bin/touchdown-analyzer
    ln -sf "$PREFIX/bin/launcher" /usr/bin/touchdown-analyzer-launcher
    ln -sf "$PREFIX/bin/launcher" /usr/bin/pta

    if command -v systemctl >/dev/null 2>&1; then
      systemctl daemon-reload || true
      if systemctl is-active --quiet pta 2>/dev/null; then systemctl restart pta || true; fi
      echo "Precision Touchdown Analyzer installed. Web server: systemctl enable --now pta"
      echo "(then http://<this machine>:8080). Data under $DATA."
    fi
    if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database -q || true; fi
    ;;
esac
db_stop || true
exit 0
"""

PRERM = """#!/bin/sh
set -e
if [ "$1" = "remove" ] && command -v systemctl >/dev/null 2>&1; then
  systemctl stop pta 2>/dev/null || true
  systemctl disable pta 2>/dev/null || true
fi
exit 0
"""

POSTRM = f"""#!/bin/sh
set -e
case "$1" in
  remove)
    rm -f /usr/bin/touchdown-analyzer /usr/bin/touchdown-analyzer-launcher /usr/bin/pta
    rm -rf "{PREFIX}/venv"
    if command -v systemctl >/dev/null 2>&1; then systemctl daemon-reload || true; fi
    ;;
  purge)
    # The data directory as configured - from the defaults file, else the
    # debconf answer, else the default.
    DATA="{DATA_DIR}"
    TOUCHDOWN_ANALYZER_HOME=
    if [ -r "{DEFAULTS_FILE}" ]; then
      . "{DEFAULTS_FILE}"
      [ -n "$TOUCHDOWN_ANALYZER_HOME" ] && DATA="$TOUCHDOWN_ANALYZER_HOME"
    fi
    if [ -f /usr/share/debconf/confmodule ]; then
      . /usr/share/debconf/confmodule
      if [ -z "$TOUCHDOWN_ANALYZER_HOME" ] && db_get {PACKAGE}/data_dir; then
        [ -n "$RET" ] && DATA="$RET"
      fi
      db_purge || true
    fi
    rm -f "{DEFAULTS_FILE}"
    case "$DATA" in
      /|"") ;;
      *) rm -rf "$DATA" ;;
    esac
    rm -rf "{PREFIX}"
    if getent passwd pta >/dev/null; then deluser --system --quiet pta || true; fi
    ;;
esac
exit 0
"""

SERVICE = f"""[Unit]
Description=Precision Touchdown Analyzer web server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pta
Group=pta
EnvironmentFile=-{DEFAULTS_FILE}
ExecStart={PREFIX}/bin/serve --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

DESKTOP = f"""[Desktop Entry]
Type=Application
Name=Precision Touchdown Analyzer
Comment=Glider spot-landing measurement - control window
Exec=/usr/bin/pta
Icon={PACKAGE}
Terminal=false
Categories=AudioVideo;Video;Science;
"""

COPYRIGHT = """Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: touchdown_analyzer
Files: *
Copyright: 2026 Intrinsic Engineering GmbH
License: see /opt/pta/LICENSE
"""

# The service's entry point: the data directory from /etc/default/pta
# (systemd's WorkingDirectory= cannot come from an EnvironmentFile, so the
# change of directory happens here).
SERVE = f"""#!/bin/sh
# Precision Touchdown Analyzer web server, as run by pta.service.
set -e
if [ -z "$TOUCHDOWN_ANALYZER_HOME" ] && [ -r "{DEFAULTS_FILE}" ]; then
  . "{DEFAULTS_FILE}"
fi
: "${{TOUCHDOWN_ANALYZER_HOME:={DATA_DIR}}}"
export TOUCHDOWN_ANALYZER_HOME
mkdir -p "$TOUCHDOWN_ANALYZER_HOME/data/raw" "$TOUCHDOWN_ANALYZER_HOME/config"
cd "$TOUCHDOWN_ANALYZER_HOME"
exec "{PREFIX}/venv/bin/touchdown-analyzer" serve --root "$TOUCHDOWN_ANALYZER_HOME/data/raw" "$@"
"""

# The control window: the configured data directory when this user may
# write there (members of group pta), else a per-user one under
# ~/.local/share/pta.
WRAPPER = f"""#!/bin/sh
# Precision Touchdown Analyzer control window (the desktop entry and `pta`).
if [ -z "$TOUCHDOWN_ANALYZER_HOME" ] && [ -r "{DEFAULTS_FILE}" ]; then
  . "{DEFAULTS_FILE}"
fi
if [ -n "$TOUCHDOWN_ANALYZER_HOME" ] && [ -w "$TOUCHDOWN_ANALYZER_HOME" ]; then
  export TOUCHDOWN_ANALYZER_HOME
else
  unset TOUCHDOWN_ANALYZER_HOME
fi
exec "{PREFIX}/venv/bin/touchdown-analyzer-launcher" "$@"
"""


def sh(*cmd: str) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def collect_wheels(wheel_dir: Path, download: bool) -> list[Path]:
    """The project wheel plus every dependency for each supported Python."""
    wheel_dir.mkdir(parents=True, exist_ok=True)
    if download:
        for old in wheel_dir.glob("touchdown_analyzer-*.whl"):
            old.unlink()
        sh(
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--quiet",
            "-w",
            str(wheel_dir),
            str(ROOT),
        )
        for py in PYTHONS:
            abi = "cp" + py.replace(".", "")
            for platform in PLATFORMS:
                sh(
                    sys.executable,
                    "-m",
                    "pip",
                    "download",
                    "--quiet",
                    "--only-binary=:all:",
                    "--dest",
                    str(wheel_dir),
                    "--python-version",
                    py,
                    "--implementation",
                    "cp",
                    "--abi",
                    abi,
                    "--abi",
                    "abi3",
                    "--abi",
                    "none",
                    "--platform",
                    platform,
                    "--platform",
                    "any",
                    f"{ROOT}[ui,analysis]",
                )
    wheels = sorted(wheel_dir.glob("*.whl"))
    if not any(w.name.startswith("touchdown_analyzer-") for w in wheels):
        raise SystemExit("no project wheel in " + str(wheel_dir))
    return wheels


class Tree:
    """Files of data.tar.gz, with Debian-friendly ownership and modes."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()
        self.tar = tarfile.open(  # noqa: SIM115 - closed by close(), which returns the bytes
            fileobj=self.buffer, mode="w:gz", format=tarfile.GNU_FORMAT
        )
        self.md5: list[str] = []
        self.size = 0
        self.dirs: set[str] = set()

    def _dir(self, path: str) -> None:
        parts = path.strip("/").split("/")
        for i in range(1, len(parts) + 1):
            name = "./" + "/".join(parts[:i]) + "/"
            if name in self.dirs:
                continue
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = int(time.time())
            info.uname = info.gname = "root"
            self.tar.addfile(info)
            self.dirs.add(name)

    def add(self, path: str, data: bytes, mode: int = 0o644) -> None:
        self._dir(path.rsplit("/", 1)[0])
        info = tarfile.TarInfo("." + path)
        info.size = len(data)
        info.mode = mode
        info.mtime = int(time.time())
        info.uname = info.gname = "root"
        self.tar.addfile(info, io.BytesIO(data))
        self.md5.append(f"{hashlib.md5(data).hexdigest()}  {path.lstrip('/')}")  # noqa: S324 - dpkg format
        self.size += len(data)

    def close(self) -> bytes:
        self.tar.close()
        return self.buffer.getvalue()


def control_tar(size_kb: int, md5sums: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        for name, text, mode in (
            ("control", CONTROL.replace("{size_kb}", str(size_kb)), 0o644),
            ("md5sums", md5sums, 0o644),
            ("templates", TEMPLATES, 0o644),
            ("config", CONFIG, 0o755),
            ("postinst", POSTINST, 0o755),
            ("prerm", PRERM, 0o755),
            ("postrm", POSTRM, 0o755),
        ):
            data = text.encode("utf-8")
            info = tarfile.TarInfo("./" + name)
            info.size = len(data)
            info.mode = mode
            info.mtime = int(time.time())
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def ar_member(name: str, data: bytes) -> bytes:
    header = f"{name:<16}{int(time.time()):<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
    return header + data + (b"\n" if len(data) % 2 else b"")


def build(out_dir: Path, wheels: list[Path], icon: Path | None) -> Path:
    tree = Tree()
    for wheel in wheels:
        tree.add(f"{PREFIX}/wheels/{wheel.name}", wheel.read_bytes())
    tree.add(f"{PREFIX}/LICENSE", (ROOT / "LICENSE").read_bytes())
    tree.add(f"{PREFIX}/bin/launcher", WRAPPER.encode(), 0o755)
    tree.add(f"{PREFIX}/bin/serve", SERVE.encode(), 0o755)
    tree.add("/lib/systemd/system/pta.service", SERVICE.encode())
    tree.add(f"/usr/share/applications/{PACKAGE}.desktop", DESKTOP.encode())
    tree.add(f"/usr/share/doc/{PACKAGE}/copyright", COPYRIGHT.encode())
    tree.add(f"/usr/share/doc/{PACKAGE}/README.md", (ROOT / "README.md").read_bytes())
    if icon and icon.is_file():
        tree.add(f"/usr/share/icons/hicolor/256x256/apps/{PACKAGE}.png", icon.read_bytes())
    data = tree.close()
    control = control_tar(tree.size // 1024 + 1, "\n".join(tree.md5) + "\n")

    out_dir.mkdir(parents=True, exist_ok=True)
    deb = out_dir / f"{PACKAGE}_{__version__}_amd64.deb"
    deb.write_bytes(
        b"!<arch>\n"
        + ar_member("debian-binary", b"2.0\n")
        + ar_member("control.tar.gz", control)
        + ar_member("data.tar.gz", data)
    )
    return deb


def verify(deb: Path) -> None:
    """Read the package back the way dpkg would and print its manifest."""
    raw = deb.read_bytes()
    assert raw.startswith(b"!<arch>\n")
    pos, members = 8, {}
    while pos < len(raw):
        header = raw[pos : pos + 60]
        name = header[:16].decode().strip()
        size = int(header[48:58].decode().strip())
        members[name] = raw[pos + 60 : pos + 60 + size]
        pos += 60 + size + (size % 2)
    assert list(members) == ["debian-binary", "control.tar.gz", "data.tar.gz"], list(members)
    with tarfile.open(fileobj=io.BytesIO(members["control.tar.gz"])) as tar:
        names = tar.getnames()
        control = tar.extractfile("./control").read().decode()  # type: ignore[union-attr]
    with tarfile.open(fileobj=io.BytesIO(members["data.tar.gz"])) as tar:
        files = [m.name for m in tar.getmembers() if m.isfile()]
    print(f"{deb.name}: {deb.stat().st_size / 1e6:.1f} MB, control members {names}")
    print(control.splitlines()[0], "|", control.splitlines()[1])
    print(f"{len(files)} files, e.g.")
    for name in [f for f in files if not f.endswith(".whl")] + [
        f for f in files if f.endswith(".whl")
    ][:3]:
        print("  ", name)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--no-download", action="store_true", help="reuse the wheels in build/wheels"
    )
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    wheels = collect_wheels(ROOT / "build" / "wheels", download=not args.no_download)
    icon = ROOT / "packaging" / "out" / "icon.png"
    deb = build(args.out, wheels, icon if icon.is_file() else None)
    verify(deb)


if __name__ == "__main__":
    main()
