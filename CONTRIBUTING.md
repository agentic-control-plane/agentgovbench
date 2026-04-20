# Contributing to AgentGovBench

## Ways to contribute

1. **Add a runner** — represent your governance product in the scoreboard.
2. **Add scenarios** — extend the category libraries. We particularly want scenarios contributed by people who've been bitten by a specific failure mode in production.
3. **Revise assertions** — if a scenario is ambiguous or incorrect, open an issue with the scenario ID and your argument. Ambiguity is a spec bug.
4. **Propose a new category** — for a governance primitive we don't currently cover. Open an issue; discuss the threat model before writing scenarios.

## Adding a runner

Your runner represents your product in the scoreboard. The benchmark is only valuable if runners are faithful. Rules:

- **No internal or unreleased APIs.** Use only what's documented in your product's public surface.
- **No cherry-picked configuration.** Document any non-default config in the runner module's docstring.
- **No short-circuiting.** You may not synthesize audit entries that satisfy the assertion check without your product actually emitting them. Reviewers will find it.
- **Declared declinations.** If a category structurally does not apply to your product (e.g., single-tenant-only, no delegation model), declare it in `RunnerMetadata.declined_categories` with a one-line justification. Declining is fine; faking is not.

### Runner template

```python
# runners/my_product.py
"""Runner for <My Product>.

Non-default configuration used:
  - foo_enabled=true (default: false)
  - timeout_ms=5000 (default: 2000)
"""
from benchmark.runner import RunnerMetadata, StatefulRunner
from benchmark.types import Action, Scenario, ToolOutcome, AuditEntry


class Runner(StatefulRunner):
    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="my_product",
            version="1.2.3",
            product="My Product",
            vendor="example.com",
            notes="",
            declined_categories={},
        )

    def setup(self, scenario: Scenario) -> None:
        super().setup(scenario)
        # Install the scenario's tenants / users / tools / policies in
        # your product's backend so the scenario can run.

    def execute_action(self, action: Action) -> Optional[ToolOutcome]:
        # Route the action through your product. Record tool_outcomes
        # and audit entries on self._tool_outcomes / self._audit.
        ...
```

Test locally:

```bash
pip install -e .
python -m benchmark.cli run --runner my_product --verbose
```

Once your runner passes setup/teardown cleanly and runs the scenarios (whether passing or failing assertions), submit a PR with the runner + a published `results/my_product-vX.Y.Z.json` from `--out`.

### Reviewing a runner PR

Maintainers review for:

- Runner imports only the vendor's documented API
- No synthetic audit synthesis (must trace to actual product logs)
- Declared declined categories are structural, not convenience
- The results JSON reproduces when we run the runner locally

Non-reproducible PRs are closed. We will not publish results we cannot reproduce.

## Adding scenarios

### When to add

- A specific governance failure you've seen (or caught) in production
- A primitive the current library doesn't exercise
- A variation on an existing scenario that stresses a different edge

### Scenario anatomy

A scenario is a YAML file at `scenarios/<category>/NN_descriptive_name.yaml`. Numbering is for ordering / readability; no semantic meaning. Required fields:

```yaml
id: <category>.NN_descriptive_name     # unique; matches path
category: <category>                    # one of the 8 categories
version: 1                              # bump on breaking changes
nist: [ONE_OR_MORE, NIST, CONTROLS]    # list primary first
summary: "One-line description"
description: |
  Multi-line what-and-why. Why does this scenario matter? What kind
  of real bug would it catch? Who cares about the result?

setup:
  use_fixture: standard_tenant         # OR inline tenants/tools

actions:
  - direct_tool_call: { ... }
  - delegation: { ... }
  - parallel_fan_out: { ... }
  - gateway_failure: { ... }
  - policy_change: { ... }

expected:
  - kind: <assertion_kind>
    params: { ... }
```

### Scenario checklist

- [ ] Tests one thing well (not a kitchen sink)
- [ ] Includes at least one *positive* assertion (not only denials) if possible — a "deny everything" product should not pass a scenario by accident
- [ ] NIST controls mapped accurately (primary first)
- [ ] Description explains *why* — the threat model, the real bug it catches
- [ ] Uses a shared fixture when possible (keeps scenarios tight)

### Scenario review

Maintainers review for:

- Does the scenario's action sequence actually exercise the claimed category?
- Are the assertions correct — does a legitimate implementation pass?
- Is there ambiguity that invites subjective interpretation?
- Does it duplicate an existing scenario?

## Adding assertion kinds

New assertion kinds land in `benchmark/scorer.py`:

1. Add a handler function `_check_<name>(assertion, outcome, scenario) -> (passed, observed, note)`
2. Register in `CHECKS = {...}`
3. Document the expected `params` shape in the docstring

## Process for scenario disputes

If a vendor claims a scenario is ill-specified or their failure is an error:

1. Open a GitHub issue: *"Dispute: scenario X"*
2. Include the scenario ID, the claim, and any runnable reproduction
3. Maintainers review, possibly with third-party input
4. Resolution:
   - **Scenario bug:** scenario is revised, version incremented. Old results stand against the old version.
   - **Runner bug:** scenario stands; runner authors fix and resubmit.
   - **Ambiguity:** rewrite the scenario to be unambiguous; both original and revised results may be published to preserve context.

## Code of conduct

Be kind to contributors. Assume good faith. Reject cheating.

## License

All contributions are MIT-licensed. By contributing, you agree your contribution may be distributed under those terms.
