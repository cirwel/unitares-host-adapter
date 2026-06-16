# unitares-host-adapter

Thin client bindings that mount [UNITARES](https://github.com/CIRWEL/unitares) governance into AI-agent hosts — Hermes Agent, Claude Code, Goose, and any MCP-capable agent.

One library. Three delivery modes. Multiple host bindings. Your agent host now carries governance.

## What this is

UNITARES governance (EISV state vectors, verdicts, coherence, calibration, a shared knowledge graph) is served as an MCP server at `gov.cirwel.org/mcp/`. This package is the client-side adapter that wires the UNITARES MCP surface into the lifecycle hooks of common agent hosts, so an agent running on any of them gets governance without writing custom plugin code.

See [`SPEC.md`](./SPEC.md) for the full delivery-surface specification.

## What this is NOT

- Not an agent host (no TUI, no gateway, no runtime).
- Not a replacement for the UNITARES governance MCP server.
- Not where the governance primitive lives — just where the host bindings live.

## Quick start

```bash
pip install unitares-host-adapter
```

### Hermes Agent

Create a normal Hermes plugin directory and delegate to the host binding:

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

Set `UNITARES_MCP_URL=http://localhost:8767/mcp/` for a local governance server,
or leave it unset to use the packaged default. Enable the plugin in Hermes config
(`plugins.enabled: [unitares]`) and restart Hermes so plugin discovery reruns.

Opt-in per-tool modes are available when you explicitly want them:

```python
register_unitares(ctx, enable_gate=True, enable_ambient=True, enable_outcomes=True)
```

### Claude Code

```bash
uhaa install claude-code --mcp-url https://gov.cirwel.org/mcp/
```

Emits the appropriate hook entries into your `.claude/settings.json`.

### Any MCP-capable host (explicit-only fallback)

Add the UNITARES server to your host's MCP config with the URL above. No adapter needed for explicit mode; install this package only if you want ambient or gated delivery.

## The three delivery modes

| Mode | Who initiates | When it fires | Default depth |
|---|---|---|---|
| **Explicit** | Agent (via MCP tool call) | When the agent asks | `auto` |
| **Ambient** | Host (tool-result hook) | Every tool call | `lite` |
| **Gated** | Host (pre-tool-call hook) | Before any tool call | N/A (block or pass) |

See [`SPEC.md`](./SPEC.md) for the full treatment.

## Status

**v0.2 — alpha.** Signatures may change before 1.0.

Bindings are landing in this order:

- [x] Spec draft
- [x] Core `UnitaresAdapter` class
- [x] Concrete streamable-HTTP MCP transport
- [x] Hermes binding: lazy first-turn onboard + turn-level check-in
- [x] Hermes opt-in gated / ambient / outcome hooks
- [ ] Claude Code binding
- [ ] Goose binding
- [ ] Generic MCP fallback
- [ ] PyPI publish

## License

Apache License 2.0. See [`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE).
