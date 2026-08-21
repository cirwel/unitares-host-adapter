"""Package version consistency tests."""

from __future__ import annotations

import tomllib
from pathlib import Path

import unitares_host_adapter
from packaging.requirements import Requirement
from packaging.version import Version


def test_runtime_version_matches_package_metadata() -> None:
    """Runtime introspection and build metadata must identify the same release."""

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    assert unitares_host_adapter.__version__ == pyproject["project"]["version"]


def test_declared_mcp_range_covers_supported_major_versions() -> None:
    """Install metadata must admit both tested MCP client protocol generations."""
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    requirement_text = next(
        item
        for item in pyproject["project"]["dependencies"]
        if Requirement(item).name == "mcp"
    )
    specifier = Requirement(requirement_text).specifier

    assert Version("1.26.0") in specifier
    assert Version("2.0.0") in specifier
    assert Version("3.0.0") not in specifier
