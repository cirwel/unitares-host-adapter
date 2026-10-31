"""Model attribution through real adapters, with a simulated MCP transport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from unitares_host_adapter.bindings import hermes
from unitares_host_adapter.core import UnitaresAdapter


class HookContext:
    """Capture synchronous Hermes hooks without loading the harness."""

    def __init__(self) -> None:
        """Initialize an empty hook registry."""
        self.hooks: dict[str, Callable[..., Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}

    def register_hook(self, name: str, callback: Callable[..., Any]) -> None:
        """Register a hook for direct invocation by the tests."""
        self.hooks[name] = callback


class AttributionTransport:
    """Record wire arguments and return distinct, test-only identity/proof pairs."""

    def __init__(self) -> None:
        """Initialize calls and configurable onboarding behavior."""
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.minted = 0
        self.fail_next_onboard = False
        self.include_proof = True

    async def list_tools(self) -> set[str]:
        """Advertise the lifecycle tools exercised by these tests."""
        return {"onboard", "sync_state", "agent"}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Simulate lifecycle responses; never contact a real server."""
        self.calls.append((name, arguments))
        if name == "onboard":
            if self.fail_next_onboard:
                self.fail_next_onboard = False
                raise RuntimeError("simulated onboarding failure")
            self.minted += 1
            response = {"agent_uuid": f"test-uuid-{self.minted}"}
            if self.include_proof:
                response["client_session_id"] = f"test-proof-{self.minted}"
            return response
        if name == "agent":
            return {"released": True}
        return {"action": "proceed"}


@pytest.fixture(params=["default", "explicit"])
def attribution_binding(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[HookContext, AttributionTransport, hermes.SessionLinks]:
    """Exercise both shipped adapter builders with isolated in-memory transport."""
    transport = AttributionTransport()
    if request.param == "default":
        monkeypatch.setattr(
            hermes._PerCallStreamableHTTPTransport,
            "from_env",
            classmethod(lambda cls: transport),
        )
        factory = hermes._build_default_adapter
    else:
        monkeypatch.setattr(
            hermes, "_PerCallStreamableHTTPTransport", lambda *args, **kwargs: transport
        )

        def factory() -> UnitaresAdapter:
            """Build the configured-server adapter without network I/O."""
            return hermes.build_adapter("https://example.invalid/mcp/")

    ctx = HookContext()
    links = hermes.SessionLinks(tmp_path / "sessions.json")
    hermes.register(
        ctx, adapter_factory=factory, session_links=links, session_lookup=ctx.sessions.get
    )
    return ctx, transport, links


@pytest.mark.parametrize("hook", ["pre_llm_call", "post_llm_call"])
def test_hook_model_reaches_onboard_separately_from_harness(
    attribution_binding: tuple[HookContext, AttributionTransport, hermes.SessionLinks],
    hook: str,
) -> None:
    """The first identity write must use the exact hook model, not the harness."""
    ctx, transport, _ = attribution_binding
    model = "test-provider/opaque:model-v1"
    assert ctx.hooks[hook](session_id="model-session", model=model, platform="cli") is None

    assert transport.calls[0] == (
        "onboard",
        {
            "name": "Hermes Agent",
            "model_type": model,
            "client_hint": f"hermes:cli:{model}",
            "force_new": True,
        },
    )
    ctx.hooks["post_llm_call"](session_id="model-session", model=model, platform="cli")
    checkin = transport.calls[-1][1]
    assert checkin["client_session_id"] == "test-proof-1"
    assert checkin["provenance_context"]["harness_type"] == "hermes_plugin"
    assert checkin["provenance_context"]["verification_source"] == "hook_observation"
    assert "provider" not in checkin["provenance_context"]
    assert transport.minted == 1


@pytest.mark.parametrize(
    "model_kwargs",
    [{}, {"model": None}, {"model": ""}, {"model": " \t"}, {"model": 42}, {"model": {"id": "m"}}],
    ids=["absent", "none", "empty", "whitespace", "number", "mapping"],
)
def test_missing_or_invalid_hook_model_is_unknown_not_harness(
    attribution_binding: tuple[HookContext, AttributionTransport, hermes.SessionLinks],
    model_kwargs: dict[str, Any],
) -> None:
    """Missing model evidence must not invent a model or stringify metadata."""
    ctx, transport, _ = attribution_binding
    ctx.hooks["pre_llm_call"](session_id="unknown-session", platform="cli", **model_kwargs)
    assert transport.calls[0][1]["model_type"] == "unknown-model"
    assert transport.calls[0][1]["client_hint"] == "hermes:cli:unknown-model"


@pytest.mark.parametrize(
    ("platform", "parent_end_reason", "spawn_reason"),
    [("subagent", "", "subagent"), ("cli", "compression", "compaction")],
)
def test_startup_defers_model_and_lineage_until_first_turn(
    attribution_binding: tuple[HookContext, AttributionTransport, hermes.SessionLinks],
    platform: str,
    parent_end_reason: str,
    spawn_reason: str,
) -> None:
    """Startup cannot preempt the actual model or the later parent DB record."""
    ctx, transport, links = attribution_binding
    ctx.hooks["pre_llm_call"](session_id="parent", model="parent-model", platform="cli")
    ctx.hooks["on_session_start"](session_id="child", model="startup-model", platform=platform)
    assert transport.minted == 1
    ctx.sessions["child"] = {
        "parent_session_id": "parent", "parent_end_reason": parent_end_reason
    }
    ctx.hooks["pre_llm_call"](
        session_id="child", model="child-model", platform=platform, parent_session_id="parent"
    )
    onboard = [args for name, args in transport.calls if name == "onboard"]
    assert onboard[0]["model_type"] == "parent-model"
    assert onboard[1]["model_type"] == "child-model"
    assert onboard[1]["parent_agent_id"] == "test-uuid-1"
    assert onboard[1]["spawn_reason"] == spawn_reason
    assert links.get("child") == "test-uuid-2"
    ctx.hooks["post_llm_call"](session_id="child", model="child-model", platform=platform)
    assert transport.calls[-1][1]["client_session_id"] == "test-proof-2"


def test_failed_onboard_retries_with_current_model_without_persisting_proof(
    attribution_binding: tuple[HookContext, AttributionTransport, hermes.SessionLinks],
    tmp_path: Path,
) -> None:
    """Attribution is set before each attempted mint, while proof stays private."""
    ctx, transport, links = attribution_binding
    transport.fail_next_onboard = True
    assert ctx.hooks["pre_llm_call"](session_id="retry", model="first-model") is None
    assert links.get("retry") is None
    assert ctx.hooks["pre_llm_call"](session_id="retry", model="retry-model") is None
    onboard = [args for name, args in transport.calls if name == "onboard"]
    assert [args["model_type"] for args in onboard] == ["first-model", "retry-model"]
    stored = json.loads((tmp_path / "sessions.json").read_text())
    assert set(stored["retry"]) == {"agent_uuid", "t"}
    assert "test-proof" not in json.dumps(stored)


def test_cached_uuid_does_not_become_session_proof(
    attribution_binding: tuple[HookContext, AttributionTransport, hermes.SessionLinks],
) -> None:
    """A cached UUID declares lineage only, including when onboarding omits proof."""
    ctx, transport, links = attribution_binding
    links.set("resumed", "cached-predecessor-uuid")
    transport.include_proof = False
    ctx.hooks["post_llm_call"](session_id="resumed", model="actual-model", platform="cli")
    onboard = transport.calls[0][1]
    assert onboard["model_type"] == "actual-model"
    assert onboard["parent_agent_id"] == "cached-predecessor-uuid"
    assert onboard["spawn_reason"] == "explicit"
    assert "client_session_id" not in onboard
    assert "client_session_id" not in transport.calls[-1][1]
    assert links.get("resumed") == "test-uuid-1"
