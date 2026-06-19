# UNITARES Delivery Surface Spec

**Version:** 0.1 (draft)
**Status:** Pre-release. Signatures may change before 1.0.

This spec defines how UNITARES governance is delivered into an AI-agent host. It is host-agnostic: any host that implements three named integration points can carry UNITARES governance with no custom code beyond a binding.

## Primitive: digital proprioception

UNITARES treats agent self-state (energy, information integrity, entropy, void — the EISV vector), verdicts (`proceed` / `guide` / `pause` / `reject`), coherence, and calibration as **proprioceptive signals**: information an agent consumes about itself so it can adjust behavior. This primitive is owned by UNITARES and served via the governance MCP. This spec defines only the delivery surface; the primitive itself is documented in the UNITARES paper and governance-fundamentals skill.

## Three delivery modes

UNITARES proprioception reaches an agent through exactly three modes, named and depth-controlled here:

### 1. Explicit

The agent initiates. It calls `checkin`, `knowledge`, `dialectic`, or a similar tool via MCP and receives a response.

- **Trigger:** agent-initiated MCP tool call.
- **Depth control:** the `response_mode` parameter on the UNITARES MCP server: `minimal` / `compact` / `standard` / `full` / `auto`.
- **Host requirements:** MCP client support. Every agentskills.io-compatible host with MCP has this today.
- **Semantics:** agent sees only what it asks for. No surprise annotations.

### 2. Ambient

The host injects governance state into tool results the agent did not explicitly request.

- **Trigger:** host-side hook on tool-result delivery, fired on every tool call (or a filtered subset).
- **Depth control:** same `response_mode` parameter, default `lite` / `minimal`. Ambient mode SHOULD default to shallow depth to avoid flooding the agent with state it did not ask for.
- **Host requirements:** a `transform_tool_result` (or equivalent) lifecycle hook on the host side that can mutate tool output before the agent reads it.
- **Semantics:** agent consumes its own EISV/verdict as context on *every* tool result, making governance feel continuous rather than interrogative. This is the surface digital proprioception is most naturally carried on.

### 3. Gated

The host refuses a tool call when UNITARES returns a blocking verdict.

- **Trigger:** host-side hook on tool-call preparation (`pre_tool_call` or equivalent), fired before the tool executes.
- **Depth control:** not applicable. The return shape is fixed:
  ```json
  {"action": "block", "message": "<human-readable reason>"}
  ```
  Returning null / nothing means proceed.
- **Host requirements:** a `pre_tool_call` lifecycle hook that can veto a tool call.
- **Semantics:** verdicts `pause` and `reject` translate to block directives; `proceed` and `guide` do not block (guide may route to ambient annotation instead).

## Mode-to-verdict mapping

| UNITARES verdict | Explicit response | Ambient annotation | Gated block |
|---|---|---|---|
| `proceed` | Returns full verdict object | Lite annotation appended | No block |
| `guide` | Returns verdict + guidance | Guidance prepended to result | No block (guide is advisory) |
| `pause` | Returns verdict + reason | Warning prepended | **Blocks** with `pause` message |
| `reject` | Returns verdict + reason | N/A (call is blocked before result) | **Blocks** with `reject` message |

Hosts SHOULD implement all three modes if their lifecycle hooks permit it. Hosts MAY implement a subset; partial implementations are valid and should be declared in the host binding.

## Binding responsibilities

A host binding is the glue between UNITARES delivery modes and a specific host's hook taxonomy. Each binding must:

1. Map the three delivery modes onto the host's available lifecycle hooks.
2. Declare which modes are implemented (full / partial / explicit-only).
3. Provide sane defaults for `response_mode` per delivery mode (ambient default: `lite`; explicit default: `auto`).
4. Expose a one-line install path: the user should not need to read UNITARES internals to wire the binding.

## Naming boundary

This spec uses **binding** or **host adapter** for code in this package. Some hosts load that binding through a host-specific plugin mechanism. For example, Hermes Agent loads `unitares_host_adapter.bindings.hermes` through a thin user plugin at `~/.hermes/plugins/unitares`; the Hermes plugin is the entrypoint, while this package remains the adapter implementation.

Direct MCP configuration is a separate surface: it makes UNITARES tools visible to the host, but it does not create automatic session lifecycle hooks unless the host adapter/plugin layer also runs.

## Session lifecycle

Bindings SHOULD also wire these lifecycle events when the host exposes them:

| UNITARES call | Host hook | Purpose |
|---|---|---|
| `onboard` | session-start | Mint governance identity for the new agent session |
| `outcome_event` | post-tool-call | Feed calibration ground truth |
| (none — local close) | session-end | Optional final check-in |

## Non-goals

This spec does NOT:

- Define how UNITARES internally computes EISV, verdicts, coherence, or calibration. That is the governance MCP's contract, documented elsewhere.
- Prescribe a wire protocol beyond MCP. Hosts MUST use MCP to reach the UNITARES governance server.
- Require any host to implement all three modes. Explicit-only is a valid implementation.
- Cover multi-agent coordination, dialectic sessions, or knowledge-graph contribution. Those are orthogonal MCP tools on the governance server.

## Versioning

This spec follows semver at the protocol level:

- **MAJOR:** breaking changes to delivery mode names, block-directive shape, or verdict semantics.
- **MINOR:** new optional delivery modes or lifecycle hooks.
- **PATCH:** documentation and clarification.

Bindings SHOULD pin to a MAJOR version of the spec.

## Reference implementations

- `unitares_host_adapter.bindings.hermes` — Hermes Agent lifecycle binding, loaded by a thin Hermes user plugin.
- `unitares_host_adapter.bindings.claude_code` — Claude Code hooks via settings.json.
- `unitares_host_adapter.bindings.goose` — Goose extension.
- `unitares_host_adapter.bindings.generic_mcp` — explicit-only fallback for any MCP-capable host.
