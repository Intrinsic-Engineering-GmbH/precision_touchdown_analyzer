"""PyInstaller entry: the control window (no console). Arguments run the CLI."""

from touchdown_analyzer.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
