# AgentGovBench

**An open benchmark for AI agent governance. Mapped to NIST AI RMF. Vendor-neutral.**

Multi-agent LLM systems have a governance layer between the user and the tools: identity propagation, permission enforcement, delegation provenance, rate limiting, audit. Existing benchmarks (AgentLeak, InjecAgent, AgentDAM) test *model* behavior under adversarial conditions. AgentGovBench tests *the governance layer around the model* — the part that's supposed to enforce policy regardless of what the model does.

## What it measures

Eight categories, each mapped to one or more NIST AI RMF 1.0 controls:

| # | Category | What breaks if this fails | NIST |
|---|---|---|---|
| 1 | **Identity propagation** | End user's identity doesn't reach the tool; audit attributes actions to the agent, not the human | MAP-2.1, MEASURE-2.6, GOVERN-1.4 |
| 2 | **Per-user policy enforcement** | User X's subagent performs actions X was forbidden from | GOVERN-1.2 |
| 3 | **Delegation provenance** | Cannot trace a tool call back to the originating user through the delegation chain | MEASURE-2.3 |
| 4 | **Scope inheritance / privilege escalation** | Child agent inherits parent's broader scope instead of being narrowed to its task | MAP-4.1, MEASURE-2.7 |
| 5 | **Rate limit cascade** | User bypasses rate limit by spawning N subagents | MANAGE-2.1 |
| 6 | **Audit completeness** | Actions happen without logs, or logs lack detail for forensic reconstruction | MEASURE-2.3 |
| 7 | **Fail-mode discipline** | Gateway failure → system defaults to fail-open when policy says fail-closed (or vice versa) | GOVERN-1.1 |
| 8 | **Cross-tenant isolation** | Tenant A's agent observes or affects tenant B's data | GOVERN-1.2 |

The deeper rationale and threat model live in [`METHODOLOGY.md`](METHODOLOGY.md). The control mapping and rationale live in [`NIST_MAPPING.md`](NIST_MAPPING.md).

## Running the benchmark

```bash
pip install -e .
# Baseline (no governance)
python -m agentgovbench run --runner vanilla
# Any vendor's runner
python -m agentgovbench run --runner acp
# Specific category only
python -m agentgovbench run --runner acp --category identity_propagation
# All scenarios, JSON output
python -m agentgovbench run --runner acp --json > results.json
```

## Submitting results for your product

We want your product represented. To add a runner:

1. Implement the `BaseRunner` interface in `benchmark/runner.py`
2. Drop your runner in `runners/<your-product>.py`
3. Submit a PR with your runner + a `results/<your-product>-vX.Y.Z.json` from a run against the current scenario set

No cherry-picking, no hidden config — the point of this benchmark is *reproducible*, *comparable* numbers. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Design principles

- **Deterministic** — no LLM in the hot path. Scenarios fully describe the agent action sequence; governance layer is tested on what it does with those actions. Reproducible byte-for-byte across runs.
- **Framework-agnostic** — scenarios don't assume CrewAI, LangGraph, AutoGen. They describe actions and expected outcomes.
- **Pluggable** — any governance product can implement the runner interface. No ACP assumptions in the scenarios.
- **Versioned** — each scenario carries a version. Old results remain comparable; new scenarios extend the set without breaking history.
- **Published honest** — the reference implementation's own results include partial failures. A benchmark that says *"we pass everything"* isn't credible.

## Status

**v0.1** — spec complete, harness scaffolding in place, 3 example scenarios across 2 categories. Scenario library fills in from here.

## Citing

```
@software{agentgovbench2026,
  title        = {AgentGovBench: an open benchmark for AI agent governance},
  year         = {2026},
  url          = {https://github.com/openagentgov/agentgovbench}
}
```

## License

MIT. See [`LICENSE`](LICENSE).
