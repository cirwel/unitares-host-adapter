"""Concrete MCPTransport — streamable-HTTP client for the UNITARES MCP server.

The v0.1 release shipped only the `MCPTransport` Protocol (`core.py`) plus an
injected-fake test path; bindings raised NotImplementedError when asked to build
a default transport. This module is the real client that lets the adapter talk
to a live governance MCP (e.g. ``https://gov.cirwel.org/mcp/``).

Lifecycle: ``streamable_http_client`` opens an anyio task group that must be
entered and exited on the SAME task. Open via ``connect()`` (or ``async with``)
and close via ``aclose()`` from that task. A lifecycle cancel scope is entered
before MCP contexts and switched to shielded only during unwind, preserving
strict LIFO order while preventing cancellation from stranding resources.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

import anyio
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import PaginatedRequestParams

DEFAULT_MCP_URL = "https://gov.cirwel.org/mcp/"
_LOGGER = logging.getLogger(__name__)


class TransportError(RuntimeError):
    """Raised when the MCP layer marks a tool call as an error."""


class StreamableHTTPTransport:
    """One transport per adapter/session. Connects lazily; reused across calls.

    Satisfies the ``MCPTransport`` protocol expected by ``UnitaresAdapter``:
    ``async def call_tool(name, arguments) -> dict``.
    """

    def __init__(
        self,
        mcp_url: str,
        *,
        bearer: Optional[str] = None,
        connect_timeout: float = 10.0,
        call_timeout: float = 30.0,
    ) -> None:
        self.mcp_url = mcp_url
        self._bearer = bearer
        self.connect_timeout = connect_timeout
        self.call_timeout = call_timeout
        self._session: Optional[ClientSession] = None
        self._http_client: Optional[Any] = None
        self._cm_stack: list[Any] = []
        self._lifecycle_scope: anyio.CancelScope | None = None

    @classmethod
    def from_env(cls, **kwargs: Any) -> "StreamableHTTPTransport":
        """Build from UNITARES_MCP_URL / UNITARES_BEARER (the binding default)."""
        return cls(
            os.environ.get("UNITARES_MCP_URL", DEFAULT_MCP_URL),
            bearer=os.environ.get("UNITARES_BEARER"),
            **kwargs,
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer}"} if self._bearer else {}

    async def __aenter__(self) -> "StreamableHTTPTransport":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        try:
            await self.aclose()
        except BaseException as close_error:
            if isinstance(
                close_error,
                (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
            ):
                raise
            _LOGGER.warning(
                "UNITARES MCP cleanup failed after MCP operation (%s); "
                "preserving the operation result",
                type(close_error).__name__,
            )
        return False

    async def connect(self) -> None:
        """Open the MCP session. Idempotent; a no-op if already connected."""
        if self._session is not None:
            return
        lifecycle_scope = anyio.CancelScope()
        lifecycle_scope.__enter__()
        self._lifecycle_scope = lifecycle_scope
        try:
            self._http_client = create_mcp_http_client(headers=self._headers())
            cm = streamable_http_client(self.mcp_url, http_client=self._http_client)
            streams = await cm.__aenter__()
            self._cm_stack.append(cm)
            read, write, *_ = streams
            session_cm = ClientSession(read, write)
            self._session = await session_cm.__aenter__()
            self._cm_stack.append(session_cm)
            # Bound the handshake: an anyio-stream hang inside initialize() is not
            # covered by httpx's timeout and would block until the caller's outer
            # timeout cancels the whole task.
            await asyncio.wait_for(self._session.initialize(), self.connect_timeout)
        except BaseException:
            try:
                await self.aclose()
            except BaseException:
                pass
            raise

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            await self.connect()
        assert self._session is not None
        with anyio.fail_after(self.call_timeout):
            result = await self._session.call_tool(name, arguments)
        return _parse_result(result)

    async def list_tools(self) -> set[str]:
        """Return the public tool names advertised by MCP tools/list."""
        if self._session is None:
            await self.connect()
        assert self._session is not None
        names: set[str] = set()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        with anyio.fail_after(self.call_timeout):
            while True:
                params = (
                    PaginatedRequestParams(cursor=cursor)
                    if cursor is not None
                    else None
                )
                result = await self._session.list_tools(params=params)
                names.update(str(tool.name) for tool in result.tools)
                next_cursor = getattr(result, "next_cursor", None) or getattr(
                    result, "nextCursor", None
                )
                if not next_cursor:
                    break
                cursor = str(next_cursor)
                if cursor in seen_cursors:
                    raise TransportError("MCP tools/list returned a repeated cursor")
                seen_cursors.add(cursor)
        return names

    async def aclose(self) -> None:
        """Close contexts in strict LIFO order in the task that opened them.

        AnyIO task groups own cancel scopes that must be exited while they are
        the current scope. Wrapping ``__aexit__`` in a new shield scope breaks
        that invariant and can leave the MCP stream generator unclosed.
        """
        lifecycle_scope = self._lifecycle_scope
        if lifecycle_scope is not None:
            lifecycle_scope.shield = True
        first_error: BaseException | None = None
        for cm in reversed(self._cm_stack):
            try:
                await cm.__aexit__(None, None, None)
            except BaseException as exc:
                first_error = first_error or exc
        self._cm_stack.clear()
        self._session = None
        http_client = self._http_client
        self._http_client = None
        if http_client is not None:
            try:
                await http_client.aclose()
            except BaseException as exc:
                first_error = first_error or exc
        self._lifecycle_scope = None
        if lifecycle_scope is not None:
            try:
                lifecycle_scope.__exit__(None, None, None)
            except BaseException as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error


def _parse_result(result: Any) -> dict[str, Any]:
    """Merge MCP tool-result content blocks into a dict.

    HTTP-level success does not imply tool-level success: the streamable_http
    transport can wrap a structured failure in an otherwise-200 response, so
    both SDK field spellings (``is_error`` / ``isError``) are checked first.
    Mirrors the UNITARES SDK's parser."""
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        error_text = ""
        for content in getattr(result, "content", []) or []:
            if hasattr(content, "text"):
                error_text = content.text
                break
        raise TransportError(error_text or "MCP tool returned isError=true")

    final: dict[str, Any] = {}
    raw_texts: list[str] = []
    json_parsed = False
    for content in getattr(result, "content", []) or []:
        if hasattr(content, "text"):
            text = content.text
            raw_texts.append(text)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                final.update(data)
                json_parsed = True

    if json_parsed:
        return final
    if raw_texts:
        return {"text": "\n".join(raw_texts), "raw": True}
    return {"success": False, "error": "No content in response"}
