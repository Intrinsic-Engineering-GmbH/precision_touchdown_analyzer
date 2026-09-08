from touchdown_analyzer import __version__
from touchdown_analyzer.cli import main


def test_version_is_set() -> None:
    assert __version__


def test_main_runs() -> None:
    assert main([]) == 0
