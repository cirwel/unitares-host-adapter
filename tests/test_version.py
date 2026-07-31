"""Package version consistency tests."""

from __future__ import annotations

import tomllib
from pathlib import Path

import unitares_host_adapter


def test_runtime_version_matches_package_metadata() -> None:
    """Runtime introspection and build metadata must identify the same release."""

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    assert unitares_host_adapter.__version__ == pyproject["project"]["version"]
