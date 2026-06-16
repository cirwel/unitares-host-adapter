"""Hermes Agent binding.

Wires the UnitaresAdapter onto Hermes's plugin hook surface. Hermes exposes
register_hook(name, callback) with the following hook names relevant here:

    pre_tool_call          -> gated mode (returns {"action": "block", ...} or None)
    post_tool_call         -> outcome_event (calibration feed)
    transform_tool_result  -> ambient annotation
    on_session_start       -> onboard
    on_session_end         -> session close

Mode coverage: full (explicit via MCP tool call + ambient + gated).

Usage in a Hermes plugin:

    # ~/.hermes/plugins/unitares/__init__.py
    from unitares_host_adapter.bindings.hermes import register

    def setup(ctx):
        register(ctx)  # reads UNITARES_MCP_URL and UNITARES_BEARER from env
"""

from __future__ import annotations

from typing import Any, Optional

from unitares_host_adapter.core import UnitaresAdapter

# Module-level singleton. Hermes instantiates one plugin per session; we keep
# one adapter instance and let it manage the session_id.
_adapter: Optional[UnitaresAdapter] = None


def register(
    ctx: Any,
    *,
    adapter: Optional[UnitaresAdapter] = None,
    enable_gate: bool = True,
    enable_ambient: bool = True,
) -> UnitaresAdapter:
    """Register UNITARES governance hooks with a Hermes plugin context.

    Pass an explicit adapter to wire to a custom transport; otherwise the
    binding looks for UNITARES_MCP_URL / UNITARES_BEARER in the environment
    and constructs a default transport. (Transport construction is deferred
    to the 0.2 milestone — this 0.1 release exposes the wiring shape.)
    """
    global _adapter
    _adapter = adapter or _build_default_adapter()

    async def pre_tool_call(**kwargs: Any) -> Optional[dict[str, str]]:
        if not enable_gate:
            return None
        tool_name = kwargs.get("tool_name", "")
        args = kwargs.get("args", {}) or {}
        directive = await _adapter.gate(tool_name, args)
        return directive.as_dict() if directive else None

    async def post_tool_call(**kwargs: Any) -> None:
        tool_name = kwargs.get("tool_name", "")
        success = kwargs.get("success", True)
        await _adapter.outcome_event(tool_name, success=success)

    async def transform_tool_result(**kwargs: Any) -> Any:
        if not enable_ambient:
            return kwargs.get("result")
        tool_name = kwargs.get("tool_name", "")
        args = kwargs.get("args", {}) or {}
        result = kwargs.get("result")
        annotated = await _adapter.annotate(tool_name, args, result)
        return annotated.render() if annotated.annotation else result

    async def on_session_start(**kwargs: Any) -> None:
        session_id = kwargs.get("session_id", "")
        await _adapter.on_session_start(session_id, purpose="hermes")

    async def on_session_end(**kwargs: Any) -> None:
        session_id = kwargs.get("session_id", "")
        await _adapter.on_session_end(session_id)
        # Close the transport if it owns a connection (the default streamable
        # transport does); injected/fake transports may not.
        transport = getattr(_adapter, "_transport", None)
        aclose = getattr(transport, "aclose", None)
        if aclose is not None:
            await aclose()

    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("transform_tool_result", transform_tool_result)
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)

    return _adapter


def _build_default_adapter() -> UnitaresAdapter:
    """Construct an adapter wired to the default streamable-HTTP transport.

    Reads UNITARES_MCP_URL / UNITARES_BEARER from the environment. The
    transport connects lazily on the first governance call (on_session_start),
    so building the adapter performs no I/O.
    """
    from unitares_host_adapter.transport import StreamableHTTPTransport

    return UnitaresAdapter(StreamableHTTPTransport.from_env())
