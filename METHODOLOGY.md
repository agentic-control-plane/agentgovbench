# AgentGovBench — methodology

## 1. The gap

Multi-agent LLM systems have three layers of defense:

1. **Model-level** — how the LLM itself responds to adversarial input. Tested by HarmBench, SALAD-Bench, CyberSecEval, JailbreakBench. These are *alignment* benchmarks.
2. **Privacy at the data layer** — what sensitive data leaks across channels. Tested by AgentLeak (multi-agent), AgentDAM (web agents), PII-Scope (training-data leakage).
3. **Governance infrastructure** — the identity, permission, audit, and rate-limit layer that sits between the user and the tools. This is where AI governance products (ACP, Guardrails AI, Arthur AI, Credo AI, and every enterprise's internal proxy) live.

**There is no standard benchmark for the third layer.** This is the gap.

Governance infrastructure failures are invisible to existing benchmarks because those benchmarks measure *what the model says* or *what data reached the output*. They don't ask:

- Did the audit log correctly attribute this tool call to the originating user?
- When the orchestrator spawned a worker, did the worker inherit the user's scope or bypass it?
- If the rate limit is 60 requests/min per user, does spawning 10 subagents bypass it?
- When the governance layer's upstream dies, does the system fail open or fail closed?
- Across tenants, does one tenant's policy edit leak into another tenant's resolution?

These are the questions AgentGovBench answers.

## 2. Who this is for

| Audience | What they get |
|---|---|
| **Compliance / risk officers** buying AI governance | NIST-mapped results, comparable across vendors. *"Does this product pass NIST MEASURE-2.3 for delegation provenance? Here's the evidence."* |
| **Engineering leads** evaluating options | Technical depth. Reproducible scenarios. Apples-to-apples comparison across products. |
| **Security researchers** | Threat model we can critique and extend. Scenario library that grows. |
| **AI governance vendors** | A way to represent their product fairly. Category-level wins/losses the market can see. |
| **Regulators / NIST / standards bodies** | An open, vendor-neutral instrument they can point at in guidance. |
| **Academics** | Citable framework for multi-agent governance research. |

## 3. Non-goals

- **Not an alignment benchmark.** Model behavior under adversarial prompts is someone else's problem. HarmBench, SALAD-Bench, and JailbreakBench do this well already.
- **Not a privacy-leakage benchmark.** AgentLeak already tests that. We assume PII redaction is orthogonal and may cite their results for products that implement both layers.
- **Not a tool-use-safety benchmark.** InjecAgent covers indirect injection leading to tool misuse. We assume that's a separate test.
- **Not a product capability benchmark.** We don't measure whether your governance product has feature X. We measure whether the guarantees it claims to provide are actually enforceable under adversarial conditions.

## 4. Design principles

### 4.1 Determinism over realism

Most scenarios require no LLM. A governance layer sees tool calls and metadata, not the model's chain of thought. By mocking the agent's "intent" as a deterministic sequence of attempted tool calls, we get:

- Identical runs across providers (OpenAI, Anthropic, Google, local)
- Zero API cost for reproduction
- Fast iteration (whole benchmark runs in seconds, not hours)
- Clean attribution: if a scenario fails, it's a governance-layer bug, not an LLM roll

Some scenarios in the *Resistance* family (see category list in `spec/categories.md`) do exercise an LLM to reproduce injection dynamics. These are flagged and optional.

### 4.2 Framework-agnostic scenarios

A scenario describes:
- Initial state (tenants, users, policies, tools)
- A sequence of agent actions
- Expected governance outcomes (what was blocked, what was logged, what attribution was recorded)

No mention of CrewAI, LangGraph, AutoGen, Claude Agent SDK, or OpenAI Agents SDK. A scenario is an abstract contract. A runner for a specific vendor/framework implements the contract.

### 4.3 Pluggable runners

Each governance product implements the `BaseRunner` interface. The benchmark harness executes the scenario against the runner and scores the outcome. Contributing a runner is the primary way for vendors to get represented.

### 4.4 Versioned scenarios

Scenarios carry a `version` field. Runners report which scenario version they passed. New scenarios extend the library; old results remain comparable. Breaking changes to a scenario create a new version (scenario_v2), keeping v1 alive for historical comparisons.

### 4.5 Published honest

The reference runner's own scorecard appears in the repo and is never cherry-picked. If ACP fails 2 of 10 audit-completeness scenarios at the current version, those are reported. A benchmark that always shows its own sponsor at 100% isn't credible.

## 5. Anchoring to NIST AI RMF

Each scenario maps to one or more NIST AI RMF 1.0 controls. This matters for three reasons:

1. **Procurement and compliance.** Enterprises buying AI governance want to see specific NIST citations in vendor claims. *"Passes MEASURE-2.3 across all delegation-provenance scenarios"* is a credible procurement input. *"Achieves 92% on AgentGovBench"* is a marketing number that means nothing to compliance.

2. **Independence from the benchmark itself.** A vendor-authored benchmark has a *"who benchmarks the benchmarker?"* problem. Tying each scenario to an external control means every result cites NIST, not us. The categories and scenarios may be ours; the authority is external.

3. **Longevity.** NIST AI RMF is not going away. Scenarios that are tied to specific controls remain meaningful even when the benchmark itself evolves.

The full mapping lives in [`NIST_MAPPING.md`](NIST_MAPPING.md). Each scenario YAML carries the `nist:` list.

## 6. Threat model (summary)

The benchmark assumes an adversarial multi-agent environment where:

- The end user may be malicious.
- The LLM may be misled by indirect prompt injection (tool outputs containing instructions).
- A subagent may attempt actions beyond its scope.
- The governance layer is network-separated from the agent (can fail, can be bypassed).
- Multiple tenants share the governance infrastructure.
- Tools have sensitive capabilities (read secrets, execute code, call external APIs).

Full model in [`THREAT_MODEL.md`](THREAT_MODEL.md).

## 7. Scoring philosophy

Binary per scenario: the governance layer either enforced the claimed guarantee or it didn't. Per-category: pass rate as percentage. Overall: weighted average across categories.

**We do not publish a single "score."** A single number invites gaming and misleads procurement. We publish the category-level matrix. A product might ship with 100% on identity propagation and 60% on audit completeness; that asymmetry is the buyer's signal, not a hidden aggregate.

Full details in [`SCORING.md`](SCORING.md).

## 8. Defense against gaming

Benchmarks get gamed once results matter. Mitigations:

### 8.1 Scenario rotation

v0.1 ships ~80 scenarios (target). Each quarterly release rotates ~20% of scenarios — retiring some, adding new ones. The rotation makes vendor-specific tuning decay over time.

### 8.2 Held-out set

A portion of scenarios (~15%) is held out and published only as an encrypted tarball. Vendors can submit results; we run the held-out set independently and publish those numbers alongside. Tuning to the public set doesn't help the held-out score.

### 8.3 Public runner code

All runners are open source, including ACP's. Reviewers can inspect exactly how each vendor interprets the scenario, catching "creative" adapter behavior.

### 8.4 Reproduce-or-reject

Any submitted result must be reproducible by a third party running the published runner against the published scenarios. Non-reproducible submissions are rejected without explanation. This disincentivizes one-off marketing numbers.

## 9. Ethics and bias

### 9.1 Vendor authorship

This benchmark is authored by the team behind ACP (Agentic Control Plane). The conflict of interest is declared up front. Mitigations: the scenarios are developed based on NIST controls and multi-agent threat models, not on ACP's feature set. Categories are chosen because they represent real governance primitives, not because ACP is strong in them. If ACP underperforms a competitor in a category, that result is published unchanged.

### 9.2 Accepting that some scoring is subjective

"Audit completeness" is partially subjective. We specify the assertions precisely (audit entry must contain user UID, tool name, input hash, decision, reason, timestamp, trace ID). But a runner might log *more* than we check, or log differently. We assert the minimum; richer logging is not penalized.

### 9.3 Not testing features, testing guarantees

A product may claim "per-user rate limits." That's a feature. The benchmark tests: *when a user's actions fan out through three subagents in parallel, do the rate limits aggregate correctly?* That's a guarantee. Features are marketing; guarantees are engineering. We test the latter.

## 10. Versioning and decay

- **Spec version** (semver) — changes to categories, scoring, or runner interface
- **Scenario version** (per-scenario field) — changes to the scenario's setup or assertions
- **Scenario library version** — releases of the bundled library (tagged quarterly)

A result is cited as: *"AgentGovBench spec v1.2, scenario library 2026Q2, ACP v0.4.0, 42/50 scenarios passed across all categories."*

## 11. Roadmap

- **v0.1** (now): spec + 8 categories defined, harness scaffolding, 3 example scenarios, 2 reference runners
- **v0.2**: 40–80 scenarios across all 8 categories, ACP results published, first external runner contributed
- **v0.3**: held-out set published (encrypted), quarterly rotation process defined
- **v1.0**: NIST AI RMF Playbook contribution, arXiv preprint methodology paper, 3+ vendor runners shipped

## 12. Comparison to adjacent work

| | Model alignment | Data leakage | Governance infra |
|---|---|---|---|
| **HarmBench / SALAD** | ✅ primary | — | — |
| **AgentLeak** | — | ✅ primary (multi-agent) | partial (C6 logs) |
| **AgentDAM** | — | ✅ primary (web agents) | — |
| **InjecAgent** | ✅ (indirect injection) | — | partial (tool misuse) |
| **CyberSecEval** | ✅ (secure code) | — | partial |
| **AgentGovBench** | — | — | **✅ primary** |

The four existing benchmark categories are complementary. A mature AI system runs at least one of each. AgentGovBench is the one that currently doesn't exist.
