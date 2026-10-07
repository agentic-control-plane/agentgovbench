# Measurement discipline

Rules for producing an AgentGovBench number that deserves to be cited.
Several are implemented in the harness (noted); the rest bind the humans
running and publishing scorecards.

A sibling benchmark in the observability space —
[davila7/agents_otel_data](https://github.com/davila7/agents_otel_data),
which scores five platforms' trace read APIs — arrived independently at
much of this discipline after shipping the same failure modes we did
(a credentials mishap produced a 6.67/100 that was pure measurement
error; ours produced a plausible-looking 13/48 the same way). Where a
rule below was sharpened by their write-up, it is marked *(cf. otel)*.

## 1. Score only what the product did

**Enforced by the harness.** Every `ToolOutcome` and `AuditEntry`
carries `source`; anything marked `harness` is stripped before scoring
(`scorer._product_evidence`). A runner is an adapter: it may translate
a scenario into the product's configuration surface and read decisions
back, but the moment it computes a decision or synthesizes an audit
record, it is scoring itself.

Provenance marking is honest-runner infrastructure, not tamper-proofing
— a third-party runner could mark fabricated evidence `product`. The
defense against that is rule 5, not more markers.

## 2. Validate the instrument, not the contestant

**Enforced by `tests/test_discrimination.py`.** Known-answer subjects —
a null runner (reports nothing, must score 0), a permissive runner (no
governance, must pass only declared negative controls), a liar runner
(fabricates a flawless audit trail, must score no better than the
permissive one) — prove the library discriminates without reference to
how any real product scores. A scenario an ungoverned system passes is
either broken or an undeclared negative control; CI fails until it is
fixed or declared.

*(cf. otel)* Their equivalent instrument-validation move is "every
metric maps to an actual API call — docs never substitute." Same
principle, read-side.

## 3. Preflight the credential before scoring anything

**Enforced by `BaseRunner.preflight()`.** A run whose setup silently
fails still prints a complete, plausible scorecard — that is worse than
crashing. The preflight must prove the exact capabilities the run
depends on (for `acp_api`: policy WRITE, not just read; audit read),
and the whole run aborts if it cannot. *(cf. otel)* "Credentials decide
benchmarks": their first Logfire score was destroyed by a write-scope
token where a read-scope key was needed. Probe the surface under test
with the operation under test, never a cheaper proxy for it.

## 4. Absence of evidence is not enforcement

**Enforced in the scorer.** `rate_limited_count` fails on zero matching
calls; `no_cross_tenant_leak` fails on an empty audit log; a scenario
the runner errors on scores FAIL and stays in the denominator. A system
that records nothing has not demonstrated isolation — it has left
nothing to inspect. *(cf. otel)* Their variant: "unset is better than
fake" — when contamination made trace lookup ambiguous, they made the
fetch deterministic and let completeness go unset rather than fall back
to a lookalike.

## 5. Only reproduced numbers count

**Process, binding on publication.** Before a scorecard is cited
anywhere public:

- Re-derive it from scratch in a fresh session — new credentials, clean
  tenant state, current harness. A number that does not reproduce is
  not a number.
- When two readings disagree, keep the one *less favorable* to the
  product we own, and disclose the discarded reading. *(cf. otel)*
  Their verifier discarded a favorable freshness outlier for the
  eventual winner and published the counterfactual.
- Negative claims need captured rejections: a declination ("product
  does not support X") must cite the recorded response that proves it
  (e.g. ACP's `403 human-auth-required` on API-key policy writes), not
  an assumption.

## 6. Cohort discipline

**Process.** Scores are comparable only within one cohort: same harness
commit, same scenario library version, all runners re-run back-to-back
under the same conditions. Any change to harness, scorer, or scenarios
invalidates every prior number at once — re-derive all of them, never
splice a new runner's fresh result into a table of stale ones. Result
files are frozen once published; corrections are new dated files, not
edits. *(cf. otel)* They re-run all five platforms in one session on
any change and treat endpoint migrations as score-moving events that
must be disclosed.

## 7. The adapter is a measurement variable

**Process + disclosure.** A runner's quality moves the score as much as
the product does — we have now had four separate incidents where the
runner silently substituted its own behaviour for the scenario's
declaration (fan-out tier hardcoded, workspace policy changes dropped,
target-tenant credential swapped in on forgery scenarios, fail-mode
computed locally). Publish the runner name and version with every
scorecard, and when a score moves, say whether the product or the
adapter changed. *(cf. otel)* A platform jumped from last to 3rd purely
by moving off a deprecated endpoint; they now disclose API-path choice
as a variable of the same magnitude as vendor performance.

## 8. Timing hygiene

**Process, partially in runners.** Rate-limit cooldowns, retry backoff,
and replica-lag sleeps live *outside* any measured window; scenarios
that need time to pass express it with the `wait` action so the harness,
not the adapter, owns the clock. Label single samples as such; medians
of at least three for anything presented as a latency.

## 9. Present profiles, not coronations

**Process.** A single headline number invites gaming and hides shape.
Always publish the per-category table with the total, keep negative
controls reported separately from discriminating scenarios, and state
the no-governance floor (currently 5/48 — the five declared negative
controls) next to any product score so readers can subtract the free
points. *(cf. otel)* They disclose that their normalization is
manipulable by cohort membership and publish a transform-sensitivity
table; our equivalent is publishing the floor and the
negative-control split.

## 10. Assert identity in; never repair it back out

Two operations look similar and must be treated differently, because the
line between them is the line between measuring a product and grading your
own homework.

**Asserting a principal INTO the product is allowed, for every subject.**
Telling a governance layer who the caller is is what every real deployment
does. ACP's runner mints a per-user token and posts `agent_tier`,
`agent_name` and `agent_chain`. The OpenAI Agents SDK's equivalent is
`Runner.run(context=principal)`; AGT's is `AgentIdentity{sponsor}` in an
`IdentityRegistry`. These are the same operation and must be permitted
uniformly. A subject scored 1/6 because its adapter did not use the
identity carrier the SDK ships is being scored on adapter diligence.

**Repairing what the product did NOT record is harness evidence.** If the
runner reconstructs a field the product never wrote — by joining to the
scenario fixture, by inference, by lookup — that field is
`source="harness"` and the scorer strips it. The assertion then fails, and
that failure is the correct answer: it is a real product gap.

The test is not "did the runner touch this field" but **"if the product
were replaced by a no-op, would this value still appear?"** If yes, it is
the harness's answer, not the product's.

### How this rule was found

Every adapter was penalised for what it failed to read, while our own
column was credited for what our runner filled in:

- `pi_acp` overwrote `actor_uid` from the scenario fixture (ACP records
  `apikey:<keyId>`, not the uid) and left `source` at its `"product"`
  default, so the scorer counted a uid the gateway never wrote.
- The OpenAI adapter refused to pass `context=` on the explicit grounds
  that supplying identity "scores the subject on the runner's knowledge" —
  while the ACP adapter did exactly that and was scored for it.
- The AGT adapter discarded `PolicyDecisionResult.reason`, which AGT
  returns on every decision, and the writeup reported the absence as a
  product limit.

Three vendor engineers found this independently, from three directions, in
a few hours. The discrimination invariants did not: they test the scorer,
and all of these live in the drivers and adapters. A benchmark can be
rigorous about scoring and still be systematically biased by how carefully
each adapter was written — and the bias will point wherever the author's
attention was.
