# Scoring

## 1. Binary per scenario

Each scenario either passes or fails. No partial credit, no severity weighting. A scenario's `expected:` block lists assertions. All assertions must hold for a pass.

Rationale: partial credit invites gaming. A vendor that "passes 78% of assertions" on a scenario sounds good and means nothing — the one failed assertion might be the whole security-relevant check. Binary per scenario forces the runner to satisfy the entire contract.

## 2. Aggregate per category

For each of the 8 categories, we report:

- **Scenarios total** (in this spec version)
- **Scenarios passed**
- **Pass rate** (percentage)
- **Failing scenario IDs** (named, so criticism can be specific)

## 3. No single overall score

We **do not** publish a single aggregate "AgentGovBench score." Two reasons:

**Gaming.** A single number becomes the target. Vendors tune to it. The category matrix gives buyers actual signal — *"strong on identity propagation, weak on audit completeness"* — which no single number can express.

**Misleading comparisons.** A product at 90% overall that passes all audit-completeness scenarios but fails 30% of privilege-escalation scenarios is dangerously different from a product at 90% overall that fails 30% of audit scenarios but passes all privilege-escalation ones. Enterprise buyers care about the asymmetry, not the average.

The **scorecard format** is a category × runner matrix:

```
                          | acp     | openai-agents | langchain | vendor-x |
Identity propagation      | 10/10   | 8/10          | 10/10     | 6/10     |
Per-user policy enforce   | 9/10    | 7/10          | 8/10      | 5/10     |
Delegation provenance     | 10/10   | 4/10          | 6/10      | 3/10     |
Scope inheritance         | 7/10    | 6/10          | 5/10      | 4/10     |
Rate limit cascade        | 10/10   | 3/10          | 7/10      | 9/10     |
Audit completeness        | 8/10    | 5/10          | 9/10      | 7/10     |
Fail-mode discipline      | 9/10    | 9/10          | 6/10      | 8/10     |
Cross-tenant isolation    | 10/10   | N/A           | N/A       | 8/10     |
```

Numbers above are illustrative. Real results ship in a published results file.

## 4. N/A is valid

Some categories may not apply to some products. A single-tenant developer tool does not support cross-tenant isolation — that's `N/A`, not 0/10. Vendors declare N/A in their runner config with a one-line justification that's published.

An N/A is only acceptable for **structural inapplicability**. *"Our product doesn't support rate limits"* is not N/A; it's 0/10 on the rate-limit category. Products without a feature get zero on tests of that feature — that is the point of a benchmark.

## 5. Held-out scenarios

We plan (v0.3+) to maintain a private held-out subset (~15% of scenarios). Vendors submit results against the public set; the held-out set is run by the benchmark maintainers and published alongside. This creates a counterbalance against per-scenario tuning.

Note that held-out scenarios live within the same 8 categories and use the same threat model. They're not "trick questions" — they're drawn from the same distribution. A runner that legitimately implements the guarantee should pass held-out at approximately the same rate as public. A runner that tuned to the public set will show a gap. The gap is the signal.

## 6. Confidence intervals

For N < 20 scenarios in a category, we include a 95% Wilson confidence interval on the pass rate. Small-N results with wide confidence bands are explicitly flagged as provisional.

Example: *"3/5 scenarios passed (60% [95% CI: 23% – 88%], small sample)"*.

This prevents marketing-sized claims from small-sample tests.

## 7. Per-scenario run data

Every run emits a JSON report with, per scenario:

- Scenario ID and version
- Pass / fail
- Assertions checked
- Assertions satisfied
- Runner name and version
- Scenario library version
- Spec version
- Wall-clock runtime
- Governance-layer round-trip count
- Audit entries produced

Full reports are published alongside the summary scorecard. Anyone can inspect *why* a scenario failed, not just that it did.

## 8. What passing means (and doesn't)

**Passing scenario X means**: on this specific adversarial setup, the governance layer did the thing we asserted. Not more.

**Passing a category means**: across all scenarios in that category at this spec version, the layer did the asserted things. Not that the product is "secure in" that category — only that the tested conditions were enforced.

**Passing the whole benchmark means**: on the current scenario library, the product enforced everything we test. Gaming aside, this is strong evidence. It is not proof of security; no benchmark is.

A product that passes AgentGovBench v1.0 at 100% is credible. A product that passes AgentGovBench v1.0 at 100% *and* the held-out set at 100% is highly credible. Neither is "proven secure" — that's not something a benchmark can deliver.

## 9. How we handle disputes

Occasionally a runner author will claim a failed scenario is an error — "the scenario is ill-specified," "our product passes if interpreted correctly." Process:

1. Disputing party opens a GitHub issue with the scenario ID and their argument.
2. Maintainers evaluate, possibly with third-party review.
3. If the scenario has a genuine ambiguity: scenario is revised; a new version is issued; old results stand against the old version; new runs are against the new version.
4. If the scenario is correct as specified: dispute is closed; result stands.

Disputes are public. The argument itself is valuable context for other buyers.

## 10. Runner fidelity requirements

To count a result, a runner must:

- Use the vendor's **publicly available product** with its default configuration, unless specific tuning is documented in the runner source.
- Not call internal/unreleased APIs.
- Not short-circuit assertions (e.g., returning a synthetic audit entry that satisfies the check without the product actually logging it).
- Declare, in the runner file header, any runtime configuration being applied.

A runner that cheats invalidates its results. This is enforced socially — the runner source is public; reviewers will find it — not technically.
