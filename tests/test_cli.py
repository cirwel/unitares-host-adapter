"""Tests for the `uhaa` CLI."""

from __future__ import annotations

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


def test_unknown_command(capsys):
    rc = main(["nope"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "unknown command" in captured.err
