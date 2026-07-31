"""Tests for the Hermes host binding.

Hermes plugin hooks are synchronous: hermes_cli.plugins.invoke_hook calls each
callback directly and does not await coroutine returns. The binding therefore
must expose synchronous callbacks that drive the async adapter internally.
"""

from __future__ import annotations

import inspect
from typing import Any

from unitares_host_adapter.bindings.hermes import register


class FakeCtx:
    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks[name] = callback


class FakeDirective:
    def as_dict(self) -> dict[str, str]:
        return {"action": "block", "message": "blocked by test"}


class FakeAnnotated:
    annotation = "guide · test"

    def render(self) -> str:
        return "annotated result"


class FakeAdapter:
    def __init__(self) -> None:
        self.events: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.session_id: str | None = None

    async def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        self.session_id = session_id
        self.events.append(("on_session_start", (session_id,), kwargs))

    async def on_session_end(self, session_id: str, **kwargs: Any) -> None:
        self.events.append(("on_session_end", (session_id,), kwargs))
        self.session_id = None

    async def checkin(self, purpose: str, **kwargs: Any) -> Any:
        self.events.append(("checkin", (purpose,), kwargs))
        return None

    async def gate(self, tool_name: str, args: dict[str, Any]) -> Any:
        self.events.append(("gate", (tool_name, args), {}))
        return FakeDirective()

    async def outcome_event(self, tool_name: str, *, success: bool, details: dict[str, Any] | None = None) -> None:
        self.events.append(("outcome_event", (tool_name,), {"success": success, "details": details}))

    async def annotate(self, tool_name: str, args: dict[str, Any], result: Any) -> Any:
        self.events.append(("annotate", (tool_name, args, result), {}))
        return FakeAnnotated()


def test_registers_default_hermes_lifecycle_hooks_without_tool_hooks() -> None:
    ctx = FakeCtx()
    register(ctx, adapter=FakeAdapter())

    assert "pre_llm_call" in ctx.hooks
    assert "post_llm_call" in ctx.hooks
    assert "on_session_finalize" in ctx.hooks
    assert "on_session_end" not in ctx.hooks
    assert "pre_tool_call" not in ctx.hooks
    assert "post_tool_call" not in ctx.hooks
    assert "transform_tool_result" not in ctx.hooks


def test_registers_tool_hooks_only_when_opted_in() -> None:
    ctx = FakeCtx()
    register(ctx, adapter=FakeAdapter(), enable_gate=True, enable_ambient=True, enable_outcomes=True)

    assert "pre_tool_call" in ctx.hooks
    assert "post_tool_call" in ctx.hooks
    assert "transform_tool_result" in ctx.hooks


def test_pre_llm_call_is_sync_and_lazy_onboards_session() -> None:
    ctx = FakeCtx()
    adapter = FakeAdapter()
    register(ctx, adapter=adapter)

    result = ctx.hooks["pre_llm_call"](
        session_id="hermes-session-1",
        user_message="hello",
        model="test-model",
        platform="cli",
        is_first_turn=True,
    )

    assert not inspect.isawaitable(result)
    assert adapter.events[0][0] == "on_session_start"
    assert adapter.events[0][1] == ("hermes-session-1",)
    assert adapter.events[0][2]["purpose"] == "hermes:cli:test-model"


def test_post_llm_call_is_sync_and_emits_turn_checkin() -> None:
    ctx = FakeCtx()
    adapter = FakeAdapter()
    register(ctx, adapter=adapter)

    ctx.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli")
    result = ctx.hooks["post_llm_call"](
        session_id="s1",
        assistant_response="Completed the requested work.",
        model="m",
        platform="cli",
    )

    assert not inspect.isawaitable(result)
    assert adapter.events[-1][0] == "checkin"
    purpose = adapter.events[-1][1][0]
    assert purpose == "Hermes assistant turn completed"
    assert "Completed the requested work" not in purpose
    assert adapter.events[-1][2]["response_mode"] == "minimal"
    assert adapter.events[-1][2]["complexity"] == 0.2
    assert "confidence" not in adapter.events[-1][2]
    assert adapter.events[-1][2]["epistemic_class"] == "substrate_interpretation"
    assert adapter.events[-1][2]["provenance_context"] == {
        "harness_type": "hermes_plugin",
        "governance_mode": "automatic_turn_checkin",
        "tool_surface": "hermes_lifecycle_hook",
        "transport": "streamable_http",
        "verification_source": "hook_observation",
    }


def test_post_llm_call_does_not_persist_secret_shaped_response_content() -> None:
    """Automatic governance metadata must never include assistant response text."""
    ctx = FakeCtx()
    adapter = FakeAdapter()
    register(ctx, adapter=adapter)

    ctx.hooks["post_llm_call"](
        session_id="secret-session",
        assistant_response="DATABASE_URL=postgres://private:secret@example/db",
        model="m",
        platform="cli",
    )

    checkin_event = adapter.events[-1]
    assert "private" not in repr(checkin_event)
    assert "secret" not in repr(checkin_event)


def test_alternating_sessions_keep_distinct_adapters_and_continuity() -> None:
    ctx = FakeCtx()
    adapters: list[FakeAdapter] = []

    def factory() -> FakeAdapter:
        adapter = FakeAdapter()
        adapters.append(adapter)
        return adapter

    register(ctx, adapter_factory=factory)

    ctx.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli")
    ctx.hooks["post_llm_call"](session_id="s1", assistant_response="first", model="m", platform="cli")
    ctx.hooks["pre_llm_call"](session_id="s2", model="m", platform="cli")
    ctx.hooks["post_llm_call"](session_id="s2", assistant_response="second", model="m", platform="cli")
    ctx.hooks["post_llm_call"](session_id="s1", assistant_response="third", model="m", platform="cli")

    assert len(adapters) == 2
    assert [event[0] for event in adapters[0].events] == ["on_session_start", "checkin", "checkin"]
    assert [event[0] for event in adapters[1].events] == ["on_session_start", "checkin"]
    assert adapters[0].events[0][1] == ("s1",)
    assert adapters[1].events[0][1] == ("s2",)


def test_session_finalize_clears_adapter_for_true_boundary() -> None:
    ctx = FakeCtx()
    adapters: list[FakeAdapter] = []

    def factory() -> FakeAdapter:
        adapter = FakeAdapter()
        adapters.append(adapter)
        return adapter

    register(ctx, adapter_factory=factory)

    ctx.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli")
    ctx.hooks["post_llm_call"](session_id="s1", assistant_response="first", model="m", platform="cli")
    ctx.hooks["on_session_finalize"](session_id="s1")
    ctx.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli")
    ctx.hooks["post_llm_call"](session_id="s1", assistant_response="second", model="m", platform="cli")

    assert len(adapters) == 2
    assert [event[0] for event in adapters[0].events] == ["on_session_start", "checkin", "on_session_end"]
    assert [event[0] for event in adapters[1].events] == ["on_session_start", "checkin"]


def test_session_finalize_is_fail_open_and_allows_a_fresh_session_adapter() -> None:
    """Cleanup failures must not escape into Hermes or strand circuit state."""

    class FailingEndAdapter(FakeAdapter):
        async def on_session_end(self, session_id: str, **kwargs: Any) -> None:
            self.events.append(("on_session_end", (session_id,), kwargs))
            raise RuntimeError("cleanup failed")

    ctx = FakeCtx()
    adapters: list[FakeAdapter] = []

    def factory() -> FakeAdapter:
        created: FakeAdapter
        if not adapters:
            created = FailingEndAdapter()
        else:
            created = FakeAdapter()
        adapters.append(created)
        return created

    register(ctx, adapter_factory=factory)
    ctx.hooks["pre_llm_call"](
        session_id="finalize-failure", model="m", platform="cli"
    )

    assert ctx.hooks["on_session_finalize"](session_id="finalize-failure") is None
    ctx.hooks["pre_llm_call"](
        session_id="finalize-failure", model="m", platform="cli"
    )

    assert len(adapters) == 2
    assert adapters[1].events[0][0] == "on_session_start"


def test_tool_hooks_are_sync_and_delegate_to_adapter() -> None:
    ctx = FakeCtx()
    adapter = FakeAdapter()
    register(ctx, adapter=adapter, enable_gate=True, enable_ambient=True, enable_outcomes=True)

    ctx.hooks["pre_llm_call"](session_id="s-tool", model="m", platform="cli")
    adapter.events.clear()

    block = ctx.hooks["pre_tool_call"](session_id="s-tool", tool_name="Bash", args={"command": "x"})
    ctx.hooks["post_tool_call"](session_id="s-tool", tool_name="Bash", status="ok", error_type="", error_message="")
    transformed = ctx.hooks["transform_tool_result"](session_id="s-tool", tool_name="Bash", args={}, result="raw")

    assert block == {"action": "block", "message": "blocked by test"}
    assert transformed == "annotated result"
    assert [event[0] for event in adapter.events] == ["gate", "outcome_event", "annotate"]


def test_outcome_hook_parses_error_result_when_status_is_absent() -> None:
    ctx = FakeCtx()
    adapter = FakeAdapter()
    register(ctx, adapter=adapter, enable_outcomes=True)
    ctx.hooks["pre_llm_call"](session_id="s-outcome", model="m", platform="cli")
    adapter.events.clear()

    ctx.hooks["post_tool_call"](
        session_id="s-outcome",
        tool_name="Bash",
        result='{"error": "boom"}',
    )

    assert adapter.events[-1][0] == "outcome_event"
    assert adapter.events[-1][2]["success"] is False


def test_outcome_hook_omits_raw_error_messages() -> None:
    ctx = FakeCtx()
    adapter = FakeAdapter()
    register(ctx, adapter=adapter, enable_outcomes=True)

    ctx.hooks["post_tool_call"](
        session_id="s-private-outcome",
        tool_name="Bash",
        status="error",
        error_type="RuntimeError",
        error_message="Authorization: Bearer private-token",
    )

    details = adapter.events[-1][2]["details"]
    assert details == {"status": "error", "error_type": "RuntimeError"}
    assert "private-token" not in repr(adapter.events[-1])


def test_tool_hooks_can_fall_back_to_task_id_when_session_id_missing() -> None:
    ctx = FakeCtx()
    adapter = FakeAdapter()
    register(ctx, adapter=adapter, enable_gate=True)

    block = ctx.hooks["pre_tool_call"](task_id="task-only", tool_name="Bash", args={"command": "x"})

    assert block == {"action": "block", "message": "blocked by test"}
    assert [event[0] for event in adapter.events] == ["on_session_start", "gate"]
    assert adapter.events[0][1] == ("task-only",)


def test_turn_checkin_failures_open_a_fail_open_session_circuit() -> None:
    """A broken governance endpoint must not fail or retry on every Hermes turn."""

    class FailingCheckinAdapter(FakeAdapter):
        async def checkin(self, purpose: str, **kwargs: Any) -> Any:
            self.events.append(("checkin", (purpose,), kwargs))
            raise RuntimeError("unknown tool")

    ctx = FakeCtx()
    adapter = FailingCheckinAdapter()
    register(ctx, adapter=adapter)
    ctx.hooks["pre_llm_call"](session_id="circuit", model="m", platform="cli")

    for _ in range(5):
        assert (
            ctx.hooks["post_llm_call"](
                session_id="circuit",
                assistant_response="response",
                model="m",
                platform="cli",
            )
            is None
        )

    checkins = [event for event in adapter.events if event[0] == "checkin"]
    assert len(checkins) == 3


def test_adapter_factory_failures_are_fail_open_and_circuit_bounded() -> None:
    ctx = FakeCtx()
    attempts = 0

    def failing_factory() -> FakeAdapter:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("factory failed")

    register(ctx, adapter_factory=failing_factory)

    for _ in range(5):
        assert (
            ctx.hooks["pre_llm_call"](
                session_id="factory-circuit", model="m", platform="cli"
            )
            is None
        )

    assert attempts == 3
