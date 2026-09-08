"""Command line entry point."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from touchdown_analyzer import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="touchdown-analyzer",
        description="Analyze touchdown data.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    print("touchdown-analyzer: nothing to do yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
