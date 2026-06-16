"""Tests for StreamableHTTPTransport — result parsing, env config, header
construction, and the default-adapter wiring. No live server required (the
network path is exercised by a separate manual smoke against a running MCP)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from unitares_host_adapter import StreamableHTTPTransport, TransportError, UnitaresAdapter
from unitares_host_adapter.transport import DEFAULT_MCP_URL, _parse_result


def _content(*texts: str) -> SimpleNamespace:
    return SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text=t) for t in texts],
    )


# --- _parse_result ----------------------------------------------------------

def test_parse_merges_json_dict_blocks():
    raw = _parse_result(_content('{"client_session_id": "csid-1", "verdict": "safe"}'))
    assert raw == {"client_session_id": "csid-1", "verdict": "safe"}


def test_parse_merges_multiple_json_blocks():
    raw = _parse_result(_content('{"a": 1}', '{"b": 2}'))
    assert raw == {"a": 1, "b": 2}


def test_parse_non_json_falls_back_to_text():
    raw = _parse_result(_content("plain status line"))
    assert raw == {"text": "plain status line", "raw": True}


def test_parse_empty_content():
    raw = _parse_result(SimpleNamespace(isError=False, content=[]))
    assert raw["success"] is False


def test_parse_raises_on_is_error():
    result = SimpleNamespace(isError=True, content=[SimpleNamespace(text="boom")])
    with pytest.raises(TransportError, match="boom"):
        _parse_result(result)


# --- env / header config ----------------------------------------------------

def test_from_env_defaults(monkeypatch):
    monkeypatch.delenv("UNITARES_MCP_URL", raising=False)
    monkeypatch.delenv("UNITARES_BEARER", raising=False)
    t = StreamableHTTPTransport.from_env()
    assert t.mcp_url == DEFAULT_MCP_URL
    assert t._headers() == {}


def test_from_env_reads_url_and_bearer(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_URL", "http://127.0.0.1:8767/mcp/")
    monkeypatch.setenv("UNITARES_BEARER", "tok-123")
    t = StreamableHTTPTransport.from_env()
    assert t.mcp_url == "http://127.0.0.1:8767/mcp/"
    assert t._headers() == {"Authorization": "Bearer tok-123"}


def test_construction_does_no_io():
    # Building the transport must not open a connection — connect() is lazy.
    t = StreamableHTTPTransport("http://example/mcp/")
    assert t._session is None
    assert t._http_client is None


# --- default-adapter wiring -------------------------------------------------

def test_build_default_adapter_uses_per_call_streamable_transport(monkeypatch):
    from unitares_host_adapter.bindings import hermes

    monkeypatch.setenv("UNITARES_MCP_URL", "http://127.0.0.1:8767/mcp/")
    adapter = hermes._build_default_adapter()
    assert isinstance(adapter, UnitaresAdapter)
    # Hermes hooks are synchronous, so the default binding uses a per-call
    # wrapper instead of a long-lived StreamableHTTPTransport session.
    assert adapter._transport.__class__.__name__ == "_PerCallStreamableHTTPTransport"
    assert getattr(adapter._transport, "mcp_url") == "http://127.0.0.1:8767/mcp/"


@pytest.mark.asyncio
async def test_call_tool_parses_via_transport():
    # Drive call_tool with a stubbed session so the parse path is covered
    # without a network round-trip.
    t = StreamableHTTPTransport("http://example/mcp/")

    class _Session:
        async def call_tool(self, name: str, arguments: dict[str, Any]):
            return _content('{"ok": true, "echoed": "%s"}' % arguments.get("x", ""))

    t._session = _Session()
    raw = await t.call_tool("health_check", {"x": "y"})
    assert raw == {"ok": True, "echoed": "y"}
