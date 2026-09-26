"""Tests for the repository-root Hermes directory-plugin entry point.

The Hermes plugin catalog installs this repository root as the plugin, so
``__init__.py`` and ``plugin.yaml`` there are shipped surface: the hooks the
manifest declares must be exactly the hooks ``register()`` installs, and the
plugin must never report to a server the user did not configure.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import unitares_host_adapter
from unitares_host_adapter.bindings import hermes as hermes_binding
from unitares_host_adapter.transport import DEFAULT_MCP_URL

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeCtx:
    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks[name] = callback


def _load_plugin_entry() -> Any:
    spec = importlib.util.spec_from_file_location("unitares_hermes_plugin_entry", REPO_ROOT / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest_list(key: str) -> list[str]:
    """Read a top-level list from plugin.yaml without a YAML dependency.

    Items are either ``- value`` or ``- name: VALUE`` lines directly under the key.
    """
    lines = (REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines()
    values: list[str] = []
    inside = False
    for line in lines:
        if re.match(rf"^{re.escape(key)}:\s*$", line):
            inside = True
            continue
        if inside:
            if line and not line.startswith(" "):
                break
            item = re.match(r"^\s+-\s+(?:name:\s+)?(\S+)\s*$", line)
            if item:
                values.append(item.group(1))
    return values


def _manifest_scalar(key: str) -> str:
    text = (REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", text, re.MULTILINE)
    assert match, key
    return match.group(1).strip('"')


def test_registered_hooks_match_the_manifest_exactly() -> None:
    ctx = FakeCtx()
    _load_plugin_entry().register(ctx)

    assert set(ctx.hooks) == set(_manifest_list("provides_hooks"))
    assert "pre_tool_call" not in ctx.hooks  # gating stays opt-in and out of the catalog build


def test_manifest_requires_the_server_url_and_matches_the_package_version() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert _manifest_list("requires_env") == ["UNITARES_MCP_URL"]
    assert _manifest_list("optional_env") == ["UNITARES_BEARER"]
    assert _manifest_scalar("version") == pyproject["project"]["version"] == unitares_host_adapter.__version__


def test_factory_refuses_without_a_configured_server() -> None:
    entry = _load_plugin_entry()
    for value in (None, "", "   "):
        env = {k: v for k, v in os.environ.items() if k != "UNITARES_MCP_URL"}
        if value is not None:
            env["UNITARES_MCP_URL"] = value
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="UNITARES_MCP_URL"):
                entry._adapter_from_env()


def test_factory_uses_only_the_configured_server_and_bearer() -> None:
    entry = _load_plugin_entry()
    env = {"UNITARES_MCP_URL": " http://127.0.0.1:8767/mcp/ ", "UNITARES_BEARER": "token-123"}
    with patch.dict(os.environ, env, clear=True):
        adapter = entry._adapter_from_env()

    transport = adapter._transport
    assert transport.mcp_url == "http://127.0.0.1:8767/mcp/"
    assert transport.mcp_url != DEFAULT_MCP_URL
    assert transport._bearer == "token-123"


def test_hooks_fail_open_when_the_server_url_is_unset() -> None:
    ctx = FakeCtx()
    _load_plugin_entry().register(ctx)
    env = {k: v for k, v in os.environ.items() if k != "UNITARES_MCP_URL"}
    with patch.dict(os.environ, env, clear=True), patch.object(
        hermes_binding, "_build_default_adapter", side_effect=AssertionError("default server used")
    ):
        assert ctx.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli") is None
        assert ctx.hooks["post_llm_call"](
            session_id="s1", assistant_response="done", model="m", platform="cli"
        ) is None
        ctx.hooks["on_session_finalize"](session_id="s1")


def test_build_adapter_rejects_an_empty_url() -> None:
    for value in ("", "   "):
        with pytest.raises(ValueError):
            hermes_binding.build_adapter(value)


def test_entry_falls_back_to_the_pinned_src_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    # Under `hermes plugins validate` nothing is installed; the entry point must
    # import the adapter from the src/ tree shipped beside it.
    saved = {name: mod for name, mod in sys.modules.items() if name.split(".")[0] == "unitares_host_adapter"}
    for name in saved:
        monkeypatch.delitem(sys.modules, name)
    src = str(REPO_ROOT / "src")
    monkeypatch.setattr(sys, "path", [p for p in sys.path if Path(p).resolve() != Path(src).resolve()])
    real_find_spec = importlib.machinery.PathFinder.find_spec
    first = {"blocked": True}

    def find_spec(name: str, path: Any = None, target: Any = None) -> Any:
        if name == "unitares_host_adapter" and first["blocked"] and src not in sys.path:
            return None  # not installed in this interpreter
        return real_find_spec(name, path, target)

    monkeypatch.setattr(importlib.machinery.PathFinder, "find_spec", staticmethod(find_spec))
    try:
        entry = _load_plugin_entry()
        assert sys.path[0] == src
        assert Path(entry._hermes_binding.__file__).resolve().is_relative_to(Path(src).resolve())
    finally:
        for name in [n for n in sys.modules if n.split(".")[0] == "unitares_host_adapter"]:
            del sys.modules[name]
        sys.modules.update(saved)
