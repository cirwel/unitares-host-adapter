"""Tests for StreamableHTTPTransport — result parsing, env config, header
construction, and the default-adapter wiring. No live server required (the
network path is exercised by a separate manual smoke against a running MCP)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import anyio
import pytest
from mcp.types import CallToolResult, TextContent

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


def test_parse_raises_on_installed_sdk_tool_error_shape():
    """Tool errors must survive both MCP 1 wire aliases and MCP 2 field names."""
    result = CallToolResult(
        content=[TextContent(type="text", text="denied")],
        isError=True,
    )

    with pytest.raises(TransportError, match="denied"):
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
async def test_connect_accepts_two_stream_mcp_transport_shape(monkeypatch):
    """MCP 2 yields read/write streams without the legacy session-id callback."""
    import unitares_host_adapter.transport as transport_module

    class _HTTPClient:
        async def aclose(self) -> None:
            return None

    class _TransportContext:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _SessionContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def initialize(self) -> None:
            return None

    http_client = _HTTPClient()
    monkeypatch.setattr(
        transport_module,
        "create_mcp_http_client",
        lambda **kwargs: http_client,
        raising=False,
    )
    legacy_httpx = getattr(transport_module, "httpx", None)
    if legacy_httpx is not None:
        monkeypatch.setattr(
            legacy_httpx,
            "AsyncClient",
            lambda **kwargs: http_client,
        )
    monkeypatch.setattr(
        transport_module,
        "streamable_http_client",
        lambda *args, **kwargs: _TransportContext(),
    )
    monkeypatch.setattr(
        transport_module,
        "ClientSession",
        lambda *args: _SessionContext(),
    )

    transport = StreamableHTTPTransport("http://example/mcp/")
    await transport.connect()
    try:
        assert transport._session is not None
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_connect_uses_mcp_http_client_factory(monkeypatch):
    """The MCP SDK must choose its matching httpx/httpx2 client implementation."""
    import unitares_host_adapter.transport as transport_module

    factory_calls: list[dict[str, Any]] = []

    class _HTTPClient:
        async def aclose(self) -> None:
            return None

    class _TransportContext:
        async def __aenter__(self):
            return object(), object(), lambda: None

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _SessionContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def initialize(self) -> None:
            return None

    http_client = _HTTPClient()

    def create_http_client(**kwargs: Any) -> _HTTPClient:
        factory_calls.append(kwargs)
        return http_client

    def streamable_client(*args: Any, **kwargs: Any) -> _TransportContext:
        assert kwargs["http_client"] is http_client
        return _TransportContext()

    monkeypatch.setattr(
        transport_module,
        "create_mcp_http_client",
        create_http_client,
        raising=False,
    )
    legacy_httpx = getattr(transport_module, "httpx", None)
    if legacy_httpx is not None:
        monkeypatch.setattr(
            legacy_httpx,
            "AsyncClient",
            lambda **kwargs: pytest.fail("legacy httpx client constructor was used"),
        )
    monkeypatch.setattr(transport_module, "streamable_http_client", streamable_client)
    monkeypatch.setattr(
        transport_module,
        "ClientSession",
        lambda *args: _SessionContext(),
    )

    transport = StreamableHTTPTransport(
        "http://example/mcp/",
        bearer="private-test-token",
    )
    await transport.connect()
    try:
        assert factory_calls == [
            {"headers": {"Authorization": "Bearer private-test-token"}}
        ]
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_list_tools_returns_advertised_public_names():
    """Capability checks consume the real MCP tools/list response."""
    t = StreamableHTTPTransport("http://example/mcp/")

    class _Session:
        async def list_tools(self, *, params=None):
            assert params is None
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

        async def list_tools(self, *, params=None):
            cursor = None if params is None else params.cursor
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
async def test_list_tools_uses_paginated_params_and_snake_case_cursor():
    """MCP 2 accepts pagination through params and returns snake-case cursors."""
    t = StreamableHTTPTransport("http://example/mcp/")

    class _Session:
        def __init__(self) -> None:
            self.cursors: list[str | None] = []

        async def list_tools(self, *, params=None):
            cursor = None if params is None else params.cursor
            self.cursors.append(cursor)
            if cursor is None:
                return SimpleNamespace(
                    tools=[SimpleNamespace(name="onboard")], next_cursor="page-2"
                )
            return SimpleNamespace(
                tools=[SimpleNamespace(name="sync_state")], next_cursor=None
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
