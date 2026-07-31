"""Tests for UnitaresAdapter with a fake MCP transport."""

from __future__ import annotations

from typing import Any

import pytest

from unitares_host_adapter import UnitaresAdapter


class FakeTransport:
    """Minimal fake that records calls and returns canned verdicts.

    Tests pass a simple {"action", "message", "margin"} dict; the fake wraps it
    in the real process_agent_update shape ({"verdict": {"value": ...}, ...}) so
    the parser is exercised against the contract it sees in production."""

    def __init__(
        self,
        verdict: dict[str, Any] | None = None,
        *,
        available_tools: set[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.list_tools_calls = 0
        self._verdict = verdict or {"action": "proceed", "message": ""}
        self._available_tools = available_tools or {
            "onboard",
            "sync_state",
            "record_result",
        }

    async def list_tools(self) -> set[str]:
        self.list_tools_calls += 1
        return self._available_tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "record_result":
            assert set(arguments) <= {
                "agent_id",
                "session_id",
                "client_session_id",
                "outcome_type",
                "detail",
                "verification_source",
                "prediction_id",
            }
        self.calls.append((name, arguments))
        if name == "sync_state":
            v = self._verdict
            response = {
                "verdict": {"value": v.get("action", "proceed"), "meaning": v.get("message", "")},
                "margin": v.get("margin"),
            }
            if v.get("prediction_id"):
                response["prediction_id"] = v["prediction_id"]
            return response
        if name == "onboard":
            return {"agent_uuid": "u-1", "client_session_id": "csid-1"}
        return {}


class FailingOnboardTransport(FakeTransport):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name == "onboard":
            raise RuntimeError("onboard failed")
        return await super().call_tool(name, arguments)


@pytest.mark.asyncio
async def test_checkin_returns_verdict_with_action():
    t = FakeTransport({"action": "guide", "message": "near boundary"})
    a = UnitaresAdapter(t)
    v = await a.checkin("unit test")
    assert v.action == "guide"
    assert v.message == "near boundary"
    assert not v.blocks


@pytest.mark.asyncio
async def test_gate_returns_block_on_pause():
    t = FakeTransport({"action": "pause", "message": "high entropy"})
    a = UnitaresAdapter(t)
    d = await a.gate("Bash", {"command": "rm -rf /"})
    assert d is not None
    assert d.as_dict() == {"action": "block", "message": "high entropy"}


@pytest.mark.asyncio
async def test_gate_returns_none_on_proceed():
    t = FakeTransport({"action": "proceed"})
    a = UnitaresAdapter(t)
    d = await a.gate("Read", {"path": "/tmp/x"})
    assert d is None


@pytest.mark.asyncio
async def test_gate_returns_block_on_reject():
    t = FakeTransport({"action": "reject", "message": "policy violation"})
    a = UnitaresAdapter(t)
    d = await a.gate("tool", {})
    assert d is not None
    assert d.as_dict()["action"] == "block"
    assert "policy violation" in d.as_dict()["message"]


@pytest.mark.asyncio
async def test_annotate_empty_on_proceed_no_margin():
    t = FakeTransport({"action": "proceed"})
    a = UnitaresAdapter(t)
    ar = await a.annotate("Read", {}, "file contents")
    assert ar.result == "file contents"
    assert ar.annotation == ""
    assert ar.render() == "file contents"


@pytest.mark.asyncio
async def test_annotate_carries_margin_and_guidance():
    t = FakeTransport({"action": "guide", "margin": "tight", "message": "watch entropy"})
    a = UnitaresAdapter(t)
    ar = await a.annotate("Read", {}, "file contents")
    assert "guide" in ar.annotation
    assert "tight" in ar.annotation
    assert "watch entropy" in ar.annotation
    rendered = ar.render()
    assert "file contents" in rendered
    assert "[UNITARES]" in rendered


@pytest.mark.asyncio
async def test_on_session_start_calls_onboard_with_real_identity_args():
    t = FakeTransport()
    a = UnitaresAdapter(t, agent_label="Hermes Test", model_type="hermes-test")
    await a.on_session_start("s-123", purpose="test")
    assert a.session_id == "s-123"
    assert t.list_tools_calls == 1
    assert t.calls[0][0] == "onboard"
    assert t.calls[0][1]["force_new"] is True
    assert t.calls[0][1]["name"] == "Hermes Test"
    assert t.calls[0][1]["model_type"] == "hermes-test"
    assert t.calls[0][1]["client_hint"] == "test"
    assert "purpose" not in t.calls[0][1]


@pytest.mark.asyncio
async def test_on_session_start_captures_client_session_id():
    t = FakeTransport()
    a = UnitaresAdapter(t)
    assert a.client_session_id is None
    await a.on_session_start("s-123")
    # Captured from the onboard response, distinct from the host session id.
    assert a.client_session_id == "csid-1"


@pytest.mark.asyncio
async def test_on_session_start_refuses_incompatible_public_tool_surface_before_onboard():
    """An incompatible server must not receive an identity-minting call."""
    t = FakeTransport(available_tools={"onboard", "process_agent_update"})
    a = UnitaresAdapter(t)

    with pytest.raises(RuntimeError, match="sync_state"):
        await a.on_session_start("s-incompatible")

    assert t.calls == []


@pytest.mark.asyncio
async def test_calls_echo_client_session_id_after_onboard():
    # The strict-safety contract: every post-onboard call must carry the
    # captured proof, or it resolves by transport fingerprint to a sibling.
    t = FakeTransport({"action": "proceed"})
    a = UnitaresAdapter(t)
    await a.on_session_start("s-1")
    await a.checkin("did work")
    await a.gate("Bash", {"command": "ls"})
    await a.annotate("Read", {}, "contents")
    await a.outcome_event("Bash", success=True)
    post_onboard = [args for name, args in t.calls if name != "onboard"]
    assert post_onboard, "expected calls after onboard"
    assert all(a.client_session_id == "csid-1" and args.get("client_session_id") == "csid-1"
               for args in post_onboard)


@pytest.mark.asyncio
async def test_no_client_session_id_echoed_before_onboard():
    # Without an onboard, there is no proof to echo — calls go out bare and the
    # server applies its own (fingerprint) resolution; we do not fabricate one.
    t = FakeTransport({"action": "proceed"})
    a = UnitaresAdapter(t)
    await a.checkin("no session yet")
    assert "client_session_id" not in t.calls[-1][1]


@pytest.mark.asyncio
async def test_session_start_does_not_rebind_or_reuse_stale_proof_on_onboard_failure():
    t = FailingOnboardTransport()
    a = UnitaresAdapter(t)
    a._session_id = "old-session"
    a._client_session_id = "old-csid"

    with pytest.raises(RuntimeError, match="onboard failed"):
        await a.on_session_start("new-session", purpose="retry-test")

    assert a.session_id == "old-session"
    assert a.client_session_id == "old-csid"

    with pytest.raises(RuntimeError, match="onboard failed"):
        await a.on_session_start("new-session", purpose="retry-test")

    onboard_calls = [call for call in t.calls if call[0] == "onboard"]
    assert len(onboard_calls) == 2


@pytest.mark.asyncio
async def test_session_end_clears_client_session_id():
    t = FakeTransport()
    a = UnitaresAdapter(t)
    await a.on_session_start("s-1")
    assert a.client_session_id == "csid-1"
    await a.on_session_end("s-1")
    assert a.client_session_id is None


@pytest.mark.asyncio
async def test_outcome_event_feeds_calibration():
    t = FakeTransport()
    a = UnitaresAdapter(t)
    await a.outcome_event("Bash", success=True, details={"exit_code": 0})
    name, args = t.calls[-1]
    assert name == "record_result"
    # Real contract: required outcome_type enum, detail (singular) carries metadata.
    assert args["outcome_type"] == "task_completed"
    assert args["detail"] == {
        "tool_name": "Bash",
        "exit_code": 0,
        "public_operation": "record_result",
    }
    assert args["verification_source"] == "agent_reported_tool_result"
    assert "success" not in args and "details" not in args


@pytest.mark.asyncio
async def test_outcome_event_keeps_observed_identity_authoritative():
    t = FakeTransport()
    a = UnitaresAdapter(t)
    await a.outcome_event(
        "Bash",
        success=True,
        details={"tool_name": "spoofed", "public_operation": "spoofed"},
    )

    detail = t.calls[-1][1]["detail"]
    assert detail["tool_name"] == "Bash"
    assert detail["public_operation"] == "record_result"


@pytest.mark.asyncio
async def test_checkin_names_public_operation_in_provenance():
    t = FakeTransport({"action": "proceed"})
    a = UnitaresAdapter(t)

    await a.checkin(
        "did work",
        provenance_context={"governance_mode": "automatic_turn_checkin"},
    )

    name, args = t.calls[-1]
    assert name == "sync_state"
    assert args["provenance_context"] == {
        "governance_mode": "automatic_turn_checkin",
        "public_operation": "sync_state",
    }


@pytest.mark.asyncio
async def test_checkin_redacts_nested_provenance_and_keeps_operation_authoritative():
    t = FakeTransport({"action": "proceed"})
    a = UnitaresAdapter(t)

    await a.checkin(
        "did work",
        provenance_context={
            "public_operation": "spoofed",
            "nested": {"authorization": "Bearer private-token"},
            "dsn": "postgres://alice:private-pass@example/db",
        },
    )

    provenance = t.calls[-1][1]["provenance_context"]
    assert provenance["public_operation"] == "sync_state"
    assert "private-token" not in repr(provenance)
    assert "private-pass" not in repr(provenance)
    assert "[REDACTED]" in repr(provenance)


@pytest.mark.asyncio
async def test_prediction_id_binding_is_explicit_per_outcome():
    t = FakeTransport({"action": "proceed", "prediction_id": "pred-public-1"})
    a = UnitaresAdapter(t)

    verdict = await a.checkin("bounded prediction", confidence=0.8)
    await a.outcome_event(
        "Bash",
        success=True,
        prediction_id=verdict.raw["prediction_id"],
    )
    first_outcome = t.calls[-1][1]
    assert first_outcome["prediction_id"] == "pred-public-1"

    await a.outcome_event("Read", success=True)
    second_outcome = t.calls[-1][1]
    assert "prediction_id" not in second_outcome


@pytest.mark.asyncio
async def test_missing_prediction_id_does_not_create_a_binding():
    t = FakeTransport({"action": "proceed"})
    a = UnitaresAdapter(t)

    await a.checkin("metadata-only checkin")
    await a.outcome_event("Read", success=True)

    assert "prediction_id" not in t.calls[-1][1]


@pytest.mark.asyncio
async def test_tool_telemetry_redacts_nested_secrets_and_dsn_credentials():
    t = FakeTransport()
    a = UnitaresAdapter(t)

    await a.gate(
        "Bash",
        {
            "env": {"API_TOKEN": "private-token"},
            "command": "connect postgres://alice:private-pass@example/db",
        },
    )
    gate_args = t.calls[-1][1]
    assert "private-token" not in gate_args["response_text"]
    assert "private-pass" not in gate_args["response_text"]
    assert "[REDACTED]" in gate_args["response_text"]

    await a.outcome_event(
        "Bash",
        success=False,
        details={"error_type": "RuntimeError", "password": "private-pass"},
    )
    outcome_args = t.calls[-1][1]
    assert "private-pass" not in repr(outcome_args["detail"])
    assert outcome_args["detail"]["password"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_outcome_event_failure_maps_to_task_failed():
    t = FakeTransport()
    a = UnitaresAdapter(t)
    await a.outcome_event("Bash", success=False)
    assert t.calls[-1][1]["outcome_type"] == "task_failed"


@pytest.mark.asyncio
async def test_response_mode_propagates_through_checkin():
    t = FakeTransport({"action": "proceed"})
    a = UnitaresAdapter(t)
    await a.checkin("p", response_mode="full")
    assert t.calls[-1][1]["response_mode"] == "full"


@pytest.mark.asyncio
async def test_gate_uses_minimal_response_mode():
    t = FakeTransport({"action": "proceed"})
    a = UnitaresAdapter(t)
    await a.gate("Read", {})
    assert t.calls[-1][1]["response_mode"] == "minimal"


# --- reconciled tool vocabulary (real governance contract) ------------------

@pytest.mark.asyncio
async def test_checkin_targets_public_sync_state_alias_with_real_args():
    # Public MCP surfaces expose sync_state, and purpose maps to response_text.
    t = FakeTransport({"action": "proceed"})
    a = UnitaresAdapter(t)
    await a.checkin("did the thing", confidence=0.8)
    name, args = t.calls[-1]
    assert name == "sync_state"
    assert args["response_text"] == "did the thing"
    assert "complexity" in args and 0.0 <= args["complexity"] <= 1.0
    assert args["confidence"] == 0.8
    assert "purpose" not in args  # the fictional arg is gone


@pytest.mark.asyncio
async def test_verdict_parsed_from_verdict_value_shape():
    # process_agent_update nests the action under verdict.value, margin at top.
    t = FakeTransport({"action": "pause", "message": "high entropy", "margin": "tight"})
    a = UnitaresAdapter(t)
    v = await a.checkin("x")
    assert v.action == "pause"
    assert v.blocks
    assert v.message == "high entropy"
    assert v.margin == "tight"


@pytest.mark.asyncio
async def test_gate_folds_context_into_response_text_no_tool_context():
    t = FakeTransport({"action": "proceed"})
    a = UnitaresAdapter(t)
    await a.gate("Bash", {"command": "ls"})
    _, args = t.calls[-1]
    assert "tool_context" not in args  # server has no such param
    assert args["response_text"].startswith("gate:Bash")
    assert args["complexity"] == 0.1  # synthetic check-in stays low-impact


@pytest.mark.asyncio
async def test_annotate_lite_maps_to_minimal_response_mode():
    t = FakeTransport({"action": "proceed"})
    a = UnitaresAdapter(t)
    await a.annotate("Read", {}, "contents")  # default response_mode="lite"
    name, args = t.calls[-1]
    assert name == "sync_state"
    assert args["response_mode"] == "minimal"
