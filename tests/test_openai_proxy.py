"""Tests for the OpenAI-compatible governance proxy.

A fake adapter records governance calls; an httpx.MockTransport stands in for
the upstream Ollama server, so neither a live MCP nor a live model is needed."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from unitares_host_adapter.bindings.openai_proxy import GovernanceProxy, build_app  # noqa: E402
from unitares_host_adapter.types import Verdict  # noqa: E402


class FakeAdapter:
    """Records on_session_start / checkin; returns a configurable verdict."""

    def __init__(self, verdict: Verdict | None = None, *, start_raises=False, checkin_raises=False):
        self.verdict = verdict or Verdict(action="proceed")
        self.start_raises = start_raises
        self.checkin_raises = checkin_raises
        self.starts: list[str] = []
        self.checkins: list[str] = []

    async def on_session_start(self, session_id: str, *, purpose: str = "", **_: Any) -> None:
        if self.start_raises:
            raise RuntimeError("governance unreachable")
        self.starts.append(session_id)

    async def checkin(self, purpose: str, **_: Any) -> Verdict:
        if self.checkin_raises:
            raise RuntimeError("checkin failed")
        self.checkins.append(purpose)
        return self.verdict


def _make(verdict=None, *, mode="observe", upstream_handler=None, **fake_kw):
    calls: list[httpx.Request] = []

    def default_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"id": "up-1", "choices": [{"message": {"content": "hi"}}]})
        return httpx.Response(200, json={"data": ["model-a"]})

    handler = upstream_handler or default_handler
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://upstream")
    adapter = FakeAdapter(verdict, **fake_kw)
    proxy = GovernanceProxy(adapter, upstream, mode=mode)
    app = build_app(proxy)
    return app, adapter, calls


CHAT_BODY = {"model": "gemma4", "messages": [{"role": "user", "content": "hi"}]}


def test_observe_forwards_and_checks_in():
    app, adapter, calls = _make()
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions", json=CHAT_BODY)
    assert r.status_code == 200
    assert r.json()["id"] == "up-1"  # upstream body passed through
    assert calls and calls[-1].url.path == "/v1/chat/completions"  # forwarded
    # observe-mode check-in runs as a post-response background task
    assert adapter.checkins == ["openai-proxy: gemma4 chat (1 msgs)"]
    assert adapter.starts == ["openai-proxy"]  # onboarded once


def test_observe_fails_open_when_checkin_raises():
    app, adapter, calls = _make(checkin_raises=True)
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions", json=CHAT_BODY)
    assert r.status_code == 200
    assert r.json()["id"] == "up-1"  # governance error never breaks the model


def test_observe_fails_open_when_onboard_raises():
    app, adapter, calls = _make(start_raises=True)
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions", json=CHAT_BODY)
    assert r.status_code == 200
    assert calls[-1].url.path == "/v1/chat/completions"
    assert adapter.checkins == []  # degraded: no check-in, but still forwarded


def test_enforce_blocks_on_pause_and_does_not_forward():
    app, adapter, calls = _make(Verdict(action="pause", message="high risk"), mode="enforce")
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions", json=CHAT_BODY)
    assert r.status_code == 200
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "content_filter"
    assert "high risk" in choice["message"]["content"]
    assert calls == []  # upstream never called


def test_enforce_allows_on_proceed_and_forwards():
    app, adapter, calls = _make(Verdict(action="proceed"), mode="enforce")
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions", json=CHAT_BODY)
    assert r.json()["id"] == "up-1"
    assert calls[-1].url.path == "/v1/chat/completions"


def test_passthrough_is_not_governed():
    app, adapter, calls = _make()
    with TestClient(app) as client:
        r = client.get("/v1/models")
    assert r.status_code == 200
    assert r.json()["data"] == ["model-a"]
    assert adapter.checkins == []  # non-chat paths are forwarded verbatim


def test_streaming_chat_passes_through():
    chunks = [b"data: {\"x\":1}\n\n", b"data: [DONE]\n\n"]

    def stream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"".join(chunks)),
                              headers={"content-type": "text/event-stream"})

    app, adapter, calls = _make(upstream_handler=stream_handler)
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions", json={**CHAT_BODY, "stream": True})
    assert r.status_code == 200
    assert b"[DONE]" in r.content
    assert adapter.checkins == ["openai-proxy: gemma4 chat (1 msgs)"]  # still governed
