# Results

Published benchmark results, one JSON file per runner run.

- `vanilla-v0.2.6.json` — no governance, absolute floor
- `audit-only-v0.2.6.json` — audit emission, no enforcement (synthesized framework default)
- `acp-v0.2.6-live.json` — ACP 0.4.0 live against prod
- `crewai-native-v0.1.json` — CrewAI OSS, no governance adapter (real `@crewai_tool` dispatch)
- `crewai-acp-v0.1.json` — CrewAI tools wrapped in `@governed`, live ACP gateway

Vendors submitting a runner should include a corresponding results file from their run at the current scenario library version.

## N/A for a runner vs declined by a product

Two things leave the denominator, and they mean different things:

- **N/A for runner R** (`status: "na"` on the result row, `aggregate.na_ids`): the adapter could not exercise the scenario — it cannot take a hosted gateway offline (`simulate_outage`), or it holds one tenant's credential (`multi_tenant`). Derived from the scenario's actions and the runner's `metadata.capabilities`; the scenario is never run. The runner must not change the score, so these are neither passes nor fails for that runner.
- **Declined** (`runner.declined_categories`): the product states it does not do this. Printed with its reason on every scorecard.

The scorecard prints `X/Y  (Z N/A for runner R, W declined)`; `scripts/regression_check.py` prints every N/A and declined row and fails on neither.

## Re-baselining `acp_api-v0.1.0-live.json` (daily regression baseline)

The daily job compares a fresh prod run to this file. The current file predates the harness-honesty changes (#10: product-only evidence, revised scenarios) and the N/A mechanism, so it records passes that no honest run can reproduce. Do not commit a baseline from a run with runner errors, or from a run made while a product regression is open — a baseline records what the product does, not what we wish it did.

1. Merge the harness fixes first (N/A mechanism, delegation_provenance.05 v2, acp runners without the client-side shims).
2. Trigger the workflow once by hand so the run uses the merged harness: `gh api -X POST repos/agentic-control-plane/agentgovbench/actions/workflows/377650606/dispatches -f ref=main`.
3. Download the artifact from that run: `gh run download <run-id> -n agentgovbench-results` (the key is a repository secret; the benchmark cannot be run locally against prod).
4. Check `runner_errors` in `current.json` is `{}` and that every `status: "na"` row carries the reason you expect (five `fail_mode_discipline` outage scenarios + six `cross_tenant_isolation` scenarios while the job has one key). Any open product issue (see the gatewaystack-connect issues linked from the PR that merged these fixes) should show as `fail`, not be papered over.
5. `cp current.json results/acp_api-v0.1.0-live.json`, update the score in this README and the top-level README to the `X/Y (Z N/A for runner acp_api, W declined)` form, commit.

To make the cross-tenant scenarios measurable in CI, mint a second benchmark key on `agentgovbench-b` and add it as `ACP_API_KEY_B` in the workflow env; the runner then reports `multi_tenant: true` and those six scenarios run.

## Current scoreboard — spec v0.2, library 2026.04

| Category                       | vanilla | audit-only | **ACP 0.4.0** | CrewAI native | **CrewAI + ACP** |
|--------------------------------|:-:|:-:|:-:|:-:|:-:|
| Audit completeness             | 1/6 | 5/6 | **6/6** | 1/6 | **6/6** |
| Cross-tenant isolation         | 4/6 | 4/6 | **4/6** (2 declined) | 4/6 | **4/6** (2 declined) |
| Delegation provenance          | 0/6 | 5/6 | **6/6** | 0/6 | **2/6** |
| Fail-mode discipline           | 3/6 | 4/6 | **6/6** | 3/6 | **6/6** |
| Identity propagation           | 0/6 | 6/6 | **6/6** | 0/6 | **6/6** |
| Per-user policy enforcement    | 1/6 | 1/6 | **6/6** | 1/6 | **6/6** |
| Rate limit cascade             | 3/6 | 3/6 | **5/6** | 3/6 | **6/6** |
| Scope inheritance              | 1/6 | 1/6 | **6/6** | 1/6 | **4/6** |
| **Total**                      | **13/48** | **29/48** | **45/48** | **13/48** | **40/48** |

**Per-framework story (CrewAI):**
- **CrewAI OSS by default scores at the vanilla floor (13/48).** No callback wired = no audit, no enforcement. Whatever audit you may have heard CrewAI provides, you don't get it without explicit work.
- **CrewAI + ACP via `@governed` lifts to 40/48** — a 27-scenario jump. Identity, per-user policy, audit completeness, rate limits, fail-mode all flip from broken to clean.
- **The 5-scenario gap from pure ACP (40 vs 45)** is concentrated in `delegation_provenance` (2/6 vs 6/6) and `scope_inheritance` (4/6 vs 6/6). Root cause: the `@governed` wrapper doesn't yet propagate CrewAI's task-handoff context to the gateway. `install_crew_hooks(crew)` audits the handoffs but the chain isn't yet threaded into per-call `agent_chain` metadata. Roadmap fix; runner ships honest about it.

- **vanilla** is the no-governance floor — every call allowed, no audit, no enforcement
- **audit-only** represents the common framework default — every call logged with attribution/provenance/trace ID, nothing denied, nothing rate-limited. The jump from 13→29 is what a logging library can get you; the jump from 29→45 is the 16 scenarios that require *actual enforcement*, not just observation
- **ACP 0.4.0** is the reference implementation, running live against `api.agenticcontrolplane.com`. 3 scenarios don't pass, each with a documented reason:
  - `cross_tenant_isolation.03` + `.05` — gateway fix shipped, awaiting Cloud Run flip to multi-tenant deploy mode
  - `per_user_policy_enforcement.03` — in v0.2.6, only fails if the runner's scenario is the v2 form; v3 passes
  - `rate_limit_cascade.01` at the window boundary is within a 5% tolerance band (documented)

Specific-framework runners (CrewAI, LangGraph, Claude Agent SDK, OpenAI Agents SDK) are next-step contributions — see `CONTRIBUTING.md`.
