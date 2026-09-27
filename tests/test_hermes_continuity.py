"""Hermes continuity, release, and per-turn count tests (adapter 0.4.0 scope).

A Hermes session that descends from another (context compression, /resume,
delegate_task subagent) must declare lineage to the predecessor's UNITARES
identity instead of minting an unrelated one. Tool calls are counted locally
and sent as numeric afferents on the turn check-in — never text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from unitares_host_adapter.bindings.hermes import SessionLinks, register


class FakeCtx:
    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks[name] = callback


class LineageAdapter:
    """Fake adapter that mints a predictable UUID per on_session_start."""

    minted = 0

    def __init__(self) -> None:
        self.events: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.session_id: Optional[str] = None
        self.agent_uuid: Optional[str] = None
        self.released = 0

    async def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        LineageAdapter.minted += 1
        self.session_id = session_id
        self.agent_uuid = f"uuid-{LineageAdapter.minted}"
        self.events.append(("on_session_start", (session_id,), kwargs))

    async def on_session_end(self, session_id: str, **kwargs: Any) -> None:
        self.events.append(("on_session_end", (session_id,), kwargs))
        self.session_id = None

    async def release_presence(self) -> bool:
        self.released += 1
        self.events.append(("release_presence", (), {}))
        return True

    async def checkin(self, purpose: str, **kwargs: Any) -> Any:
        self.events.append(("checkin", (purpose,), kwargs))
        return None


def _setup(tmp_path: Path, lookup=None):
    ctx = FakeCtx()
    adapters: list[LineageAdapter] = []

    def factory() -> LineageAdapter:
        a = LineageAdapter()
        adapters.append(a)
        return a

    links = SessionLinks(tmp_path / "sessions.json")
    register(ctx, adapter_factory=factory, session_links=links, session_lookup=lookup or (lambda s: None))
    return ctx, adapters, links


def _start_kwargs(adapter: LineageAdapter) -> dict[str, Any]:
    return next(e[2] for e in adapter.events if e[0] == "on_session_start")


def test_first_session_declares_no_lineage(tmp_path: Path) -> None:
    ctx, adapters, links = _setup(tmp_path)
    ctx.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli")

    kwargs = _start_kwargs(adapters[0])
    assert "parent_agent_id" not in kwargs
    assert "spawn_reason" not in kwargs
    assert links.get("s1") == adapters[0].agent_uuid


def test_compression_child_declares_compaction_lineage_and_retires_parent(tmp_path: Path) -> None:
    sessions = {"child": {"parent_session_id": "parent", "parent_end_reason": "compression"}}
    ctx, adapters, links = _setup(tmp_path, lookup=sessions.get)

    ctx.hooks["pre_llm_call"](session_id="parent", model="m", platform="cli")
    parent_uuid = adapters[0].agent_uuid
    ctx.hooks["pre_llm_call"](session_id="child", model="m", platform="cli", parent_session_id="parent")

    kwargs = _start_kwargs(adapters[1])
    assert kwargs["parent_agent_id"] == parent_uuid
    assert kwargs["spawn_reason"] == "compaction"
    # compressed-away session gets no finalize hook from Hermes; the binding
    # releases it when the child takes over.
    assert adapters[0].released == 1
    assert [e[0] for e in adapters[0].events][-2:] == ["release_presence", "on_session_end"]


def test_non_compression_parent_is_an_explicit_successor(tmp_path: Path) -> None:
    sessions = {"child": {"parent_session_id": "parent", "parent_end_reason": "session_reset"}}
    ctx, adapters, _ = _setup(tmp_path, lookup=sessions.get)

    ctx.hooks["pre_llm_call"](session_id="parent", model="m", platform="cli")
    ctx.hooks["on_session_finalize"](session_id="parent")
    ctx.hooks["pre_llm_call"](session_id="child", model="m", platform="cli")

    kwargs = _start_kwargs(adapters[1])
    assert kwargs["spawn_reason"] == "explicit"
    assert kwargs["parent_agent_id"] == adapters[0].agent_uuid


def test_subagent_declares_subagent_lineage_to_live_parent(tmp_path: Path) -> None:
    ctx, adapters, _ = _setup(tmp_path)
    ctx.hooks["pre_llm_call"](session_id="parent", model="m", platform="cli")
    ctx.hooks["pre_llm_call"](
        session_id="sub", model="m", platform="subagent", parent_session_id="parent"
    )

    kwargs = _start_kwargs(adapters[1])
    assert kwargs["spawn_reason"] == "subagent"
    assert kwargs["parent_agent_id"] == adapters[0].agent_uuid
    # The dispatcher stays live: no release of the parent.
    assert adapters[0].released == 0


def test_resume_of_mapped_session_in_a_new_process_is_explicit_successor(tmp_path: Path) -> None:
    ctx, adapters, _ = _setup(tmp_path)
    ctx.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli")
    first_uuid = adapters[0].agent_uuid
    ctx.hooks["on_session_finalize"](session_id="s1")

    # A new process re-registers and reads the persisted link file.
    ctx2, adapters2, _ = _setup(tmp_path)
    ctx2.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli")

    kwargs = _start_kwargs(adapters2[0])
    assert kwargs["spawn_reason"] == "explicit"
    assert kwargs["parent_agent_id"] == first_uuid


def test_unknown_parent_declares_no_lineage(tmp_path: Path) -> None:
    sessions = {"child": {"parent_session_id": "never-seen", "parent_end_reason": "compression"}}
    ctx, adapters, _ = _setup(tmp_path, lookup=sessions.get)
    ctx.hooks["pre_llm_call"](session_id="child", model="m", platform="cli")

    assert "parent_agent_id" not in _start_kwargs(adapters[0])


def test_finalize_releases_presence_once(tmp_path: Path) -> None:
    ctx, adapters, _ = _setup(tmp_path)
    ctx.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli")
    ctx.hooks["on_session_finalize"](session_id="s1")
    ctx.hooks["on_session_finalize"](session_id="s1")

    assert adapters[0].released == 1


def test_link_file_stores_uuids_only_never_proof(tmp_path: Path) -> None:
    ctx, adapters, _ = _setup(tmp_path)
    ctx.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli")

    stored = json.loads((tmp_path / "sessions.json").read_text())
    assert set(stored["s1"]) == {"agent_uuid", "t"}
    assert "client_session_id" not in json.dumps(stored)


def test_corrupt_or_unwritable_link_file_is_fail_open(tmp_path: Path) -> None:
    bad = tmp_path / "sessions.json"
    bad.write_text("{not json")
    ctx = FakeCtx()
    adapters: list[LineageAdapter] = []

    def factory() -> LineageAdapter:
        a = LineageAdapter()
        adapters.append(a)
        return a

    register(ctx, adapter_factory=factory, session_links=SessionLinks(bad), session_lookup=lambda s: None)
    ctx.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli")
    assert adapters[0].session_id == "s1"

    # Unwritable: parent path is a file, so mkdir fails. Still onboards.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    ctx2 = FakeCtx()
    register(
        ctx2,
        adapter_factory=factory,
        session_links=SessionLinks(blocker / "sessions.json"),
        session_lookup=lambda s: None,
    )
    ctx2.hooks["pre_llm_call"](session_id="s2", model="m", platform="cli")
    assert adapters[-1].session_id == "s2"


def test_session_lookup_errors_are_fail_open(tmp_path: Path) -> None:
    def boom(session_id: str) -> dict[str, Any]:
        raise RuntimeError("db locked")

    ctx, adapters, _ = _setup(tmp_path, lookup=boom)
    ctx.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli")
    assert adapters[0].session_id == "s1"


def test_link_file_is_bounded(tmp_path: Path) -> None:
    links = SessionLinks(tmp_path / "sessions.json")
    for i in range(230):
        links.set(f"s{i}", f"u{i}")
    stored = json.loads((tmp_path / "sessions.json").read_text())
    assert len(stored) == 200


def test_turn_checkin_carries_numeric_tool_counts_and_resets(tmp_path: Path) -> None:
    ctx, adapters, _ = _setup(tmp_path)
    ctx.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli")
    ctx.hooks["post_tool_call"](
        session_id="s1", tool_name="terminal", args={"command": "cat /etc/secret"},
        result='{"output": "TOPSECRET"}', status="ok", duration_ms=40,
    )
    ctx.hooks["post_tool_call"](
        session_id="s1", tool_name="read_file", args={"path": "/private"},
        result='{"error": "denied PRIVATEPATH"}', status="error",
        error_message="PRIVATEPATH denied", duration_ms=2,
    )
    ctx.hooks["post_llm_call"](session_id="s1", assistant_response="done", model="m", platform="cli")

    checkin = [e for e in adapters[0].events if e[0] == "checkin"][-1]
    afferents = checkin[2]["afferents"]
    assert set(afferents) == {"turn_tool_calls", "turn_tool_errors", "turn_tool_ms", "turn_wall_ms"}
    assert afferents["turn_tool_calls"] == 2
    assert afferents["turn_tool_errors"] == 1
    assert afferents["turn_tool_ms"] == 42
    assert all(isinstance(v, (int, float)) for v in afferents.values())
    assert checkin[2]["complexity"] == 0.2
    for secret in ("TOPSECRET", "PRIVATEPATH", "/etc/secret", "terminal", "read_file"):
        assert secret not in repr(checkin)

    ctx.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli")
    ctx.hooks["post_llm_call"](session_id="s1", assistant_response="again", model="m", platform="cli")
    second = [e for e in adapters[0].events if e[0] == "checkin"][-1][2]["afferents"]
    assert second["turn_tool_calls"] == 0
    assert second["turn_tool_errors"] == 0


def test_default_post_tool_call_makes_no_network_call(tmp_path: Path) -> None:
    ctx, adapters, _ = _setup(tmp_path)
    ctx.hooks["pre_llm_call"](session_id="s1", model="m", platform="cli")
    before = list(adapters[0].events)
    ctx.hooks["post_tool_call"](session_id="s1", tool_name="terminal", status="ok", duration_ms=5)
    assert adapters[0].events == before
