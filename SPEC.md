# UNITARES Delivery Surface Spec

**Version:** 0.1 (draft)
**Status:** Pre-release. Signatures may change before 1.0.

This spec defines how host bindings connect agent lifecycle checkpoints to UNITARES, self-hosted accountability infrastructure for long-running AI agents. The server connects process identities, claims, evidence, governed reviews, and outcomes in an operator-owned record. A binding maps supported host hooks to selected server operations.

## Server and adapter responsibilities

The server owns the durable record and returns runtime policy actions, reasons, and next steps. Its [EISV proprioception model](https://github.com/cirwel/unitares/blob/master/docs/ontology/eisv-proprioception-contract.md) makes changes in an agent process visible for diagnosis and action with evidence. A state estimate or policy action does not establish that a task succeeded; outcomes need their own supporting observations.

The adapter supplies lifecycle reporting and optional ways to deliver policy responses to the host or agent. It does not automatically publish claims and evidence, conduct reviews, or reconstruct earlier work. See the [UNITARES README](https://github.com/cirwel/unitares#readme) for the broader product and [evidence and limits](https://github.com/cirwel/unitares/blob/master/docs/EVIDENCE_AND_LIMITS.md) for the status of its claims.

## Library delivery modes

The library supports three ways to request or deliver a server policy response. Default automatic turn reporting is described separately under [Session lifecycle](#session-lifecycle); it does not inject a response into the conversation.

### 1. Explicit

The agent initiates. It calls `sync_state`, `search_shared_memory`, `request_review`, or another server tool via MCP and receives a response.

- **Trigger:** agent-initiated MCP tool call.
- **Depth control:** the `response_mode` parameter on the UNITARES MCP server: `minimal` / `compact` / `standard` / `full` / `auto`.
- **Host requirements:** An MCP client configured for the operator's server.
- **Semantics:** agent sees only what it asks for. No surprise annotations.

### 2. Ambient

The host injects governance state into tool results the agent did not explicitly request.

- **Trigger:** host-side hook on tool-result delivery, fired on every tool call (or a filtered subset).
- **Depth control:** same `response_mode` parameter, default `lite` / `minimal`. Ambient mode SHOULD default to shallow depth to avoid flooding the agent with state it did not ask for.
- **Host requirements:** a `transform_tool_result` (or equivalent) lifecycle hook on the host side that can mutate tool output before the agent reads it.
- **Semantics:** the agent sees a policy annotation alongside the tool result. Annotation content and frequency depend on the binding and response depth.

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
| `proceed` | Returns policy response at requested depth | Annotation when margin is present | No block |
| `guide` | Returns verdict + guidance | Guidance appended to result | No block (guide is advisory) |
| `pause` | Returns verdict + reason | Pause annotation appended | **Blocks** with `pause` message |
| `reject` | Returns verdict + reason | Reject annotation appended if result delivery runs | **Blocks** with `reject` message |

Bindings MAY implement a subset, declared according to the host hooks they support. Gated and ambient calls each submit a check-in; they are not read-only state inspections. The current Hermes binding is fail-open on server or transport errors, while typed `AGENT_PAUSED` refusals remain blocking.

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
| `onboard` or `start_session` | session-start / first turn | Mint governance identity for the new agent session |
| `record_result` | opt-in post-tool-call | Report host-observed success/failure; associate a prediction ID when grading a prediction |
| (none — local close in Hermes 0.3.x) | session finalize / reset | Clear local session binding and failure state |

Hermes 0.3.x defaults to automatic turn reporting: one fresh identity for each active host session and one `sync_state` marker per completed turn. This is host observation, not ambient delivery: the returned verdict is not injected into the conversation. Tool gating, result annotations, and outcome reporting are disabled unless explicitly registered by a custom plugin. Default session close is local only; persisted resume/compaction/subagent lineage and server presence release are not implemented in 0.3.x. See the [Hermes setup guide](./README.md#hermes-agent) for the shipped behavior, disclosure, and hook declarations.

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
- `unitares_host_adapter.bindings.openai_proxy` — transport-level binding for OpenAI-compatible clients (`uhaa-proxy`).

Claude Code and Codex lifecycle hooks are provided by the separate [`unitares-governance`](https://github.com/cirwel/unitares-governance-plugin) plugin; any MCP-capable host can use explicit mode with no binding.
