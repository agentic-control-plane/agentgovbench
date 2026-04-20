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

### Against your own ACP instance

Reproduces the published scorecard on *your* deployment. Requires a Firebase service account JSON with admin rights on your ACP project.

```bash
# 1. Clone and install
git clone https://github.com/openagentgov/agentgovbench
cd agentgovbench
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Point at YOUR Firebase project
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/firebase-service-account.json
export AGB_PROJECT=your-firebase-project-id
export AGB_EMAIL_DOMAIN=bench.yourdomain.com   # any domain you control
export FIREBASE_WEB_API_KEY=your-public-firebase-web-api-key

# 3. Bootstrap a clean benchmark tenant + synthetic users
python setup/bootstrap_tenant.py
# Prints AGB_TENANT_ID and AGB_TENANT_SLUG

# 4. Set those into your environment and run
export AGB_TENANT_ID=<printed_above>
export AGB_TENANT_SLUG=agentgovbench
python -m agentgovbench run --runner acp --out results/my-acp.json
```

If your ACP instance is configured with the same policy defaults we ship (`setup/bootstrap_tenant.py` writes them for you), you should see the same scorecard as the reference implementation: **45/48**, with 3 documented gaps.

If you see *different* results, that's the benchmark's main job — either you're on an older ACP version (upgrade and rerun) or you've found a real governance gap in our product we haven't seen yet. File an issue.

### Against any governance product (not just ACP)

Implement the `BaseRunner` interface (`benchmark/runner.py`). Scenarios are framework-agnostic — they describe what the governance layer should enforce, not how. See `CONTRIBUTING.md` for the runner template.

```bash
python -m agentgovbench run --runner vanilla        # no-governance baseline
python -m agentgovbench run --runner acp            # reference runner
python -m agentgovbench run --runner my-vendor      # your runner

# Limit to one category for quick iteration
python -m agentgovbench run --runner acp --category identity_propagation

# Full results JSON
python -m agentgovbench run --runner acp --out results.json
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
