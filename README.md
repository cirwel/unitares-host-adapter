# unitares-host-adapter

Thin client bindings that mount [UNITARES](https://github.com/cirwel/unitares) governance into AI-agent hosts — Hermes Agent, Claude Code, Goose, and any MCP-capable agent.

One library. Three delivery modes. Multiple host bindings. Your agent host now carries governance.

## What this is

UNITARES governance (EISV state vectors, verdicts, coherence, calibration, a shared knowledge graph) is served by a UNITARES MCP server that you run (see [`cirwel/unitares`](https://github.com/cirwel/unitares); a local install listens on `http://127.0.0.1:8767/mcp/`). This package is the host-adapter library that wires the UNITARES MCP surface into the lifecycle hooks of common agent hosts, so an agent running on any of them gets governance without each host reimplementing governance logic.

See [`SPEC.md`](./SPEC.md) for the full delivery-surface specification.

## What this is NOT

- Not an agent host (no TUI, no gateway, no runtime).
- Not a replacement for the UNITARES governance MCP server.
- Not where the governance primitive lives — just where the host bindings live.
- Not the `unitares-governance-plugin` repo; that repo packages Claude/Codex-facing guidance, hooks, and sidecar tooling.

## Terminology: adapter vs plugin vs MCP

These names are easy to blur, especially for Hermes:

| Name | Lives where | Role |
|---|---|---|
| UNITARES governance server | `cirwel/unitares` / MCP endpoint | Source of truth for identities, EISV, verdicts, calibration, dialectic, and KG. |
| Direct MCP config | Host config, e.g. `mcp_servers.unitares` in Hermes | Makes UNITARES tools callable. By itself it does not add lifecycle hooks or automatic check-ins. |
| Host adapter library | This repo, `unitares-host-adapter` | Reusable bindings that connect host lifecycle hooks to UNITARES. |
| Hermes user plugin | `~/.hermes/plugins/unitares` | Thin Hermes runtime entrypoint that imports `unitares_host_adapter.bindings.hermes`. |
| Governance plugin repo | `unitares-governance-plugin` | Claude/Codex plugin packaging, shared skills, command guidance, and sidecar workflows. It is not the Hermes-native lifecycle adapter. |

## Quick start

The package is not on PyPI yet; install it from GitHub:

```bash
pip install "git+https://github.com/cirwel/unitares-host-adapter"
```

### Hermes Agent

This repository is itself a Hermes directory plugin (`plugin.yaml` and `__init__.py` at the root), so Hermes can install it directly:

```bash
hermes plugins install cirwel/unitares-host-adapter
```

Hermes asks for `UNITARES_MCP_URL`, the Streamable HTTP endpoint of the UNITARES server you run (for example `http://127.0.0.1:8767/mcp/`, trailing slash included), and will not load the plugin without it; there is no default server. Set `UNITARES_BEARER` too if your server requires a token. Enabling the plugin asks for consent to build this package and its two dependencies (`mcp`, `httpx`) into the Hermes environment.

The installed plugin runs the binding's default light mode: one onboard at the first turn of each Hermes session, one check-in per completed turn, and session close on finalize or reset. Every hook is fail-open: a server error is logged and never blocks the agent.

To wire it by hand instead, or to opt into the per-tool modes, use a normal Hermes user plugin. The plugin directory is the runtime entrypoint; this repo supplies the binding logic. Create a normal Hermes plugin directory and delegate to the host binding:

```yaml
# ~/.hermes/plugins/unitares/plugin.yaml
name: unitares
version: 0.2.0
description: UNITARES governance lifecycle adapter for Hermes
provides_hooks:
  - pre_llm_call
  - post_llm_call
```

```python
# ~/.hermes/plugins/unitares/__init__.py
from unitares_host_adapter.bindings.hermes import register as register_unitares

def register(ctx):
    # Default: lazy onboard on the first turn, then one check-in per completed
    # Hermes turn. Per-tool gate/ambient/outcome traffic stays opt-in.
    register_unitares(ctx)
```

Set `UNITARES_MCP_URL` to your server's endpoint, for example
`http://localhost:8767/mcp/` for a local server. As with the installed plugin
above, the library has no default server: with the variable unset, the hooks
log a warning and stay inactive instead of reporting anywhere. Enable the plugin in Hermes
config (`plugins.enabled: [unitares]`) and restart Hermes so plugin discovery
reruns.

Opt-in per-tool modes are available when you explicitly want them:

```python
register_unitares(ctx, enable_gate=True, enable_ambient=True, enable_outcomes=True)
```

### Claude Code

There is no `uhaa` installer for Claude Code yet; `uhaa` itself only has
`--version` and `spec`. Claude Code lifecycle hooks come from the
[`unitares-governance`](https://github.com/cirwel/unitares-governance-plugin)
plugin:

```text
/plugin marketplace add cirwel/unitares-governance-plugin
/plugin install unitares-governance@unitares-governance
```

### Any MCP-capable host (explicit-only fallback)

Add your UNITARES server's MCP endpoint (for example `http://127.0.0.1:8767/mcp/`) to your host's MCP config. No adapter needed for explicit mode; install this package only if you want ambient or gated delivery.

This path is **voluntary** — the agent *may* call governance if it chooses. The proxy below is how you make any OpenAI/MCP client *carry* governance instead.

### OpenAI-compatible clients — Ollama, Open WebUI, Cursor (governance proxy)

For clients that speak the OpenAI API but expose no lifecycle hooks, run the governance proxy in front of the model server and point the client's base URL at it:

```bash
pip install "unitares-host-adapter[proxy] @ git+https://github.com/cirwel/unitares-host-adapter"
UNITARES_MCP_URL=http://127.0.0.1:8767/mcp/ \
UNITARES_PROXY_UPSTREAM=http://localhost:11434 \
uhaa-proxy            # listens on http://127.0.0.1:11435  -> point your client at /v1
```

Every request through the proxy is governed under one identity, non-optionally — unlike exposing MCP tools and hoping the model calls them. It is **fail-open** (governance errors never break the model) and **non-blocking by default** (`UNITARES_PROXY_MODE=observe`); set `enforce` to gate. This is the substrate-agnostic *floor*: identity + check-ins + observability for any client. True gated tool-call enforcement is the *ceiling*, delivered by the per-host bindings above where the host exposes a pre-tool-call hook (the proxy returns the model's response before the client executes a tool, so it can observe but not intercept execution).

## The three delivery modes

| Mode | Who initiates | When it fires | Default depth |
|---|---|---|---|
| **Explicit** | Agent (via MCP tool call) | When the agent asks | `auto` |
| **Ambient** | Host (tool-result hook) | Every tool call | `lite` |
| **Gated** | Host (pre-tool-call hook) | Before any tool call | N/A (block or pass) |

See [`SPEC.md`](./SPEC.md) for the full treatment.

## Status

**v0.3 — alpha.** Signatures may change before 1.0.

Bindings are landing in this order:

- [x] Spec draft
- [x] Core `UnitaresAdapter` class
- [x] Concrete streamable-HTTP MCP transport
- [x] Hermes binding: lazy first-turn onboard + turn-level check-in
- [x] Hermes opt-in gated / ambient / outcome hooks
- [x] Hermes directory plugin (`hermes plugins install cirwel/unitares-host-adapter`)
- [x] OpenAI-compatible governance proxy (transport-level binding; any client)
- [ ] Claude Code binding
- [ ] Goose binding
- [ ] Generic MCP fallback
- [ ] PyPI publish

## License

Apache License 2.0. See [`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE).
