# unitares-host-adapter

Connect [UNITARES](https://github.com/cirwel/unitares) to Hermes Agent: record a governance identity for each session and an automatic check-in after each completed turn on a server you run. This repository is both the **Hermes plugin** and the Python library behind it. It also includes a [proxy for OpenAI-compatible clients](#openai-compatible-clients--ollama-open-webui-cursor-governance-proxy).

The default Hermes plugin observes session activity. It does not inject verdicts into the conversation or block tools. Agent-visible governance and tool gating are separate, opt-in integrations.

## Hermes Agent

Coming from the [Hermes plugin catalog](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/unitares.yaml)? This is the plugin's setup guide. The catalog installs a reviewed commit, which can be older than this repository's latest version. The steps below apply to the catalog's 0.3.1 plugin and this repository's 0.3.3 release.

### Install and enable

You need **Hermes Agent 0.21.0 or later** and a reachable UNITARES Streamable HTTP MCP server. The plugin does not start a server for you. If you need one, follow the [UNITARES Docker install](https://github.com/cirwel/unitares#install); its default local endpoint is `http://127.0.0.1:8767/mcp/`.

Install from the catalog UI, or install this repository directly:

```bash
hermes plugins install cirwel/unitares-host-adapter
```

When prompted, set `UNITARES_MCP_URL` to **your server's** endpoint, including `/mcp/` and its trailing slash. Accept the package dependency installation when Hermes asks; it builds this library and installs `mcp` and `httpx` into the Hermes environment. A separate `pip install` is unnecessary for the directory plugin.

If you skipped configuration, set these values in your active Hermes profile's `.env` file (normally `~/.hermes/.env`):

```dotenv
UNITARES_MCP_URL=http://127.0.0.1:8767/mcp/
# Only needed when your server requires a token:
# UNITARES_BEARER=your-server-token
```

There is no default server. Hermes checks the required URL before loading the plugin; the library also refuses to send anything while it is unset. The default loopback Docker server needs no bearer token. For a protected server, use a token accepted by its `UNITARES_MCP_BEARER_TOKENS` setting.

Enable the plugin if you did not enable it during installation, then check its status:

```bash
hermes plugins enable unitares
hermes plugins list
```

The plugin name is **`unitares`**, even though the repository and Python package are named `unitares-host-adapter`. Restart your Hermes CLI process after enabling it. If you use the gateway, run `hermes gateway restart`.

For a server on another machine or in another container, `127.0.0.1` refers to the machine/container running Hermes. Use an address Hermes can reach and configure the server's allowed hosts and authentication as described in the [MCP client guide](https://github.com/cirwel/unitares/blob/master/docs/integration/MCP_CLIENTS.md).

### Verify it is working

Start a Hermes conversation and complete one assistant turn. On your UNITARES dashboard, look for a new identity labelled `Hermes Agent` and a check-in with the marker `Hermes assistant turn completed`. The plugin registers session-start and first-turn hooks, so onboarding can happen at session start or lazily on the first turn. Later completed turns use the same governance binding while that Hermes session stays active.

Installing this plugin does not add agent-callable tools or a chat command. It reports automatically; a successful install alone does not prove the server is reachable.

### What the default plugin sends

| Event | Sent to your configured UNITARES server |
|---|---|
| New active session | A fresh identity request with label `Hermes Agent`, model type `hermes-agent`, and a client hint containing the Hermes platform and model name. |
| Completed assistant turn | The fixed marker `Hermes assistant turn completed`, fixed complexity `0.2`, and metadata identifying the report as a host observation (`substrate_interpretation`). The server-issued session binding accompanies the check-in. |
| Session finalize or reset | Local adapter state is cleared. In 0.3.x this does not send a server-side close or presence-release call. |

Default hooks send **no user, assistant, or tool text, tool names, arguments, or results**. They do not send per-tool outcomes or turn timing/count metrics. These automatic markers describe host activity; they are not agent-authored reflections or independent evidence that a task succeeded.

Version 0.3.x does not persist a session-to-identity map across process restarts or declare compaction, resume, or subagent lineage. A newly onboarded session receives a fresh identity.

Verdicts are recorded on the server, but the default plugin does not show them to the agent or enforce them. Hook failures are fail-open: Hermes continues after a governance error. Hooks run synchronously, so an unresponsive server can still delay a call by roughly 40 seconds (connection plus call timeout). After three consecutive failures, the adapter stops retrying for that session until finalize/reset clears its failure state.

### Troubleshooting

Check `hermes plugins list` and warnings in your active profile's `logs/` directory (normally `~/.hermes/logs/errors.log`).

| Symptom | What to check |
|---|---|
| Plugin installed but inactive | Enable `unitares`, accept dependency installation, and restart the CLI or gateway. |
| Missing `UNITARES_MCP_URL` | Set it in the active profile's `.env`; restart Hermes so the process sees the change. There is no fallback endpoint. |
| No identity or check-ins | Complete a turn, check the URL and server availability, and inspect hook warnings. Confirm the dashboard is connected to the same server. |
| HTTP 421 / refused Host | Add the hostname and port used in the client URL to the **server's** `UNITARES_MCP_ALLOWED_HOSTS`, then restart the server. Version 0.3.2 names this setting in its warning; older catalog pins may only log an exception type. |
| Authentication failure | Set `UNITARES_BEARER` to a token accepted by your server and restart Hermes. |
| Hook warnings followed by silence | Three consecutive failures open the session's circuit breaker. Fix the cause, then reset the session or restart Hermes. |
| Public tool contract incompatibility | The server must advertise `sync_state` and either `onboard` or `start_session`. Check the server version and advertised tool configuration. |

### Let the agent call UNITARES tools

Automatic lifecycle reporting and direct MCP access are independent. If you want the agent to inspect state, write findings, or request reviews itself, also add your server to Hermes's MCP configuration:

```yaml
# ~/.hermes/config.yaml (use your active profile's config)
mcp_servers:
  unitares:
    url: http://127.0.0.1:8767/mcp/
    tools:
      resources: false
      prompts: false
```

This example is for the default local server without authentication. For bearer headers and remote servers, use the [MCP client guide](https://github.com/cirwel/unitares/blob/master/docs/integration/MCP_CLIENTS.md). Restart Hermes after changing the configuration. Direct MCP access makes tools available; the agent decides when to call them.

### Advanced: manual binding and per-tool modes

For a custom Hermes plugin, install the library into the Python environment Hermes uses:

```bash
pip install "git+https://github.com/cirwel/unitares-host-adapter"
```

Create a plugin that delegates to the binding. Use this as an alternative to the repository plugin so you do not register duplicate lifecycle hooks:

```yaml
# ~/.hermes/plugins/unitares/plugin.yaml
manifest_version: 1
name: unitares
kind: standalone
version: 0.3.3
description: UNITARES governance lifecycle adapter for Hermes
provides_hooks:
  - pre_llm_call
  - post_llm_call
  - on_session_start
  - on_session_finalize
  - on_session_reset
```

```python
# ~/.hermes/plugins/unitares/__init__.py
from unitares_host_adapter.bindings.hermes import register as register_unitares

def register(ctx):
    register_unitares(ctx)
```

Set `UNITARES_MCP_URL` and optional `UNITARES_BEARER` as above, enable `unitares`, and restart Hermes.

Per-tool modes require explicit registration **and matching hook declarations** in the custom plugin manifest:

| Registration option | Add to `provides_hooks` | Behavior |
|---|---|---|
| `enable_gate=True` | `pre_tool_call` | Ask governance before a tool call; block on a blocking verdict. Errors remain fail-open. |
| `enable_ambient=True` | `transform_tool_result` | Append governance annotations to tool results the agent sees. |
| `enable_outcomes=True` | `post_tool_call` | Send tool success/failure observations. |

For example, after adding all three hooks to the manifest:

```python
register_unitares(ctx, enable_gate=True, enable_ambient=True, enable_outcomes=True)
```

The default privacy disclosure above applies to light mode. Per-tool modes send additional context: gate and ambient check-ins include tool names and argument previews; outcomes include tool names and status/error metadata. Review these modes before enabling them for your workload.

## Other hosts

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

## Library delivery modes

| Mode | Who initiates | When it fires | Default depth |
|---|---|---|---|
| **Explicit** | Agent (via MCP tool call) | When the agent asks | `auto` |
| **Ambient** | Host (tool-result hook) | Every tool call | `lite` |
| **Gated** | Host (pre-tool-call hook) | Before any tool call | N/A (block or pass) |

These are library capabilities. The default Hermes plugin uses automatic turn reporting, with ambient, gated, and outcome hooks disabled. See [`SPEC.md`](./SPEC.md) for the delivery-surface contract.

## Repository map

| Path | Purpose |
|---|---|
| [`plugin.yaml`](./plugin.yaml), [`__init__.py`](./__init__.py) | Installable Hermes directory plugin and runtime entrypoint. |
| [`src/unitares_host_adapter/bindings/hermes.py`](./src/unitares_host_adapter/bindings/hermes.py) | Hermes lifecycle hooks and opt-in tool modes. |
| [`src/unitares_host_adapter/bindings/openai_proxy.py`](./src/unitares_host_adapter/bindings/openai_proxy.py) | OpenAI-compatible governance proxy. |
| [`SPEC.md`](./SPEC.md) | Host-agnostic delivery-surface specification. |
| [`tests/`](./tests/) | Core, transport, binding, and plugin contract tests. |

The [UNITARES server](https://github.com/cirwel/unitares) owns identities, state estimation, verdicts, reviews, and shared knowledge. The separate [governance plugin](https://github.com/cirwel/unitares-governance-plugin) packages Claude/Codex hooks, skills, and guidance. This repository supplies host bindings and requires a server for governance operations.

## Status

**v0.3.3 — alpha.** Signatures may change before 1.0.

Version 0.3.3 reads current UNITARES `sync_state` decision envelopes (`action_summary` and `state_summary`) as well as older canonical verdict responses. Opt-in gates respect the final policy action; an advisory cold-start pause deferred by policy does not block a tool. Guided decisions remain visible in ambient annotations. Contract fixtures were generated by UNITARES's actual envelope builder at commit [`f5cb44268`](https://github.com/cirwel/unitares/commit/f5cb44268e63247e31ec99ea7dca816e931a8e91), covering minimal, compact, standard, mirror, and full responses. This validates response compatibility; it is not an end-to-end Hermes deployment test.

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
