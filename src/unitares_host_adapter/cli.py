"""`uhaa` — CLI entry point for the UNITARES host adapter.

v0.1 surface is deliberately tiny:

    uhaa --version     # print adapter version
    uhaa spec          # print path to the delivery-surface spec

Real subcommands (install, gate, annotate, checkin) land in v0.2 once the
default MCP transport is wired up.
"""

from __future__ import annotations

import sys
from pathlib import Path

from unitares_host_adapter import __version__


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print(_help_text())
        return 0

    if args[0] in ("-V", "--version", "version"):
        print(f"unitares-host-adapter {__version__}")
        return 0

    if args[0] == "spec":
        spec_path = Path(__file__).resolve().parents[2] / "SPEC.md"
        print(spec_path if spec_path.exists() else "spec bundled with source distribution only")
        return 0

    print(f"unknown command: {args[0]}", file=sys.stderr)
    print(_help_text(), file=sys.stderr)
    return 2


def _help_text() -> str:
    return (
        "uhaa — UNITARES host adapter\n"
        "\n"
        "usage:\n"
        "  uhaa --version   print adapter version\n"
        "  uhaa spec        show path to the delivery-surface spec\n"
        "\n"
        "Real subcommands (install, gate, annotate, checkin) land in v0.2.\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
