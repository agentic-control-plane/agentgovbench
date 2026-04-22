<h1 align="center">AgentGovBench</h1>

<p align="center">
  <strong>An open benchmark for AI agent governance. Mapped to NIST AI RMF. Vendor-neutral.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/Scenarios-48-5B5BD6" alt="48 scenarios" />
  <img src="https://img.shields.io/badge/Framework%20runners-7-5B5BD6" alt="7 runners" />
  <a href="https://doi.org/10.6028/NIST.AI.100-1" target="_blank" rel="noopener"><img src="https://img.shields.io/badge/NIST%20AI%20RMF-1.0-4285F4" alt="NIST AI RMF 1.0" /></a>
</p>

<p align="center">
  <a href="https://agenticcontrolplane.com/benchmark">Live scorecard</a> ·
  <a href="https://agenticcontrolplane.com/benchmark/scenarios">All 48 scenarios</a> ·
  <a href="https://agenticcontrolplane.com/blog/how-we-test-agent-governance">Methodology</a> ·
  <a href="https://agenticcontrolplane.com/blog/architecture-is-governance">Architecture-is-governance</a> ·
  <a href="https://agenticcontrolplane.com">agenticcontrolplane.com</a>
</p>

---

## What it measures

Existing benchmarks (HarmBench, InjecAgent, AgentDAM, AgentLeak) test the **model** — does the LLM refuse harmful prompts, resist injection, protect PII. AgentGovBench tests the **governance layer around the model** — the part responsible for who can call which tool, whose identity rides along with each call, how rate limits cascade across delegated subagents, and what the audit record contains after the fact.

```
  What other benchmarks test             What AgentGovBench tests
  ─────────────────────────              ───────────────────────────
       The model's behavior              The system around the model
       (refuses bad prompts?)            (enforces the policy?)
                                         (attributes the call?)
                                         (logs enough to reconstruct?)
```

Eight categories, each mapped to one or more <a href="https://doi.org/10.6028/NIST.AI.100-1" target="_blank" rel="noopener">NIST AI RMF 1.0</a> controls:

| # | Category | What breaks if this fails | NIST |
|---|---|---|---|
| 1 | **Identity propagation** | End user's identity doesn't reach the tool; audit attributes actions to the agent, not the human | <a href="https://doi.org/10.6028/NIST.AI.100-1" target="_blank" rel="noopener">MAP-2.1</a>, <a href="https://doi.org/10.6028/NIST.AI.100-1" target="_blank" rel="noopener">MEASURE-2.6</a>, <a href="https://doi.org/10.6028/NIST.AI.100-1" target="_blank" rel="noopener">GOVERN-1.4</a> |
| 2 | **Per-user policy enforcement** | User X's subagent performs actions X was forbidden from | <a href="https://doi.org/10.6028/NIST.AI.100-1" target="_blank" rel="noopener">GOVERN-1.2</a> |
| 3 | **Delegation provenance** | Cannot trace a tool call back to the originating user through the delegation chain | <a href="https://doi.org/10.6028/NIST.AI.100-1" target="_blank" rel="noopener">MEASURE-2.3</a> |
| 4 | **Scope inheritance** | Child agent inherits parent's broader scope instead of being narrowed to its task | <a href="https://doi.org/10.6028/NIST.AI.100-1" target="_blank" rel="noopener">MAP-4.1</a>, <a href="https://doi.org/10.6028/NIST.AI.100-1" target="_blank" rel="noopener">MEASURE-2.7</a> |
| 5 | **Rate-limit cascade** | User bypasses a rate limit by spawning N subagents | <a href="https://doi.org/10.6028/NIST.AI.100-1" target="_blank" rel="noopener">MANAGE-2.1</a> |
| 6 | **Audit completeness** | Actions happen without logs, or logs lack detail for forensic reconstruction | <a href="https://doi.org/10.6028/NIST.AI.100-1" target="_blank" rel="noopener">MEASURE-2.3</a> |
| 7 | **Fail-mode discipline** | Gateway failure → system defaults to fail-open when policy says fail-closed (or vice versa) | <a href="https://doi.org/10.6028/NIST.AI.100-1" target="_blank" rel="noopener">GOVERN-1.1</a> |
| 8 | **Cross-tenant isolation** | Tenant A's agent observes or affects tenant B's data | <a href="https://doi.org/10.6028/NIST.AI.100-1" target="_blank" rel="noopener">GOVERN-1.2</a> |

Deeper rationale and threat model: [`METHODOLOGY.md`](METHODOLOGY.md). Full control mapping: [`NIST_MAPPING.md`](NIST_MAPPING.md). All 48 scenarios with expected outcomes: [`scenarios/`](scenarios/).

## Quickstart

### 1. Run the no-governance baseline (zero setup, ~60 seconds)

Works on a fresh clone with no credentials. Shows what a framework scores when governance is not in place — the scorecard floor.

```bash
git clone https://github.com/agentic-control-plane/agentgovbench
cd agentgovbench
python -m venv .venv && source .venv/bin/activate
pip install -e .
agentgovbench run --runner vanilla
```

Expected: **13/48**. Shows the harness, scorer, and scenario library are working.

### 2. Reproduce the ACP scorecard (zero Firebase, ~5 minutes)

Hits a live ACP deployment using only an API key. No Firebase Admin SDK, no service-account JSON. You'll need a `gsk_` API key minted on the target ACP deployment with `bench.impersonate` and `admin.audit.read` scopes.

```bash
pip install -e '.[acp]'   # adds firebase-admin; optional for this runner, required for --runner acp
export ACP_API_KEY=gsk_your-tenant-slug_...
export ACP_BASE_URL=https://api.agenticcontrolplane.com   # or your deployment
export ACP_TENANT_SLUG=your-tenant-slug
agentgovbench run --runner acp_api --out results/acp-api.json
```

Expected: **46/48** against `api.agenticcontrolplane.com`, with 2 documented declinations (the two cross-tenant scenarios that require multi-tenant deployment mode). Different number? Either you're on an older ACP version, your tenant has custom policy that changes outcomes, or you've found a governance gap we haven't seen. [File an issue.](https://github.com/agentic-control-plane/agentgovbench/issues)

### 3. Run any of the seven framework runners

```bash
agentgovbench run --runner crewai_native                # CrewAI without governance — baseline
agentgovbench run --runner crewai_acp                   # CrewAI + ACP @governed decorator
agentgovbench run --runner langgraph_native
agentgovbench run --runner langgraph_acp
agentgovbench run --runner claude_code_acp              # via hook protocol
agentgovbench run --runner codex_acp
agentgovbench run --runner openai_agents_acp            # via base_url proxy
agentgovbench run --runner anthropic_agent_sdk_acp      # via governHandlers
agentgovbench run --runner cursor_acp                   # via MCP server

# Limit to one category for quick iteration
agentgovbench run --runner acp_api --category identity_propagation
```

Each framework runner requires the respective SDK. Install with `pip install -e '.[crewai]'` / `.[langchain]` / etc.

## The seven-framework result

We ran every runner against the same backend and published every scorecard. The nine-point spread tells the story:

| Integration pattern | Frameworks | Score |
|---|---|---|
| **Decorator** at orchestration boundary | Anthropic Agent SDK (`governHandlers`) | **46 / 48** |
| **Proxy** | OpenAI Agents SDK (`base_url` swap) | 45 / 48 |
| **Hook** | Claude Code · Codex CLI | 43 / 48 each |
| **Decorator** below orchestration | CrewAI · LangGraph (`@governed`) | 40 / 48 each |
| **MCP** | Cursor | 37 / 48 |

Same gateway. Same scenarios. Same scorer. The spread is architectural, not product-quality. [Full walkthrough →](https://agenticcontrolplane.com/blog/architecture-is-governance)

## Design principles

- **Deterministic** — no LLM in the hot path. Scenarios fully describe the agent action sequence; governance is tested on what it does with those actions. Reproducible byte-for-byte across runs.
- **Framework-agnostic** — scenarios don't assume CrewAI, LangGraph, Claude, etc. They describe actions and expected governance outcomes.
- **Pluggable** — any governance product implements the `BaseRunner` interface. No ACP assumptions in the scenarios.
- **Versioned** — each scenario carries a version. Old results remain comparable; new scenarios extend the set without breaking history.
- **Published honest** — the reference ACP runner declines 3 scenarios in its own scorecard. A benchmark that says *"we pass everything"* isn't credible.

## Submitting results for your product

We want your product represented. The ACP team built this benchmark, but the scenarios don't know what ACP is — the same `BaseRunner` interface works for any governance product, regardless of vendor.

1. Implement `BaseRunner` in `runners/<your-product>.py` — typically ~200 lines.
2. Run the full scenario set and commit `results/<your-product>-vX.Y.Z.json`.
3. Open a PR. No cherry-picking, no hidden config. That's the point.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the runner template and PR checklist.

## Status

**v0.2** — 48 scenarios across 8 categories. Reference ACP runner passes 45/48 with 3 documented declinations. Seven framework runners shipped. Live scorecard at [agenticcontrolplane.com/benchmark](https://agenticcontrolplane.com/benchmark).

Maintained by the [Agentic Control Plane](https://agenticcontrolplane.com) team. We're the first to put a number on our own governance product; we'd like the rest of the space to follow.

## Citing

```
@software{agentgovbench2026,
  title        = {AgentGovBench: an open benchmark for AI agent governance},
  year         = {2026},
  version      = {0.2.0},
  url          = {https://github.com/agentic-control-plane/agentgovbench}
}
```

## License

MIT. See [`LICENSE`](LICENSE).
