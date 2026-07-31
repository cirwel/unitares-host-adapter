"""Tests for StreamableHTTPTransport — result parsing, env config, header
construction, and the default-adapter wiring. No live server required (the
network path is exercised by a separate manual smoke against a running MCP)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import anyio
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

    t._session = cast(Any, _Session())
    raw = await t.call_tool("health_check", {"x": "y"})
    assert raw == {"ok": True, "echoed": "y"}


@pytest.mark.asyncio
async def test_list_tools_returns_advertised_public_names():
    """Capability checks consume the real MCP tools/list response."""
    t = StreamableHTTPTransport("http://example/mcp/")

    class _Session:
        async def list_tools(self, cursor: str | None = None):
            assert cursor is None
            return SimpleNamespace(
                tools=[SimpleNamespace(name="onboard"), SimpleNamespace(name="sync_state")],
                nextCursor=None,
            )

    t._session = cast(Any, _Session())

    assert await t.list_tools() == {"onboard", "sync_state"}


@pytest.mark.asyncio
async def test_list_tools_follows_mcp_pagination():
    """Capability discovery must not reject tools advertised after page one."""
    t = StreamableHTTPTransport("http://example/mcp/")

    class _Session:
        def __init__(self) -> None:
            self.cursors: list[str | None] = []

        async def list_tools(self, cursor: str | None = None):
            self.cursors.append(cursor)
            if cursor is None:
                return SimpleNamespace(
                    tools=[SimpleNamespace(name="onboard")], nextCursor="page-2"
                )
            return SimpleNamespace(
                tools=[SimpleNamespace(name="sync_state")], nextCursor=None
            )

    session = _Session()
    t._session = cast(Any, session)

    assert await t.list_tools() == {"onboard", "sync_state"}
    assert session.cursors == [None, "page-2"]


@pytest.mark.asyncio
async def test_aclose_exits_existing_anyio_scope_without_nesting_a_new_scope():
    """Transport cleanup must preserve AnyIO cancel-scope LIFO ordering."""
    t = StreamableHTTPTransport("http://example/mcp/")

    class _ScopeContext:
        def __init__(self) -> None:
            self.scope = anyio.CancelScope()
            self.exited = False

        async def __aenter__(self):
            self.scope.__enter__()
            return self

        async def __aexit__(self, *exc: Any) -> None:
            self.scope.__exit__(*exc)
            self.exited = True

    context = _ScopeContext()
    await context.__aenter__()
    t._cm_stack.append(context)
    try:
        await t.aclose()
        assert context.exited is True
    finally:
        if not context.exited:
            context.scope.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_aclose_finishes_all_resources_before_reraising_cancellation():
    """Cancellation from one context must not strand later cleanup or state."""
    t = StreamableHTTPTransport("http://example/mcp/")

    class _Context:
        def __init__(self, *, cancel: bool = False) -> None:
            self.cancel = cancel
            self.exited = False

        async def __aexit__(self, *exc: Any) -> None:
            self.exited = True
            if self.cancel:
                raise asyncio.CancelledError()

    class _HTTPClient:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    normal = _Context()
    cancelling = _Context(cancel=True)
    client = _HTTPClient()
    t._cm_stack.extend([normal, cancelling])
    t._session = cast(Any, object())
    t._http_client = cast(Any, client)

    with pytest.raises(asyncio.CancelledError):
        await t.aclose()

    assert cancelling.exited is True
    assert normal.exited is True
    assert client.closed is True
    assert t._cm_stack == []
    assert t._session is None
    assert t._http_client is None


@pytest.mark.asyncio
async def test_aclose_shields_resources_from_an_active_outer_cancel_scope():
    """Level cancellation must not interrupt cleanup between MCP and HTTP resources."""
    t = StreamableHTTPTransport("http://example/mcp/")

    class _Context:
        def __init__(self) -> None:
            self.exited = False

        async def __aexit__(self, *exc: Any) -> None:
            await anyio.sleep(0)
            self.exited = True

    class _HTTPClient:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            await anyio.sleep(0)
            self.closed = True

    context = _Context()
    client = _HTTPClient()
    completed = False
    with anyio.CancelScope() as outer_scope:
        lifecycle_scope = anyio.CancelScope()
        lifecycle_scope.__enter__()
        t._lifecycle_scope = lifecycle_scope
        t._cm_stack.append(context)
        t._http_client = cast(Any, client)
        outer_scope.cancel()
        await t.aclose()
        completed = True

    assert completed is True
    assert context.exited is True
    assert client.closed is True


@pytest.mark.asyncio
async def test_context_exit_does_not_retry_a_completed_mutation_on_cleanup_error(
    caplog: pytest.LogCaptureFixture,
):
    """An ordinary post-response close error must not make a write look failed."""
    t = StreamableHTTPTransport("http://example/mcp/")

    class _BrokenClose:
        async def __aexit__(self, *exc: Any) -> None:
            raise RuntimeError("close failed")

    t._cm_stack.append(_BrokenClose())

    assert await t.__aexit__(None, None, None) is False
    assert "cleanup failed after MCP operation" in caplog.text
