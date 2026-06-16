"""UnitaresAdapter — the core client that maps UNITARES MCP tools onto three delivery modes."""

from __future__ import annotations

from typing import Any, Optional, Protocol

from unitares_host_adapter.types import (
    AnnotatedResult,
    BlockDirective,
    ResponseMode,
    Verdict,
)


class MCPTransport(Protocol):
    """Minimal transport surface the adapter needs. The real implementation wraps mcp.ClientSession."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class UnitaresAdapter:
    """Adapter that exposes UNITARES governance as three delivery modes.

    Typical lifetime: one instance per host session. Construct with an MCPTransport
    already connected to the UNITARES governance MCP (e.g. gov.cirwel.org/mcp/).

    For host-specific wiring, import from unitares_host_adapter.bindings.<host>.
    """

    def __init__(self, transport: MCPTransport, *, agent_label: Optional[str] = None) -> None:
        self._transport = transport
        self._agent_label = agent_label
        self._session_id: Optional[str] = None
        # Governance-issued continuity proof, captured from onboard. Echoed on
        # every later call so the host's calls form ONE trajectory. Distinct
        # from `_session_id` (the host's own session id): under the strict
        # identity gate a call without this resolves by transport fingerprint
        # and lands on a sibling identity (or is refused), so onboarding alone
        # is not enough — the proof must be threaded through.
        self._client_session_id: Optional[str] = None

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def client_session_id(self) -> Optional[str]:
        return self._client_session_id

    async def on_session_start(self, session_id: str, *, purpose: str = "", **_: Any) -> None:
        """Mint governance identity for a new host session. Host-agnostic lifecycle entry point."""
        self._session_id = session_id
        raw = await self._transport.call_tool(
            "onboard",
            {
                "purpose": purpose or f"host-session:{session_id}",
                "force_new": True,
            },
        )
        self._client_session_id = _find(raw, "client_session_id")

    async def on_session_end(self, session_id: str, **_: Any) -> None:
        """Optional final check-in on session close. No-op if not supported by the host."""
        self._session_id = None
        self._client_session_id = None

    def _bind(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Echo the captured client_session_id into a call's arguments, if known."""
        if self._client_session_id and "client_session_id" not in arguments:
            arguments["client_session_id"] = self._client_session_id
        return arguments

    async def checkin(
        self,
        purpose: str,
        *,
        response_mode: ResponseMode = "auto",
        confidence: Optional[float] = None,
    ) -> Verdict:
        """Mode 1: Explicit delivery. Agent initiates a check-in."""
        arguments: dict[str, Any] = {
            "purpose": purpose,
            "response_mode": response_mode,
        }
        if confidence is not None:
            arguments["confidence"] = confidence
        raw = await self._transport.call_tool("checkin", self._bind(arguments))
        return self._verdict_from_raw(raw)

    async def gate(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> Optional[BlockDirective]:
        """Mode 3: Gated delivery. Return a BlockDirective to block the tool call, or None to allow.

        Intended for host pre_tool_call hooks. The caller converts the returned
        BlockDirective to the host's native block shape (e.g. as_dict() for Hermes).
        """
        raw = await self._transport.call_tool(
            "checkin",
            self._bind({
                "purpose": f"gate:{tool_name}",
                "response_mode": "minimal",
                "tool_context": {"tool_name": tool_name, "args": args},
            }),
        )
        verdict = self._verdict_from_raw(raw)
        if verdict.blocks:
            return BlockDirective(
                message=verdict.message or f"UNITARES governance {verdict.action}ed this call."
            )
        return None

    async def annotate(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: Any,
        *,
        response_mode: ResponseMode = "lite",
    ) -> AnnotatedResult:
        """Mode 2: Ambient delivery. Annotate a tool result with proprioceptive state."""
        raw = await self._transport.call_tool(
            "checkin",
            self._bind({
                "purpose": f"ambient:{tool_name}",
                "response_mode": response_mode,
                "tool_context": {"tool_name": tool_name, "args_preview": _preview(args)},
            }),
        )
        verdict = self._verdict_from_raw(raw)
        annotation = _format_ambient(verdict)
        return AnnotatedResult(result=result, annotation=annotation)

    async def outcome_event(
        self,
        tool_name: str,
        *,
        success: bool,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        """Feed calibration ground truth after a tool call completes."""
        await self._transport.call_tool(
            "outcome_event",
            self._bind({
                "tool_name": tool_name,
                "success": success,
                "details": details or {},
            }),
        )

    @staticmethod
    def _verdict_from_raw(raw: dict[str, Any]) -> Verdict:
        action = raw.get("action") or raw.get("verdict") or "proceed"
        return Verdict(
            action=action,  # type: ignore[arg-type]
            message=raw.get("message") or raw.get("guidance") or "",
            margin=raw.get("margin"),
            raw=raw,
        )


def _find(obj: Any, key: str) -> Optional[str]:
    """First non-empty value for `key` anywhere in a nested dict/list, else None.

    The onboard response shape varies (top-level on canonical names, nested
    under raw_governance / agent_signature on friendly aliases), so search
    rather than assume a fixed path."""
    if isinstance(obj, dict):
        if obj.get(key):
            return obj[key]
        for value in obj.values():
            found = _find(value, key)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find(value, key)
            if found:
                return found
    return None


def _preview(args: dict[str, Any], *, max_chars: int = 200) -> str:
    s = str(args)
    return s if len(s) <= max_chars else s[: max_chars - 1] + "…"


def _format_ambient(verdict: Verdict) -> str:
    if verdict.action == "proceed" and not verdict.margin:
        return ""
    parts = [verdict.action]
    if verdict.margin:
        parts.append(f"margin:{verdict.margin}")
    if verdict.message:
        parts.append(verdict.message)
    return " · ".join(parts)
