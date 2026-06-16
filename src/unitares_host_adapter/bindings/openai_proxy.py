"""OpenAI-compatible governance proxy — a transport-level UNITARES binding.

Sits in front of an OpenAI-compatible model server (Ollama's ``/v1`` by
default) and governs *any* client that points at it — Open WebUI, Cursor,
scripts, curl — without per-client wiring. This is the substrate-agnostic
counterpart to the per-host plugin bindings (e.g. Hermes): it binds at the
protocol layer, which is stable, instead of a frontend's plugin API, which is
not.

Design posture — a proxy that ALL local traffic flows through must never make
the user's model worse:

- **Fail-open.** If governance is unreachable or errors, the request is still
  forwarded. Governance never breaks model access.
- **Non-blocking by default** (``mode="observe"``). The governance check-in is
  fired as a post-response background task, so it adds zero latency and the
  client is never held waiting on it. The payoff for chat traffic is
  observability + identity continuity (one governed trajectory of local-model
  use), not enforcement — chat has no tool calls to block.
- **Opt-in enforcement** (``mode="enforce"``). The check-in is awaited *before*
  forwarding; a blocking verdict returns a refusal instead of calling the
  model. Still fail-open on governance error.

Only ``/v1/chat/completions`` is governed; every other path is forwarded
verbatim so the proxy is a drop-in replacement for the upstream base URL.

Built on Starlette (pure passthrough — no request validation needed). Run:
``uhaa-proxy`` (needs the ``proxy`` extra: ``pip install
unitares-host-adapter[proxy]``). Point your client's base URL at it, e.g.
``http://127.0.0.1:11435/v1``.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import httpx

from unitares_host_adapter.core import UnitaresAdapter

# Hop-by-hop headers must not be forwarded across the proxy boundary.
_HOP_BY_HOP = frozenset({
    "host", "content-length", "transfer-encoding", "connection",
    "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "upgrade",
})

DEFAULT_UPSTREAM = "http://localhost:11434"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 11435  # one past Ollama's 11434


def _filter_headers(headers: Any) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _summarize(body: dict[str, Any]) -> str:
    model = body.get("model", "?")
    messages = body.get("messages")
    n = len(messages) if isinstance(messages, list) else 0
    return f"openai-proxy: {model} chat ({n} msgs)"


class GovernanceProxy:
    """Holds the upstream client + one governed identity for the proxy process.

    The governance MCP session has task affinity: the streamable-HTTP transport
    must be opened, used, and closed on ONE task. But HTTP requests each run on
    their own task. So a single long-lived worker task owns the session, connects
    on startup, and services check-in jobs off a queue; request handlers submit
    jobs and await a result future. This confines all session I/O to one task."""

    def __init__(
        self,
        adapter: UnitaresAdapter,
        upstream: httpx.AsyncClient,
        *,
        mode: str = "observe",
        session_label: str = "openai-proxy",
    ) -> None:
        self.adapter = adapter
        self.upstream = upstream
        self.mode = mode
        self.session_label = session_label
        self._started = False
        self._degraded = False  # governance unreachable; keep forwarding
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Spawn the governance worker (which owns and connects the session)."""
        if self._worker is None:
            self._worker = asyncio.create_task(self._run_worker())

    async def stop(self) -> None:
        """Signal the worker to drain and close the session, then await it."""
        if self._worker is not None:
            await self._queue.put(None)  # shutdown sentinel
            await self._worker
            self._worker = None
        await self.upstream.aclose()

    async def _run_worker(self) -> None:
        # Connect on THIS task; all later call_tool + aclose stay on it.
        try:
            await self.adapter.on_session_start(
                self.session_label, purpose="openai governance proxy"
            )
            self._started = True
        except Exception:
            self._degraded = True  # forward un-governed rather than break the user
        try:
            while True:
                job = await self._queue.get()
                if job is None:
                    break
                body, fut = job
                try:
                    result = await self._do_checkin(body)
                except Exception:
                    result = None  # fail-open
                if fut is not None and not fut.done():
                    fut.set_result(result)
        finally:
            transport = getattr(self.adapter, "_transport", None)
            aclose = getattr(transport, "aclose", None)
            if aclose is not None:
                await aclose()

    async def _do_checkin(self, body: dict[str, Any]) -> Optional[str]:
        if self._degraded:
            return None
        verdict = await self.adapter.checkin(
            _summarize(body), response_mode="minimal", complexity=0.1
        )
        if self.mode == "enforce" and verdict.blocks:
            return verdict.message or "UNITARES governance blocked this request."
        return None

    async def checkin(self, body: dict[str, Any]) -> Optional[str]:
        """Submit a check-in to the worker and await its result. Fail-open: if the
        worker is gone or errors, return None (forward un-governed)."""
        if self._worker is None or self._worker.done():
            return None
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        await self._queue.put((body, fut))
        try:
            return await fut
        except Exception:
            return None


def build_app(
    proxy: Optional[GovernanceProxy] = None,
    *,
    upstream_url: Optional[str] = None,
    mode: Optional[str] = None,
):
    """Build the Starlette/FastAPI proxy app.

    Pass an explicit ``proxy`` (with a custom adapter/upstream) for testing;
    otherwise one is built from the environment (UNITARES_MCP_URL /
    UNITARES_PROXY_UPSTREAM / UNITARES_PROXY_MODE)."""
    from contextlib import asynccontextmanager

    # Built on Starlette directly (not FastAPI): the proxy does pure passthrough
    # with no request validation or schema, so FastAPI's layer adds nothing and
    # only couples us to its starlette-version expectations.
    from starlette.applications import Starlette
    from starlette.background import BackgroundTask
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response, StreamingResponse
    from starlette.routing import Route

    if proxy is None:
        from unitares_host_adapter.transport import StreamableHTTPTransport

        up = upstream_url or os.environ.get("UNITARES_PROXY_UPSTREAM", DEFAULT_UPSTREAM)
        proxy = GovernanceProxy(
            UnitaresAdapter(StreamableHTTPTransport.from_env()),
            httpx.AsyncClient(base_url=up, timeout=None),
            mode=mode or os.environ.get("UNITARES_PROXY_MODE", "observe"),
        )

    @asynccontextmanager
    async def lifespan(_app):
        # The worker task owns the governance session (connect/use/close all on
        # one task — the transport has task affinity). Requests submit check-in
        # jobs to it. Fail-open: a governance error degrades to plain forwarding.
        await proxy.start()
        yield
        await proxy.stop()

    async def _forward(request: Request, body_bytes: bytes) -> Response:
        """Transparently forward a request to the upstream, streaming if asked."""
        fwd_headers = _filter_headers(request.headers)
        path = request.url.path
        params = dict(request.query_params)
        is_stream = b'"stream":true' in body_bytes.replace(b" ", b"") or b'"stream": true' in body_bytes

        if is_stream:
            cm = proxy.upstream.stream(
                request.method, path, params=params, headers=fwd_headers, content=body_bytes
            )
            r = await cm.__aenter__()

            async def body_iter():
                try:
                    async for chunk in r.aiter_raw():
                        yield chunk
                finally:
                    await cm.__aexit__(None, None, None)

            return StreamingResponse(
                body_iter(),
                status_code=r.status_code,
                headers=_filter_headers(r.headers),
            )

        r = await proxy.upstream.request(
            request.method, path, params=params, headers=fwd_headers, content=body_bytes
        )
        return Response(
            content=r.content,
            status_code=r.status_code,
            headers=_filter_headers(r.headers),
        )

    async def chat_completions(request: Request) -> Response:
        body_bytes = await request.body()
        try:
            import json
            body = json.loads(body_bytes)
        except (ValueError, TypeError):
            body = {}

        # Enforce mode: gate BEFORE forwarding so a block prevents the call.
        if proxy.mode == "enforce":
            block = await proxy.checkin(body)
            if block is not None:
                return JSONResponse(
                    status_code=200,
                    content={
                        "id": "unitares-governance-block",
                        "object": "chat.completion",
                        "model": body.get("model", "unknown"),
                        "choices": [{
                            "index": 0,
                            "message": {"role": "assistant", "content": f"[UNITARES] {block}"},
                            "finish_reason": "content_filter",
                        }],
                    },
                )
            return await _forward(request, body_bytes)

        # Observe mode (default): forward now, check in afterward (non-blocking).
        response = await _forward(request, body_bytes)
        response.background = BackgroundTask(proxy.checkin, body)
        return response

    async def passthrough(request: Request) -> Response:
        """Everything else (/v1/models, /api/*, embeddings, …) forwarded verbatim."""
        return await _forward(request, await request.body())

    # Order matters: the specific chat route before the catch-all.
    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
            Route("/{path:path}", passthrough, methods=["GET", "POST", "PUT", "DELETE", "PATCH"]),
        ],
    )
    app.state.proxy = proxy
    return app


def main() -> None:
    import uvicorn

    host = os.environ.get("UNITARES_PROXY_HOST", DEFAULT_BIND_HOST)
    port = int(os.environ.get("UNITARES_PROXY_PORT", DEFAULT_BIND_PORT))
    upstream = os.environ.get("UNITARES_PROXY_UPSTREAM", DEFAULT_UPSTREAM)
    mode = os.environ.get("UNITARES_PROXY_MODE", "observe")
    mcp_url = os.environ.get("UNITARES_MCP_URL", "https://gov.cirwel.org/mcp/")

    print(f"UNITARES OpenAI governance proxy  ({mode} mode)")
    print(f"  listening : http://{host}:{port}  ->  point your client's base URL here (/v1)")
    print(f"  upstream  : {upstream}")
    print(f"  governance: {mcp_url}")

    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
