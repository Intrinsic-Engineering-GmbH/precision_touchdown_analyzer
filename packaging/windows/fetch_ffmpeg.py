"""Fetch ffmpeg.exe and ffprobe.exe into vendor/ffmpeg/ for the installer.

The Windows installer ships its own ffmpeg so nothing has to be installed
on the airfield machine: app.spec copies whatever is in vendor/ffmpeg/ to
tools/ next to the program, where ``paths.bundled_tool`` looks first. This
script fills vendor/ffmpeg/ from the gyan.dev "essentials" release build -
the one the ffmpeg site links for Windows - and checks it against the
publisher's SHA-256 before unpacking. Stdlib only.

    python packaging/windows/fetch_ffmpeg.py            # skips when already there
    python packaging/windows/fetch_ffmpeg.py --force    # re-download

The zip is kept under build/ so a rebuild does not download the ~110 MB
again. The build is GPL; its LICENSE comes along as LICENSE-ffmpeg.txt and
is installed next to the binaries.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor" / "ffmpeg"
CACHE = ROOT / "build" / "ffmpeg-release-essentials.zip"

URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
SHA_URL = URL + ".sha256"

# archive member basename -> file in vendor/ffmpeg/
WANTED = {
    "ffmpeg.exe": "ffmpeg.exe",
    "ffprobe.exe": "ffprobe.exe",
    "LICENSE": "LICENSE-ffmpeg.txt",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("ascii").strip()


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as response, part.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done / 1e6:6.1f} / {total / 1e6:.1f} MB", end="", flush=True)
    print()
    part.replace(dest)


def ensure_archive(force: bool) -> Path:
    """The verified zip in build/, downloading it unless the cached one matches."""
    expected = fetch_text(SHA_URL).split()[0].lower()
    if CACHE.is_file() and not force and sha256(CACHE) == expected:
        print(f"== using cached {CACHE.relative_to(ROOT)}")
        return CACHE
    print(f"== downloading {URL}")
    download(URL, CACHE)
    actual = sha256(CACHE)
    if actual != expected:
        CACHE.unlink()
        raise SystemExit(f"SHA-256 mismatch for {URL}\n  expected {expected}\n  got      {actual}")
    return CACHE


def unpack(archive: Path) -> None:
    VENDOR.mkdir(parents=True, exist_ok=True)
    found: set[str] = set()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            name = Path(member.filename).name
            target = WANTED.get(name)
            # bin/ffmpeg.exe, not some other ffmpeg.exe; LICENSE at the top level.
            if target is None or member.is_dir() or name in found:
                continue
            parts = Path(member.filename).parts
            if name.endswith(".exe") and (len(parts) < 2 or parts[-2] != "bin"):
                continue
            with zf.open(member) as src, (VENDOR / target).open("wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            found.add(name)
    missing = set(WANTED) - found
    if missing:
        raise SystemExit(f"{archive.name} lacks {', '.join(sorted(missing))}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="download and unpack even if present")
    args = parser.parse_args()

    present = all((VENDOR / target).is_file() for target in WANTED.values())
    if present and not args.force:
        print(f"== ffmpeg already in {VENDOR.relative_to(ROOT)} (--force to refresh)")
        return
    if sys.platform != "win32":
        print("note: fetching Windows binaries on a non-Windows host", file=sys.stderr)

    unpack(ensure_archive(args.force))
    for target in WANTED.values():
        path = VENDOR / target
        print(f"  {path.relative_to(ROOT)}  ({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
