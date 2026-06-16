"""Tests for UnitaresAdapter with a fake MCP transport."""

from __future__ import annotations

from typing import Any

import pytest

from unitares_host_adapter import UnitaresAdapter


class FakeTransport:
    """Minimal fake that records calls and returns canned verdicts."""

    def __init__(self, verdict: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._verdict = verdict or {"action": "proceed", "message": ""}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name == "checkin":
            return self._verdict
        if name == "onboard":
            return {"agent_uuid": "u-1", "client_session_id": "csid-1"}
        return {}


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
async def test_on_session_start_calls_onboard():
    t = FakeTransport()
    a = UnitaresAdapter(t)
    await a.on_session_start("s-123", purpose="test")
    assert a.session_id == "s-123"
    assert t.calls[0][0] == "onboard"
    assert t.calls[0][1]["force_new"] is True
    assert t.calls[0][1]["purpose"] == "test"


@pytest.mark.asyncio
async def test_on_session_start_captures_client_session_id():
    t = FakeTransport()
    a = UnitaresAdapter(t)
    assert a.client_session_id is None
    await a.on_session_start("s-123")
    # Captured from the onboard response, distinct from the host session id.
    assert a.client_session_id == "csid-1"


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
    assert t.calls[-1][0] == "outcome_event"
    assert t.calls[-1][1]["success"] is True
    assert t.calls[-1][1]["details"] == {"exit_code": 0}


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
