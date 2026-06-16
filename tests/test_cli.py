"""Tests for the `uhaa` CLI."""

from __future__ import annotations

from pathlib import Path

from unitares_host_adapter import __version__
from unitares_host_adapter.cli import main


def test_version(capsys):
    rc = main(["--version"])
    assert rc == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_help_default(capsys):
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "UNITARES host adapter" in captured.out


def test_spec_prints_existing_spec_path(capsys):
    rc = main(["spec"])
    assert rc == 0
    captured = capsys.readouterr()
    spec_path = Path(captured.out.strip())
    assert spec_path.name == "SPEC.md"
    assert spec_path.exists()


def test_unknown_command(capsys):
    rc = main(["nope"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "unknown command" in captured.err
