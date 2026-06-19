import sys
from pathlib import Path

import tmuxbot
from tmuxbot.__main__ import run

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def test_package_version_matches_pyproject():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    assert tmuxbot.__version__ == pyproject["project"]["version"]


def test_python_version_classifier_matches_runtime_floor():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    assert pyproject["project"]["requires-python"] == ">=3.10"
    assert sys.version_info >= (3, 10)


def test_cli_version_exits_without_starting_service(capsys):
    try:
        run(["--version"])
    except SystemExit as exc:
        assert exc.code == 0

    assert f"tmuxbot {tmuxbot.__version__}" in capsys.readouterr().out
