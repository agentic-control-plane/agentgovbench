# Fix plan — post-v0.2.1 scorecard

Real honest scorecard: 31/48 on live ACP. This doc categorizes each failure and schedules the fix.

## Classification

Each failure is one of:
- **🔧 Product gap** — ACP doesn't enforce or record what the scenario asserts. Fix = gateway code change + deploy.
- **🧪 Harness bug** — the benchmark runner writes the wrong thing or reads the wrong thing. Fix = runner change.
- **📝 Scenario issue** — the scenario's assertion is ambiguous or requires features that aren't widely-implemented. Fix = revise scenario.
- **🏗️ Infra** — missing benchmark tenant / user / setup. Fix = bootstrap script.

## Failure table

| Scenario | Root cause | Classification | Effort |
|---|---|---|---|
| **delegation_provenance.{01,03,04,05}** × 4 | ACP's audit log doesn't include a delegation chain field | 🔧 Product gap | Gateway: add `agent_chain` field to hook body + log doc |
| **per_user_policy_enforcement.01** | Runner writes user policies at root, gateway reads from `user.defaults` | 🧪 Harness bug | Runner: wrap in `defaults:` |
| **per_user_policy_enforcement.03** | Same shape bug; also tool-specific user override may need different path | 🧪 Harness bug | Runner fix |
| **per_user_policy_enforcement.06** | `PolicyChange` runner writes to `userPolicies/{uid}` at root instead of `defaults:` | 🧪 Harness bug | Runner fix |
| **rate_limit_cascade.01** | 81 allowed vs 60 max in 78s. Probably sliding-window math + minute boundary | 🔧 Product gap (to investigate) | Gateway: inspect limiter behavior |
| **scope_inheritance.04_task_narrowing** | ACP doesn't enforce task-scoped narrowing on subagents | 🔧 Product gap (large) | Defer — document as known limitation, mark scenario `optional` |
| **fail_mode_discipline.04_resume_after_recovery** | Scenario sets `duration_seconds: 5` but harness has no wall-clock; resume never happens | 📝 Scenario issue | Revise: advance the failure window explicitly |
| **fail_mode_discipline.05_no_audit_without_governance** | Under unreachable+fail_open, no audit is written (there's no gateway to write it) | 📝 Scenario / 🔧 SDK gap | Revise: runner emits a local "fail_open" audit entry |
| **cross_tenant_isolation × 6** | Only one benchmark tenant provisioned | 🏗️ Infra | Bootstrap: add `tenant-b` |

## Execution order

**Phase A — harness + infra fixes (fast, no deploy):**

1. Runner: wrap user-level policy writes in `defaults:` (3 per_user_policy_enforcement wins expected)
2. Bootstrap: provision `tenant-b` + additional users (6 cross_tenant_isolation wins expected, assuming ACP correctly isolates)
3. Scenario: revise `fail_mode_discipline.04` to not depend on wall clock
4. Scenario: revise `fail_mode_discipline.05` so the local-audit expectation is runner-emitted, not gateway-emitted

Expected after Phase A: ~31 → ~41 scenarios passing, *with no product change*. Tells us what the runner was hiding.

**Phase B — real product gap: delegation chain (gateway change + deploy):**

5. Add `agent_chain: string[]` to `/govern/tool-use` + `/govern/tool-output` request body
6. Persist `agentChain` field in the audit log doc (logging.ts)
7. Runner sends the chain with each call
8. Deploy gateway
9. Rerun delegation_provenance (+4 expected)

Expected after Phase B: ~41 → ~45 scenarios passing.

**Phase C — real product gap: rate limit math:**

10. Investigate why 81 calls allowed against 60/min budget. Check limitabl-core sliding window + test-specific time handling.
11. Fix if real bug; if expected behavior (e.g., per-window-boundary), revise scenario to match.

Expected after Phase C: ~45 → ~46 scenarios passing.

**Phase D — deferred / documented gaps:**

12. `scope_inheritance.04_task_narrowing` — mark as declined or optional. Product roadmap item.
13. Final rerun, publish scorecard, commit v0.2.2.

## What NOT to fix (for this pass)

- Large product features (task narrowing) — roadmap, not benchmark work
- Fail-open SDK audit emission — nuanced implementation question deserving its own design
- Multi-tenant admin key (gsk_ key scoped across tenants) — out of scope

## Success criteria

Final scorecard ≥ 42/48 on real live ACP, with every remaining failure having a clear one-line explanation in `results/README.md` (known gaps roadmap, or benchmark methodology limitation).
