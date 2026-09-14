"""PyInstaller entry: the command line (with a console), for a terminal."""

from touchdown_analyzer import paths
from touchdown_analyzer.cli import main

if __name__ == "__main__":
    paths.enter_data_home()
    raise SystemExit(main())
