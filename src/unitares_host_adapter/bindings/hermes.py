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
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from unitares_host_adapter.core import UnitaresAdapter
from unitares_host_adapter.types import LocalConfigError, MissingServerURLError

# Adapter most recently touched by a hook. Returned from register() for backward
# compatibility with early smoke tests; production state is kept per session in
# _adapters.
_adapter: Any = None
_adapters: dict[str, Any] = {}

_HOOK_TIMEOUT_SECONDS = 30.0
_HOOK_FAILURE_LIMIT = 3
_LINKS_MAX = 200
_TURN_PROVENANCE = {
    "harness_type": "hermes_plugin",
    "governance_mode": "automatic_turn_checkin",
    "tool_surface": "hermes_lifecycle_hook",
    "transport": "streamable_http",
    "verification_source": "hook_observation",
}
_LOGGER = logging.getLogger(__name__)


class SessionLinks:
    """Local map of Hermes session id -> the UNITARES agent UUID minted for it.

    Lets a compressed, resumed, or delegated Hermes session declare lineage to
    the identity its predecessor used. Only UUIDs are stored — never the
    ``client_session_id`` proof — so the file cannot be used to act as an
    identity. Bounded to the most recent ``_LINKS_MAX`` sessions, written
    atomically, and fail-open: a missing, corrupt, or unwritable file just
    means no lineage is declared.
    """

    def __init__(self, path: Optional[Path]) -> None:
        self._path = path
        self._data: Optional[dict[str, dict[str, Any]]] = None
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._data is None:
            data: dict[str, dict[str, Any]] = {}
            try:
                if self._path is not None and self._path.is_file():
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data = {
                            str(k): v
                            for k, v in raw.items()
                            if isinstance(v, dict) and isinstance(v.get("agent_uuid"), str)
                        }
            except Exception:
                data = {}
            self._data = data
        return self._data

    def get(self, session_id: str) -> Optional[str]:
        if not session_id:
            return None
        with self._lock:
            entry = self._load().get(session_id)
        return entry.get("agent_uuid") if entry else None

    def set(self, session_id: str, agent_uuid: Optional[str]) -> None:
        if not session_id or not agent_uuid:
            return
        with self._lock:
            data = self._load()
            data[session_id] = {"agent_uuid": agent_uuid, "t": int(time.time())}
            if len(data) > _LINKS_MAX:
                keep = sorted(data.items(), key=lambda kv: kv[1].get("t", 0))[-_LINKS_MAX:]
                data.clear()
                data.update(keep)
            if self._path is None:
                return
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".links-")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                os.replace(tmp, self._path)
            except Exception:
                _LOGGER.debug("UNITARES Hermes session links not persisted", exc_info=True)


def _default_links_path() -> Optional[Path]:
    """``<HERMES_HOME>/plugin-data/unitares/sessions.json``; None outside Hermes."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()) / "plugin-data" / "unitares" / "sessions.json"
    except Exception:
        return None


def _default_session_lookup(session_id: str) -> Optional[dict[str, Any]]:
    """Return ``{"parent_session_id", "parent_end_reason"}`` from Hermes's session DB.

    Read-only; returns None when Hermes's state module or row is unavailable.
    """
    try:
        from hermes_state import SessionDB
    except Exception:
        return None
    db = None
    try:
        db = SessionDB(read_only=True)
        row = db.get_session(session_id) or {}
        parent = row.get("parent_session_id") or ""
        if not parent:
            return None
        parent_row = db.get_session(parent) or {}
        return {
            "parent_session_id": parent,
            "parent_end_reason": parent_row.get("end_reason") or "",
        }
    except Exception:
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


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
        from unitares_host_adapter.transport import mcp_url_from_env
        import os

        return cls(
            mcp_url_from_env(),
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
    session_links: Optional[SessionLinks] = None,
    session_lookup: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
) -> Any:
    """Register UNITARES governance hooks with a Hermes plugin context.

    Defaults restore the missing automatic behavior without flooding the server:
    one lazy onboard at the first Hermes turn, then one check-in per completed
    assistant turn. Per-tool gate/ambient/outcome modes are available but opt-in.

    Continuity: when a Hermes session descends from another (context
    compression, resume, delegated subagent), the new identity declares
    lineage to the predecessor's UNITARES UUID instead of starting unrelated.
    ``post_tool_call`` only counts locally; the counts ride on the next turn
    check-in as numeric ``sensor_data.afferents`` (no extra network calls).
    """
    global _adapter, _adapters
    _adapters = {}
    failure_counts: dict[str, int] = {}
    open_circuits: set[str] = set()
    links = session_links if session_links is not None else SessionLinks(_default_links_path())
    lookup = session_lookup or _default_session_lookup
    turn_counts: dict[str, dict[str, float]] = {}
    turn_started: dict[str, float] = {}
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
                # Only local-config advice (missing URL, refused Host) is logged
                # verbatim; its message is built locally. Other errors may carry
                # server text, so they are logged by type alone.
                reason = type(exc).__name__
                if isinstance(exc, LocalConfigError):
                    reason = f"{reason}: {exc}"
                _LOGGER.warning(
                    "UNITARES Hermes %s failed (%s); consecutive_failures=%d; circuit_open=%s",
                    operation,
                    reason,
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
        parent_uuid, spawn_reason, parent_session = _lineage_for(session_id, platform, kwargs)
        lineage: dict[str, Any] = {}
        if parent_uuid and spawn_reason:
            lineage = {"parent_agent_id": parent_uuid, "spawn_reason": spawn_reason}
        ok, _ = _run_guarded(
            session_id,
            "onboard",
            lambda: adapter_for_session.on_session_start(session_id, purpose=purpose, **lineage),
        )
        if not ok:
            return None
        links.set(session_id, getattr(adapter_for_session, "agent_uuid", None))
        if spawn_reason == "compaction" and parent_session and parent_session in _adapters:
            # The compressed-away session never gets on_session_finalize, so
            # retire its in-process adapter here.
            _retire(parent_session)
        return adapter_for_session

    def _lineage_for(
        session_id: str, platform: str, kwargs: dict[str, Any]
    ) -> tuple[Optional[str], Optional[str], str]:
        """(parent UUID, spawn_reason, parent Hermes session) for a new identity."""
        try:
            info = lookup(session_id) or {}
        except Exception:
            info = {}
        parent_session = str(info.get("parent_session_id") or kwargs.get("parent_session_id") or "")
        if parent_session and parent_session != session_id:
            parent_uuid = links.get(parent_session)
            if not parent_uuid:
                return None, None, parent_session
            if platform == "subagent":
                return parent_uuid, "subagent", parent_session
            if info.get("parent_end_reason") == "compression":
                return parent_uuid, "compaction", parent_session
            return parent_uuid, "explicit", parent_session
        # Same Hermes session starting a new identity after it was finalized in
        # this or an earlier process (for example /resume): a successor.
        own_uuid = links.get(session_id)
        if own_uuid:
            return own_uuid, "explicit", session_id
        return None, None, ""

    def _retire(session_id: str) -> None:
        """Release presence and drop the adapter for a Hermes session; never raises."""
        adapter_for_session = _adapters.pop(session_id, None)
        turn_counts.pop(session_id, None)
        turn_started.pop(session_id, None)
        try:
            if adapter_for_session is not None:
                release = getattr(adapter_for_session, "release_presence", None)
                if release is not None:
                    _run(release())
                _run(adapter_for_session.on_session_end(session_id))
        except (Exception, asyncio.CancelledError) as exc:
            _LOGGER.warning(
                "UNITARES Hermes session finalization failed (%s); continuing fail-open",
                type(exc).__name__,
            )
        finally:
            failure_counts.pop(session_id, None)
            open_circuits.discard(session_id)

    def pre_llm_call(**kwargs: Any) -> None:
        _ensure_session(**kwargs)
        session_id = _session_key(kwargs)
        if session_id:
            # After onboarding, so a first turn's wall time excludes the mint.
            turn_started[session_id] = time.monotonic()
        return None

    def _turn_afferents(session_id: str) -> dict[str, float]:
        counts = turn_counts.pop(session_id, None) or {}
        started = turn_started.pop(session_id, None)
        afferents = {
            "turn_tool_calls": counts.get("calls", 0),
            "turn_tool_errors": counts.get("errors", 0),
            "turn_tool_ms": round(counts.get("ms", 0.0)),
        }
        if started is not None:
            afferents["turn_wall_ms"] = round((time.monotonic() - started) * 1000)
        return afferents

    def post_llm_call(**kwargs: Any) -> None:
        session_id = _session_key(kwargs)
        adapter_for_session = _ensure_session(**kwargs)
        if adapter_for_session is None:
            turn_counts.pop(session_id, None)
            turn_started.pop(session_id, None)
            return None
        if not enable_turn_checkin:
            return None
        afferents = _turn_afferents(session_id)
        _run_guarded(
            session_id,
            "turn_checkin",
            lambda: adapter_for_session.checkin(
                "Hermes assistant turn completed",
                response_mode="minimal",
                complexity=0.2,
                epistemic_class="substrate_interpretation",
                provenance_context=dict(_TURN_PROVENANCE),
                afferents=afferents,
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

    def _count_tool(**kwargs: Any) -> None:
        """Count one finished tool call for this turn; local only, no text kept."""
        session_id = _session_key(kwargs)
        if not session_id:
            return
        counts = turn_counts.setdefault(session_id, {"calls": 0, "errors": 0, "ms": 0.0})
        counts["calls"] += 1
        if not _tool_success(kwargs):
            counts["errors"] += 1
        duration = kwargs.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
            counts["ms"] += float(duration)

    def post_tool_call(**kwargs: Any) -> None:
        try:
            _count_tool(**kwargs)
        except Exception:
            pass
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
        # Hermes fires this before inserting the session row and omits parent
        # metadata. Minting here would permanently lose child lineage: later
        # hooks see an already-bound session and cannot amend its declaration.
        # pre_llm_call runs after row creation and supplies parent_session_id.
        # A session opened without a turn therefore creates no server identity.
        return None

    def _close_session(**kwargs: Any) -> None:
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return None
        _retire(session_id)
        return None

    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_llm_call", post_llm_call)
    if enable_gate:
        ctx.register_hook("pre_tool_call", pre_tool_call)
    if enable_turn_checkin or enable_outcomes:
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


def build_adapter(mcp_url: str, *, bearer: Optional[str] = None) -> UnitaresAdapter:
    """Construct an adapter for an explicit server URL, with no default.

    Like ``_build_default_adapter``, this has no fallback server: a caller that
    has no configured server gets an error instead of sending governance
    traffic to a server it did not choose.
    """
    url = (mcp_url or "").strip()
    if not url:
        raise ValueError("a UNITARES MCP URL is required")
    return UnitaresAdapter(
        _PerCallStreamableHTTPTransport(url, bearer=bearer or None),
        agent_label="Hermes Agent",
        model_type="hermes-agent",
    )
