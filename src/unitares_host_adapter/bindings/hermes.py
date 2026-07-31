"""Hermes Agent binding.

Hermes plugin callbacks are synchronous: ``hermes_cli.plugins.invoke_hook`` calls
callbacks directly and does not await coroutine returns. This binding therefore
registers synchronous hook functions that drive the async ``UnitaresAdapter``
behind the scenes.

Default mode is deliberately light:

* ``pre_llm_call`` lazily onboards the current Hermes session.
* ``post_llm_call`` emits one turn-level check-in.
* per-tool gated, ambient, and outcome hooks are opt-in because they can create
  high-frequency governance traffic.

Usage in a Hermes plugin::

    # ~/.hermes/plugins/unitares/plugin.yaml
    # name: unitares
    # provides_hooks: [pre_llm_call, post_llm_call]

    # ~/.hermes/plugins/unitares/__init__.py
    from unitares_host_adapter.bindings.hermes import register as register_unitares

    def register(ctx):
        register_unitares(ctx)  # reads UNITARES_MCP_URL / UNITARES_BEARER from env
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Callable, Optional

from unitares_host_adapter.core import UnitaresAdapter

# Adapter most recently touched by a hook. Returned from register() for backward
# compatibility with early smoke tests; production state is kept per session in
# _adapters.
_adapter: Any = None
_adapters: dict[str, Any] = {}

_HOOK_TIMEOUT_SECONDS = 30.0
_HOOK_FAILURE_LIMIT = 3
_TURN_PROVENANCE = {
    "harness_type": "hermes_plugin",
    "governance_mode": "automatic_turn_checkin",
    "tool_surface": "hermes_lifecycle_hook",
    "transport": "streamable_http",
    "verification_source": "hook_observation",
}
_LOGGER = logging.getLogger(__name__)


class _PerCallStreamableHTTPTransport:
    """Hermes-safe transport wrapper.

    The base StreamableHTTPTransport keeps an MCP session open across calls and
    must open/close its anyio task group from the same task. Hermes hooks are
    sync callbacks, so a naive ``asyncio.run`` per hook would reuse that session
    across different event loops/tasks. This wrapper opens a fresh MCP session
    for each governance call and closes it inside the same coroutine task.

    The UNITARES ``client_session_id`` still persists in ``UnitaresAdapter`` and
    is echoed into later calls, so governance attribution remains continuous
    even though the HTTP MCP transport session is per-call.
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

    @classmethod
    def from_env(cls) -> "_PerCallStreamableHTTPTransport":
        from unitares_host_adapter.transport import DEFAULT_MCP_URL
        import os

        return cls(
            os.environ.get("UNITARES_MCP_URL", DEFAULT_MCP_URL),
            bearer=os.environ.get("UNITARES_BEARER"),
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from unitares_host_adapter.transport import StreamableHTTPTransport

        async with StreamableHTTPTransport(
            self.mcp_url,
            bearer=self._bearer,
            connect_timeout=self.connect_timeout,
            call_timeout=self.call_timeout,
        ) as transport:
            return await transport.call_tool(name, arguments)

    async def list_tools(self) -> set[str]:
        """Discover capabilities in the same bounded per-call transport lifecycle."""
        from unitares_host_adapter.transport import StreamableHTTPTransport

        async with StreamableHTTPTransport(
            self.mcp_url,
            bearer=self._bearer,
            connect_timeout=self.connect_timeout,
            call_timeout=self.call_timeout,
        ) as transport:
            return await transport.list_tools()


def _run(awaitable: Any) -> Any:
    """Resolve an awaitable from Hermes's synchronous hook surface.

    If no loop is running, use ``asyncio.run``. If a future Hermes call site
    invokes hooks from inside an active loop, run the coroutine in a helper
    thread with its own loop so the hook still returns a concrete value.
    """
    if not inspect_awaitable(awaitable):
        return awaitable

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    outcome: dict[str, Any] = {}
    failure: dict[str, BaseException] = {}
    done = threading.Event()

    def _runner() -> None:
        try:
            outcome["value"] = asyncio.run(awaitable)
        except BaseException as exc:  # pragma: no cover - re-raised below
            failure["exc"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_runner, name="unitares-hermes-hook-await", daemon=True)
    thread.start()
    if not done.wait(timeout=_HOOK_TIMEOUT_SECONDS):
        raise TimeoutError(
            "UNITARES Hermes hook did not complete within "
            f"{_HOOK_TIMEOUT_SECONDS:.0f}s"
        )
    if "exc" in failure:
        raise failure["exc"]
    return outcome.get("value")


def inspect_awaitable(value: Any) -> bool:
    """Tiny indirection for tests/type clarity without importing inspect hot."""
    import inspect

    return inspect.isawaitable(value)


def _session_key(kwargs: dict[str, Any]) -> str:
    """Stable host key for a Hermes hook callback.

    Turn hooks provide session_id; tool hooks may only provide task_id in some
    Hermes versions/call sites, so fall back to task_id rather than dropping
    opt-in governance coverage entirely.
    """
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "")


def _tool_success(kwargs: dict[str, Any]) -> bool:
    """Infer whether a Hermes post_tool_call result succeeded."""
    status = kwargs.get("status")
    if status is not None:
        return str(status) == "ok"

    result = kwargs.get("result")
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            if parsed.get("error"):
                return False
            if parsed.get("success") is False:
                return False
    return True


def register(
    ctx: Any,
    *,
    adapter: Optional[Any] = None,
    adapter_factory: Optional[Callable[[], Any]] = None,
    enable_gate: bool = False,
    enable_ambient: bool = False,
    enable_outcomes: bool = False,
    enable_turn_checkin: bool = True,
) -> Any:
    """Register UNITARES governance hooks with a Hermes plugin context.

    Defaults restore the missing automatic behavior without flooding the server:
    one lazy onboard at the first Hermes turn, then one check-in per completed
    assistant turn. Per-tool gate/ambient/outcome modes are available but opt-in.
    """
    global _adapter, _adapters
    _adapters = {}
    failure_counts: dict[str, int] = {}
    open_circuits: set[str] = set()
    if adapter is not None:
        def factory() -> Any:
            return adapter

        _adapter = adapter
    else:
        factory = adapter_factory or _build_default_adapter
        _adapter = None

    def _run_guarded(
        session_id: str,
        operation: str,
        awaitable_factory: Callable[[], Any],
    ) -> tuple[bool, Any]:
        """Run fail-open and stop retrying after bounded consecutive failures."""
        if session_id in open_circuits:
            return False, None
        try:
            value = _run(awaitable_factory())
        except (Exception, asyncio.CancelledError) as exc:
            count = failure_counts.get(session_id, 0) + 1
            failure_counts[session_id] = count
            if count >= _HOOK_FAILURE_LIMIT:
                open_circuits.add(session_id)
            if count == 1 or count == _HOOK_FAILURE_LIMIT:
                _LOGGER.warning(
                    "UNITARES Hermes %s failed (%s); consecutive_failures=%d; circuit_open=%s",
                    operation,
                    type(exc).__name__,
                    count,
                    count >= _HOOK_FAILURE_LIMIT,
                )
            return False, None
        failure_counts.pop(session_id, None)
        return True, value

    def _adapter_for(session_id: str) -> Any:
        global _adapter
        if session_id not in _adapters:
            _adapters[session_id] = factory()
        _adapter = _adapters[session_id]
        return _adapter

    def _ensure_session(**kwargs: Any) -> Any:
        session_id = _session_key(kwargs)
        if not session_id or session_id in open_circuits:
            return None
        if session_id not in _adapters:
            ok, adapter_for_session = _run_guarded(
                session_id,
                "adapter_factory",
                lambda: _adapter_for(session_id),
            )
            if not ok:
                return None
        else:
            adapter_for_session = _adapter_for(session_id)
        current = getattr(adapter_for_session, "session_id", None)
        if current == session_id:
            return adapter_for_session
        platform = str(kwargs.get("platform") or "hermes")
        model = str(kwargs.get("model") or "unknown-model")
        purpose = f"hermes:{platform}:{model}"
        ok, _ = _run_guarded(
            session_id,
            "onboard",
            lambda: adapter_for_session.on_session_start(session_id, purpose=purpose),
        )
        return adapter_for_session if ok else None

    def pre_llm_call(**kwargs: Any) -> None:
        _ensure_session(**kwargs)
        return None

    def post_llm_call(**kwargs: Any) -> None:
        session_id = _session_key(kwargs)
        adapter_for_session = _ensure_session(**kwargs)
        if adapter_for_session is None:
            return None
        if not enable_turn_checkin:
            return None
        _run_guarded(
            session_id,
            "turn_checkin",
            lambda: adapter_for_session.checkin(
                "Hermes assistant turn completed",
                response_mode="minimal",
                complexity=0.2,
                epistemic_class="substrate_interpretation",
                provenance_context=dict(_TURN_PROVENANCE),
            ),
        )
        return None

    def pre_tool_call(**kwargs: Any) -> Optional[dict[str, str]]:
        if not enable_gate:
            return None
        adapter_for_session = _ensure_session(**kwargs)
        if adapter_for_session is None:
            return None
        tool_name = kwargs.get("tool_name", "")
        args = kwargs.get("args", {}) or {}
        ok, directive = _run_guarded(
            _session_key(kwargs),
            "tool_gate",
            lambda: adapter_for_session.gate(tool_name, args),
        )
        if not ok:
            return None
        return directive.as_dict() if directive else None

    def post_tool_call(**kwargs: Any) -> None:
        if not enable_outcomes:
            return None
        adapter_for_session = _ensure_session(**kwargs)
        if adapter_for_session is None:
            return None
        tool_name = kwargs.get("tool_name", "")
        success = _tool_success(kwargs)
        details = {
            "status": kwargs.get("status") or ("ok" if success else "error"),
            "error_type": kwargs.get("error_type") or "",
            "governance_mode": "automatic_tool_outcome",
            "harness": "hermes_plugin",
            "verification_source": "hook_observation",
        }
        _run_guarded(
            _session_key(kwargs),
            "tool_outcome",
            lambda: adapter_for_session.outcome_event(
                tool_name, success=success, details=details
            ),
        )
        return None

    def transform_tool_result(**kwargs: Any) -> Any:
        if not enable_ambient:
            return kwargs.get("result")
        adapter_for_session = _ensure_session(**kwargs)
        if adapter_for_session is None:
            return kwargs.get("result")
        tool_name = kwargs.get("tool_name", "")
        args = kwargs.get("args", {}) or {}
        result = kwargs.get("result")
        ok, annotated = _run_guarded(
            _session_key(kwargs),
            "tool_annotation",
            lambda: adapter_for_session.annotate(tool_name, args, result),
        )
        if not ok:
            return result
        return annotated.render() if annotated.annotation else result

    def on_session_start(**kwargs: Any) -> None:
        _ensure_session(**kwargs)
        return None

    def _close_session(**kwargs: Any) -> None:
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return None
        adapter_for_session = _adapters.pop(session_id, None)
        try:
            if adapter_for_session is not None:
                _run(adapter_for_session.on_session_end(session_id))
        except (Exception, asyncio.CancelledError) as exc:
            _LOGGER.warning(
                "UNITARES Hermes session finalization failed (%s); continuing fail-open",
                type(exc).__name__,
            )
        finally:
            failure_counts.pop(session_id, None)
            open_circuits.discard(session_id)
        return None

    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_llm_call", post_llm_call)
    if enable_gate:
        ctx.register_hook("pre_tool_call", pre_tool_call)
    if enable_outcomes:
        ctx.register_hook("post_tool_call", post_tool_call)
    if enable_ambient:
        ctx.register_hook("transform_tool_result", transform_tool_result)
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_finalize", _close_session)
    ctx.register_hook("on_session_reset", _close_session)

    return _adapter


def _build_default_adapter() -> UnitaresAdapter:
    """Construct an adapter wired to the Hermes-safe per-call transport."""
    return UnitaresAdapter(
        _PerCallStreamableHTTPTransport.from_env(),
        agent_label="Hermes Agent",
        model_type="hermes-agent",
    )
