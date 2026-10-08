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

Expected: **13/48** ([full vanilla scorecard →](https://agenticcontrolplane.com/blog/full-scorecard-seven-frameworks-48-scenarios)). Shows the harness, scorer, and scenario library are working.

### 2. Reproducing the ACP score (zero Firebase, ~5 minutes)

Hits a live ACP deployment using only an API key — no Firebase Admin SDK, no service-account JSON.

Every scenario writes its own policy before it runs, and the runner clears policy between scenarios. ACP never lets an API key change the policy of a real workspace (an agent's key must not be able to loosen its own rules), so the benchmark runs in a **separate benchmark workspace** with a short-lived key that may write policy only there:

1. Sign in to the [ACP console](https://cloud.agenticcontrolplane.com) as a workspace owner or admin → **API Keys** → in the **Reproduce our AgentGovBench score** card, click **Create benchmark workspace**. (An API key can't do this step — it has to be a signed-in human.)
2. It creates a workspace named `<yourslug>-agb`, shows a key once (expires in 24 hours), and prints the command. Copy and run it:

```bash
git clone https://github.com/agentic-control-plane/agentgovbench && cd agentgovbench
pip install -e .
export ACP_API_KEY=gsk_...                  # the key the card shows; valid 24 hours
export ACP_TENANT_SLUG=yourslug-agb         # the workspace the card shows
agentgovbench run --runner acp_api --out results/acp-api.json
```

`ACP_BASE_URL` defaults to `https://api.agenticcontrolplane.com`; set it only for your own deployment. Your real workspace's policy is never read or changed. Clicking the button again reuses the same benchmark workspace and gives you a fresh 24-hour key.

Before scoring anything the runner **preflights** the key: it reads the benchmark workspace's policy and writes the same document back (a no-op on success), so the gateway has to actually authorise a policy write with your key. If the gateway answers `403 human-auth-required`, the tenant in `ACP_TENANT_SLUG` is not a benchmark workspace and the run stops there with that explanation — no scorecard. The only way to drive a non-benchmark tenant is `ACP_ADMIN_TOKEN` (a Firebase ID token for a signed-in admin of that tenant); when set it is used for policy setup only, and the API key remains the credential whose behaviour is measured.

With one benchmark workspace the `acp_api` runner measures 37 of the 48 scenarios and reports the other 11 as not measured (6 cross-tenant scenarios need a second tenant; 5 fail-mode scenarios need a real outage). In the merged scorecard all 6 fail-mode scenarios, including the no-failure baseline, are scored by the hook runner. Those are measured by the pieces below. Different number? Either you're on an older ACP version or you've found a governance gap we haven't seen. [File an issue.](https://github.com/agentic-control-plane/agentgovbench/issues)

#### Measuring all 48

The published score is out of **48**. Declined scenarios count as failures and are named; a scenario nobody measured counts against the score. Two more pieces cover the rest:

- **Cross-tenant (6 scenarios):** create a second benchmark workspace the same way and export its key too: `export ACP_API_KEY_B=gsk_... ACP_TENANT_SLUG_B=yourslug-b-agb`. The runner then maps the scenarios' two tenants onto two real ones.
- **Fail-mode (6 scenarios, 5 need an outage):** these test what the client does when ACP is unreachable, which the shipped Claude Code hook decides, not the hosted gateway. The `claude_code_hook` runner executes the real, unmodified `govern.mjs` with the gateway really down (a closed local port, a local server answering HTTP 500). It needs only node and a clone of the plugin; no credentials:

```bash
git clone https://github.com/agentic-control-plane/claude-code-acp-plugin
AGB_CC_PLUGIN=$PWD/claude-code-acp-plugin/bin/govern.mjs \
  agentgovbench run --runner claude_code_hook --category fail_mode_discipline --out hook.json
python scripts/scorecard.py results/acp-api.json hook.json --date $(date +%F)   # merged X/48 + SCORECARD.md
```

How the hook runner is honest about what it is: the hook is the unmodified shipped file; the outage is real; but the gateway during recovery and in the no-failure baseline is a local always-allow stand-in, so this category measures the client's failure posture only. The plugin has no per-tenant fail-mode setting; it fails open (loudly) for attended sessions and closed for unattended ones, so the runner runs a `fail_open` scenario as an attended session and a `fail_closed` one as an unattended one. That mapping is the runner's and is part of the method, not a product claim.

The merged, dated scorecard (which runner measured which scenario, every non-pass named) is produced daily by [the workflow](.github/workflows/daily-acp-regression.yml) and published in each [run's summary](https://github.com/agentic-control-plane/agentgovbench/actions/workflows/daily-acp-regression.yml).

Known non-passes counted in the score: `scope_inheritance.04_task_narrowing` (declined: ACP does not yet hold a sub-agent to a narrower task than its parent's scope) and `fail_mode_discipline.05_no_audit_without_governance` (the hook's offline record of an ungoverned call carries no user identity, so the scenario's attribution check cannot be met client-side).

> **"RUN ABORTED — no scorecard produced"?** ACP refused the benchmark's policy write (403), or the key itself (401). The runner stops rather than score scenarios that never got their policy — that would read as a plausible all-allow result, and it used to happen silently. Only a **Create benchmark workspace** key on its `-agb` workspace may write those policies. The API Keys page also still offers an **"AgentGovBench testing (24h, impersonation)"** scope preset; a key from it on your real workspace (or any other key) gets 403 here — it exists for ACP's own reference benchmark workspace, not for reproducing the score. Check the key hasn't expired and that `ACP_TENANT_SLUG` matches the workspace the card showed.

### 3. Run any framework — seven frameworks, each with a native and an ACP runner

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

Historical, from the first release (April 2026), before runner-neutral scoring and before the daily run; not re-measured since, so do not compare these to the dated ACP score above. We ran every runner against the same backend and published every scorecard. The nine-point spread tells the story:

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
- **Published honest** — the reference ACP runner declares 5 declinations in its own committed result file, reasons included (single-tenant runner scope, one capability ACP doesn't have yet). A benchmark that says *"we pass everything"* isn't credible.

## Submitting results for your product

We want your product represented. The ACP team built this benchmark, but the scenarios don't know what ACP is — the same `BaseRunner` interface works for any governance product, regardless of vendor.

1. Implement `BaseRunner` in `runners/<your-product>.py` — typically ~200 lines.
2. Run the full scenario set and commit `results/<your-product>-vX.Y.Z.json`.
3. Open a PR. No cherry-picking, no hidden config. That's the point.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the runner template and PR checklist.

## Status

**v0.2** — 48 scenarios across 8 categories. ACP is scored out of all 48 by `scripts/scorecard.py` from two runners (HTTP against production, and the real Claude Code hook for the outage scenarios); see the daily workflow summary for the dated number. The old per-framework figures below predate runner-neutral scoring and are historical. Seven frameworks shipped, each with a native and an ACP runner. Live scorecard at [agenticcontrolplane.com/benchmark](https://agenticcontrolplane.com/benchmark).

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
