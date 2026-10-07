# Pi driver

Deterministic executor. Python invokes this; it drives a **real pi agent
session** through a scripted sequence of tool calls, with the governance
extension under test attached, and reports what the governance layer did.

No model is in the decision loop, so runs are deterministic and cost nothing.

## Why pi

The binding requirement is that the harness must contribute **no governance
of its own**, or a deny cannot be attributed to the layer under test. pi
meets that on the record rather than by inference — its own security docs
state it has no built-in sandbox and no permission system, and that project
trust "is not a sandbox … only an input-loading guard." Trust resolves at
startup and never sees a tool call.

Claude Code is disqualified for exactly the opposite reason: its classifier
runs *upstream* of `PreToolUse` hooks, so anything it blocks first, the
control layer never sees. Codex has the same problem via its seatbelt
sandbox.

It is also not an ACP-only surface: `pi-dcg` (unaffiliated author) already
bridges **dcg** to the same `tool_call` event, mapping allow/deny/ask onto
pi outcomes. Two independent governance products against one hook shape.

## Requirements

**Node ≥ 22.19.** pi fails to import on Node 20 with
`webidl.util.markAsUncloneable is not a function` (an undici/Node 22 API).
This machine's default is v20.20.0, so the driver must select 22 explicitly:

```sh
eval "$(fnm env)" && fnm use 22
```

Both packages are pinned exactly at **0.84.2**. Do not float them — see the
interception seam below.

## Interception seam

pi exposes `on("tool_call")` returning `{ block?, reason?, terminate? }`, and
`beforeToolCall` is awaited inside `prepareToolCall` — after schema
validation, before dispatch. Nothing runs ahead of it. `undefined` means
allow; there is no native `ask`, so a layer synthesises one by awaiting
`ctx.ui.confirm()` and blocking on refusal.

## Driving it without a model

Two public seams, both documented — **no vendoring of test internals is
required**, contrary to the initial assessment:

1. **`session.agent.streamFunction`** is a public mutable field read per
   prompt, so a scripted stream can be swapped in after construction.
   `@earendil-works/pi-ai` exports `createAssistantMessageEventStream` and
   `fauxAssistantMessage`, which is everything needed to emit a synthetic
   assistant message carrying the tool calls we want.
2. **`pi.registerProvider(name, config)`** (extensions.md §1709) accepts a
   complete provider with custom stream behaviour — the process-isolated
   alternative if in-process construction proves awkward.

Seam 1 is the default. pi-ai being separately published at the same version
is what makes this safe: the stream helpers are a declared dependency with
a version contract, not copied internals that drift.

## What does NOT work as a driver

- **RPC `{"type":"bash"}`** looks like a scripted tool invocation but fires
  `user_bash`, not `tool_call` — different event, different result contract.
  Routing scripted calls through it would silently measure nothing.
- **`packages/evals`** is model-backed and needs a real provider and API key.
  Not deterministic, not free.

## Known limits of this substrate

These are properties of pi, not of the products under test, and they must be
declared in the results rather than scored:

- **No agent-tier concept.** pi exposes only `mode` and `hasUI`. ACP's plugin
  synthesises a tier from an env var — an ACP convention, not a pi contract.
  A competing layer attached to pi sees neither, so any tier-conditional
  scenario structurally advantages ACP. Disclose it or drop the dimension.
- **No native subagents.** The shipped example spawns child `pi` *processes*
  with no parent-session link, so delegation provenance is not observable.
  Inventing a correlation ID would mean scoring a mechanism we authored.
  `delegation_provenance` and `scope_inheritance` are therefore
  substrate-inapplicable here and belong to the framework-native subjects,
  which have real delegation.
- **In-message parallelism is not concurrent at the hook.** Tool calls are
  *prepared* sequentially and only execution overlaps, and `beforeToolCall`
  runs during prepare. Rate-limit cascade scenarios must use N concurrent
  sessions or processes.
- **`user_bash` bypasses `tool_call` entirely** (RPC bash, interactive
  `!cmd`), and ACP's pi plugin does not subscribe to it. pi-dcg's README
  lists the same blind spot independently. Worth its own benchmark row;
  scripted calls must not route through it.
