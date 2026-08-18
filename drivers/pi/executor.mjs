/**
 * AgentGovBench pi executor.
 *
 * Reads a scenario spec as JSON on stdin, drives a REAL pi agent session
 * through the scripted tool calls with the governance extension under test
 * attached, and writes observed outcomes as JSON on stdout.
 *
 * No model is in the decision loop: streamFunction is replaced with a
 * synthetic stream that emits exactly the calls the scenario declares. Runs
 * are deterministic and cost nothing.
 *
 * Ground truth for a decision is EXECUTION, not a parsed event: a tool whose
 * execute() fired was allowed, one that never fired was blocked. That is the
 * observable the product actually controls, and it cannot be faked by the
 * driver — which is the property this whole rebuild exists to get.
 *
 * Input:
 *   {
 *     "extensions": ["/abs/path/to/governance-extension.ts"],
 *     "calls": [ { "id": "c1", "tool": "read_file", "args": {"path": "/a"} } ],
 *     "env": { "ACP_AGENT_TIER": "interactive" }
 *   }
 *
 * Output:
 *   {
 *     "outcomes": [ { "id", "tool", "args", "allowed", "reason" } ],
 *     "errors": [ ... ]
 *   }
 *
 * Run: eval "$(fnm env)" && fnm use 22 && node executor.mjs < spec.json
 */
import { mkdtempSync, mkdirSync, writeFileSync, copyFileSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";

import { createAgentSession, SessionManager, SettingsManager } from "@earendil-works/pi-coding-agent";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";

const FAUX = {
  provider: "agb-faux",
  id: "agb-faux-1",
  name: "AGB faux",
  api: "anthropic-messages",
  reasoning: false,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 200000,
  maxTokens: 8192,
  baseUrl: "http://127.0.0.1:1",
};

async function readStdin() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

const spec = await readStdin();
const errors = [];

// Isolation: a fresh cwd AND agentDir, in-memory session store, settings
// scoped to the temp dir. Ambient ~/.pi config cannot perturb a run and no
// real credential is reachable.
const dir = mkdtempSync(join(tmpdir(), "agb-pi-"));

// Extensions are discovered from <agentDir>/extensions. Copying rather than
// symlinking keeps the run hermetic if the source tree changes mid-suite.
if (spec.extensions?.length) {
  mkdirSync(join(dir, "extensions"), { recursive: true });
  // Copied extensions resolve imports relative to their NEW location, so a
  // node_modules must be reachable from the temp dir. Without this an
  // extension that imports its own SDK fails to load and blocks every
  // call — which is indistinguishable from a policy denial unless the
  // reason is surfaced. That produced a 1/6 for a competitor whose engine
  // was working perfectly.
  try {
    symlinkSync(join(import.meta.dirname, "node_modules"), join(dir, "node_modules"), "dir");
  } catch (e) {
    errors.push(`node_modules link failed: ${e.message}`);
  }
  for (const ext of spec.extensions) {
    try {
      copyFileSync(ext, join(dir, "extensions", basename(ext)));
    } catch (e) {
      errors.push(`extension copy failed for ${ext}: ${e.message}`);
    }
  }
}

// Isolate HOME. Extensions look for operator credentials in ~/.acp and
// friends; the ACP plugin explicitly falls back there when its env var is
// empty. Without this, a run with no credential silently authenticates as
// whoever is sitting at the machine — which is both the wrong measurement
// and a leak of a personal credential into a benchmark.
process.env.HOME = dir;
process.env.USERPROFILE = dir;

for (const [k, v] of Object.entries(spec.env ?? {})) process.env[k] = String(v);

// One custom tool per distinct name the scenario calls. execute() firing IS
// the allow signal; pi's built-ins are disabled so nothing else can run.
const executed = new Map(); // toolCallId -> {tool, params}
const toolNames = [...new Set((spec.calls ?? []).map((c) => c.tool))];

const customTools = toolNames.map((name) => ({
  name,
  label: name,
  description: `AgentGovBench stand-in for ${name}. Records that it ran.`,
  parameters: { type: "object", properties: {}, additionalProperties: true },
  async execute(toolCallId, params) {
    executed.set(toolCallId, { tool: name, params });
    return { output: "ok" };
  },
}));

writeFileSync(
  join(dir, "auth.json"),
  JSON.stringify({ [FAUX.provider]: { type: "api_key", key: "agb-faux-key" } }),
);

// WHY a call was blocked, captured from the subject's own handler return
// value. This was previously an empty Map that nothing ever wrote to, so
// every outcome for every subject reported reason: null — including the
// runs where an integration was silently broken. Without a reason, "the
// extension crashed" and "policy said no" are the same observation, which
// is exactly how a working competitor engine was once scored 1/6.
const blockReasons = new Map();

/** Fatal instrument failures. Any entry here invalidates the whole run. */
const fatal = [];

const { session, extensionsResult } = await createAgentSession({
  cwd: dir,
  agentDir: dir,
  model: FAUX,
  // "builtin" disables pi's own read/write/edit/bash but KEEPS custom tools.
  // "all" would silently disable the scripted tools too and the run would
  // resolve cleanly having executed nothing.
  noTools: "builtin",
  sessionManager: SessionManager.inMemory(),
  settingsManager: SettingsManager.create(dir, dir),
  customTools,
});

// ── Instrument self-check ─────────────────────────────────────────────
//
// Three failure modes were previously indistinguishable from a real
// product verdict, and all three fail SILENTLY:
//
//   1. The extension fails to import. No tool_call handler is registered,
//      every call executes, and the scorecard reads "the product allowed
//      it." Fails OPEN and looks like a permissive product.
//   2. The extension throws inside tool_call. pi's emitToolCall is the one
//      emitter without a try/catch, so the throw propagates and blocks the
//      tool. Fails CLOSED and looks like a paranoid product.
//   3. The handler returns a block with a reason, and the harness discards
//      it, so a crash and a policy denial are the same observation.
//
// Between them, a broken integration could land anywhere from 0 to
// near-perfect with nothing in the output to say so. An instrument that
// cannot detect its own failure is not an instrument.

if (spec.extensions?.length) {
  for (const e of extensionsResult?.errors ?? []) {
    fatal.push(`extension failed to load: ${e.path}: ${e.error}`);
  }

  // A subject was supplied but nothing registered a decision point. Every
  // call would execute unimpeded and score as "allowed by the product".
  const loaded = extensionsResult?.extensions ?? [];
  const withToolCall = loaded.filter(
    (ext) => (ext.handlers?.get("tool_call")?.length ?? 0) > 0,
  );
  if (withToolCall.length === 0) {
    fatal.push(
      `${spec.extensions.length} extension(s) supplied but none registered a ` +
        `tool_call handler — there is no decision point, so every call would ` +
        `execute and be scored as permitted by the product`,
    );
  }

  // Wrap each registered handler to record its verdict. The wrapper is
  // strictly an observer: it forwards the original return value untouched,
  // and a throw is recorded and rethrown so pi's fail-closed behaviour is
  // preserved rather than papered over.
  for (const ext of loaded) {
    const handlers = ext.handlers?.get("tool_call");
    if (!handlers?.length) continue;
    for (let i = 0; i < handlers.length; i++) {
      const original = handlers[i];
      handlers[i] = async (event, ...rest) => {
        const id = event?.toolCallId ?? event?.id;
        try {
          const result = await original(event, ...rest);
          if (result?.block) {
            blockReasons.set(
              id,
              result.reason ?? "blocked without a stated reason",
            );
          }
          return result;
        } catch (err) {
          const msg = String(err?.message ?? err).split("\n")[0];
          // Distinguishes a crash from a policy denial in the output, and
          // records it as an instrument failure rather than a verdict.
          blockReasons.set(id, `EXTENSION THREW: ${msg}`);
          fatal.push(`extension threw in tool_call for ${id}: ${msg}`);
          throw err;
        }
      };
    }
  }
}

// The faux provider must exist in the registry BEFORE auth resolution, or
// lookup fails for a provider it has never heard of. A key alone is not enough.
const rt = session.modelRuntime;
await rt.registerProvider(FAUX.provider, {
  baseUrl: FAUX.baseUrl,
  api: FAUX.api,
  models: [{ ...FAUX }],
});
await rt.setRuntimeApiKey(FAUX.provider, "agb-faux-key");

// Turn 1 issues every scripted call; turn 2 stops. A stream that always
// returns the same toolUse response loops forever and re-executes the tools
// on every pass.
let turn = 0;
session.agent.streamFunction = () => {
  const stream = createAssistantMessageEventStream();
  const opening = { role: "assistant", content: [], stopReason: "pending" };

  if (turn++ > 0) {
    queueMicrotask(() => {
      stream.push({ type: "start", partial: { ...opening } });
      stream.push({
        type: "done",
        reason: "stop",
        message: { role: "assistant", content: [{ type: "text", text: "done" }], stopReason: "stop" },
      });
    });
    return stream;
  }

  queueMicrotask(() => {
    const partial = { ...opening, content: [] };
    stream.push({ type: "start", partial: { ...partial } });
    (spec.calls ?? []).forEach((c, i) => {
      const block = { type: "toolCall", id: c.id, name: c.tool, arguments: c.args ?? {} };
      partial.content = [...partial.content, { type: "toolCall", id: c.id, name: c.tool, arguments: {} }];
      stream.push({ type: "toolcall_start", contentIndex: i, partial: { ...partial } });
      partial.content[i].arguments = c.args ?? {};
      stream.push({ type: "toolcall_end", contentIndex: i, toolCall: block, partial: { ...partial } });
    });
    stream.push({
      type: "done",
      reason: "toolUse",
      message: {
        role: "assistant",
        content: (spec.calls ?? []).map((c) => ({
          type: "toolCall", id: c.id, name: c.tool, arguments: c.args ?? {},
        })),
        stopReason: "toolUse",
      },
    });
  });
  return stream;
};

session.agent.onToolResult = session.agent.onToolResult ?? null;
try {
  await session.prompt(spec.prompt ?? "run the scripted calls");
} catch (e) {
  errors.push(`prompt: ${String(e).split("\n")[0]}`);
}

const outcomes = (spec.calls ?? []).map((c) => ({
  id: c.id,
  tool: c.tool,
  args: c.args ?? {},
  allowed: executed.has(c.id),
  // Why it was blocked, straight from the layer under test. Without this,
  // "the extension crashed" and "policy said no" are the same observation.
  reason: blockReasons.get(c.id) ?? null,
}));

process.stdout.write(
  JSON.stringify({ outcomes, errors, fatal }, null, 2) + "\n",
);
process.exit(0);
