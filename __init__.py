"""Hermes Agent directory-plugin entry point for the UNITARES host adapter.

Installed from the Hermes plugin catalog, this repository root is the plugin:
``plugin.yaml`` declares it, and Hermes builds ``pyproject.toml`` (the
``unitares-host-adapter`` package in ``src/``) into its environment when the
plugin is enabled. If the package is not importable (for example under
``hermes plugins validate``, which does not install anything), the pinned
``src/`` tree next to this file is used instead.

The plugin only talks to the server named by ``UNITARES_MCP_URL``. Hermes
refuses to load it while that variable is unset (``requires_env`` in
``plugin.yaml``), and the adapter factory below re-checks it, so there is no
fallback to a default server. ``UNITARES_BEARER`` is sent when set.

Behavior is the binding's default light mode: one lazy onboard at the first
turn, one check-in per completed assistant turn, and session close on finalize
or reset. All hooks are fail-open: a governance error never blocks the agent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

try:
    from unitares_host_adapter.bindings import hermes as _hermes_binding
except ImportError:
    _SRC = Path(__file__).resolve().parent / "src"
    if not (_SRC / "unitares_host_adapter").is_dir():
        raise
    sys.path.insert(0, str(_SRC))
    from unitares_host_adapter.bindings import hermes as _hermes_binding

MCP_URL_ENV = "UNITARES_MCP_URL"
BEARER_ENV = "UNITARES_BEARER"


def _adapter_from_env() -> Any:
    """Build the adapter for the configured server; never a default one."""
    url = os.environ.get(MCP_URL_ENV, "").strip()
    if not url:
        raise RuntimeError(f"{MCP_URL_ENV} is not set; the UNITARES plugin has no server to report to")
    return _hermes_binding.build_adapter(url, bearer=os.environ.get(BEARER_ENV))


def register(ctx: Any) -> None:
    """Register the default UNITARES lifecycle hooks with Hermes."""
    _hermes_binding.register(ctx, adapter_factory=_adapter_from_env)
