"""Concrete MCPTransport — streamable-HTTP client for the UNITARES MCP server.

The v0.1 release shipped only the `MCPTransport` Protocol (`core.py`) plus an
injected-fake test path; bindings raised NotImplementedError when asked to build
a default transport. This module is the real client that lets the adapter talk
to a live governance MCP (e.g. ``https://gov.cirwel.org/mcp/``).

Lifecycle: ``streamable_http_client`` opens an anyio task group that must be
entered and exited on the SAME task. Open via ``connect()`` (or ``async with``)
and close via ``aclose()`` from that task. ``aclose()`` shields the unwind so a
caller-side cancellation cannot tear the task group down on a different task —
the "exit cancel scope in a different task" crash class the UNITARES SDK hit in
its sentinel loop.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

import anyio
import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

DEFAULT_MCP_URL = "https://gov.cirwel.org/mcp/"


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
        self._http_client: Optional[httpx.AsyncClient] = None
        self._cm_stack: list[Any] = []

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

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def connect(self) -> None:
        """Open the MCP session. Idempotent; a no-op if already connected."""
        if self._session is not None:
            return
        self._http_client = httpx.AsyncClient(headers=self._headers(), timeout=self.call_timeout)
        cm = streamable_http_client(self.mcp_url, http_client=self._http_client)
        read, write, _ = await cm.__aenter__()
        self._cm_stack.append(cm)
        session_cm = ClientSession(read, write)
        self._session = await session_cm.__aenter__()
        self._cm_stack.append(session_cm)
        # Bound the handshake: an anyio-stream hang inside initialize() is not
        # covered by httpx's timeout and would block until the caller's outer
        # timeout cancels the whole task.
        await asyncio.wait_for(self._session.initialize(), self.connect_timeout)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            await self.connect()
        assert self._session is not None
        with anyio.fail_after(self.call_timeout):
            result = await self._session.call_tool(name, arguments)
        return _parse_result(result)

    async def aclose(self) -> None:
        """Close the session and HTTP client. Shielded so a caller cancellation
        cannot unwind the anyio task group on a different task."""
        for cm in reversed(self._cm_stack):
            try:
                with anyio.CancelScope(shield=True):
                    await cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._cm_stack.clear()
        self._session = None
        if self._http_client is not None:
            try:
                with anyio.CancelScope(shield=True):
                    await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None


def _parse_result(result: Any) -> dict[str, Any]:
    """Merge MCP tool-result content blocks into a dict.

    HTTP-level success does not imply tool-level success: the streamable_http
    transport can wrap a structured failure in an otherwise-200 response, so
    ``isError`` is checked first. Mirrors the UNITARES SDK's parser."""
    if getattr(result, "isError", False):
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
