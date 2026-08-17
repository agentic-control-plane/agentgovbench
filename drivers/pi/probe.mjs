/**
 * Minimal proof that pi can be driven deterministically.
 *
 * One custom tool, one scripted tool call, no model. If the tool's execute()
 * fires, the whole approach works and the executor is mechanical from here.
 *
 * Run: eval "$(fnm env)" && fnm use 22 && node probe.mjs
 */
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createAgentSession, SessionManager, SettingsManager } from "@earendil-works/pi-coding-agent";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";

const dir = mkdtempSync(join(tmpdir(), "agb-pi-"));

// A model that is never called — streamFunction is replaced before any prompt.
const fauxModel = {
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

// prompt() resolves auth before it ever reaches streamFunction, so the faux
// provider needs a credential on disk. agentDir is a fresh temp dir, so this
// cannot touch real credentials.
writeFileSync(
  join(dir, "auth.json"),
  JSON.stringify({ [fauxModel.provider]: { type: "api_key", key: "agb-faux-key" } }),
);

const executed = [];

const { session } = await createAgentSession({
  cwd: dir,
  agentDir: dir,               // isolate from ~/.pi so ambient config can't perturb the run
  model: fauxModel,
  noTools: "builtin",   // disable pi's built-ins but KEEP our scripted custom tools
  sessionManager: SessionManager.inMemory(),
  settingsManager: SettingsManager.create(dir, dir),
  customTools: [
    {
      name: "read_file",
      label: "Read file",
      description: "Benchmark stand-in. Records that it ran.",
      parameters: {
        type: "object",
        properties: { path: { type: "string" } },
        required: ["path"],
      },
      async execute(args) {
        executed.push({ tool: "read_file", args });
        return { output: "ok" };
      },
    },
  ],
});

// prompt() resolves a credential before it reaches streamFunction. Register a
// throwaway one at runtime for the faux provider — the model is never called.
// The faux provider has to exist in the registry before auth resolution runs,
// or lookup fails with "No API key found" for a provider it has never heard of.
const rt = session.modelRuntime;
await rt.registerProvider(fauxModel.provider, {
  baseUrl: fauxModel.baseUrl,
  api: fauxModel.api,
  models: [{
    id: fauxModel.id, name: fauxModel.name, api: fauxModel.api,
    reasoning: fauxModel.reasoning, input: fauxModel.input, cost: fauxModel.cost,
    contextWindow: fauxModel.contextWindow, maxTokens: fauxModel.maxTokens,
    baseUrl: fauxModel.baseUrl,
  }],
});
await rt.setRuntimeApiKey(fauxModel.provider, "agb-faux-key");

// Emit one synthetic assistant message carrying a scripted tool call.
const scripted = [{ id: "call-1", name: "read_file", arguments: { path: "/tmp/a.txt" } }];

// The agent loop calls the model AGAIN after tools run, to get the next turn.
// A stream that always returns the same toolUse response loops forever — the
// tools re-execute on every pass. Turn 1 issues the scripted calls; turn 2
// stops. pi's own faux helper does this by cycling a response array.
let turn = 0;

session.agent.streamFunction = () => {
  const stream = createAssistantMessageEventStream();
  if (turn++ > 0) {
    queueMicrotask(() => {
      const done = { role: "assistant", content: [{ type: "text", text: "done" }], stopReason: "stop" };
      stream.push({ type: "start", partial: { role: "assistant", content: [], stopReason: "pending" } });
      stream.push({ type: "done", reason: "stop", message: done });
    });
    return stream;
  }
  queueMicrotask(() => {
    // Shape mirrors pi's own streamWithDeltas: a `start` event opens the
    // stream, content blocks follow, and `done` closes it. Omitting `start`
    // leaves the consumer waiting forever.
    const partial = { role: "assistant", content: [], stopReason: "pending" };
    stream.push({ type: "start", partial: { ...partial } });
    scripted.forEach((tc, i) => {
      const block = { type: "toolCall", id: tc.id, name: tc.name, arguments: tc.arguments };
      partial.content = [...partial.content, { type: "toolCall", id: tc.id, name: tc.name, arguments: {} }];
      stream.push({ type: "toolcall_start", contentIndex: i, partial: { ...partial } });
      partial.content[i].arguments = tc.arguments;
      stream.push({ type: "toolcall_end", contentIndex: i, toolCall: block, partial: { ...partial } });
    });
    const message = { role: "assistant", content: scripted.map(tc => ({ type: "toolCall", id: tc.id, name: tc.name, arguments: tc.arguments })), stopReason: "toolUse" };
    stream.push({ type: "done", reason: "toolUse", message });
  });
  return stream;
};

try {
  console.log("prompting...");
  const r = await session.prompt("run the scripted calls");
  console.log("prompt resolved:", typeof r);
} catch (e) {
  console.log("PROMPT ERROR:", String(e).split("\n")[0]);
}

console.log(JSON.stringify({ executed, count: executed.length }, null, 2));
process.exit(executed.length ? 0 : 1);
