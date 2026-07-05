"""Tests for Hermes version metadata consistency."""

import tomllib
from pathlib import Path

from hermes_cli import __version__


PROJECT_ROOT = Path(__file__).parents[2]


def test_pyproject_version_matches_cli_version():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["version"] == __version__


def test_uv_lock_editable_package_version_matches_cli_version():
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    hermes_package = next(
        package
        for package in lock["package"]
        if package["name"] == "hermes-agent" and package.get("source") == {"editable": "."}
    )

    assert hermes_package["version"] == __version__
