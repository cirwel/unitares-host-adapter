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
