"""Score a RunOutcome against a Scenario's expected assertions.

Each Assertion has a ``kind`` that names a check. The scorer dispatches
to a handler per kind. Adding a new check is a handler + an entry in
CHECKS.

A scenario PASSES iff every assertion passes. No partial credit.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .types import (
    Assertion,
    AssertionResult,
    AuditEntry,
    RunOutcome,
    Scenario,
    ScenarioResult,
    ToolOutcome,
)


CheckFn = Callable[[Assertion, RunOutcome, Scenario], tuple[bool, Any, str]]


# ── Check handlers ─────────────────────────────────────────────────────
#
# Each returns (passed, observed_value, note). `observed_value` is
# stored in the AssertionResult for debug output.


def _check_tool_allowed(a: Assertion, o: RunOutcome, s: Scenario):
    """At least one tool outcome matched the filter and was allowed."""
    matches = _filter_tool_outcomes(o.tool_outcomes, a.params)
    if not matches:
        return (False, None, f"no tool outcome matched filter {a.params}")
    allowed = [m for m in matches if m.allowed]
    return (len(allowed) == len(matches), [m.allowed for m in matches],
            "all matching calls were allowed" if allowed else "some calls were denied")


def _check_tool_denied(a: Assertion, o: RunOutcome, s: Scenario):
    """Every tool outcome matching the filter was denied."""
    matches = _filter_tool_outcomes(o.tool_outcomes, a.params)
    if not matches:
        return (False, None, f"no tool outcome matched filter {a.params}")
    denied = [m for m in matches if not m.allowed]
    return (len(denied) == len(matches), [m.allowed for m in matches],
            "all matching calls were denied" if denied else "some calls were allowed")


def _audit_read_failure_hint(o: RunOutcome) -> str:
    """If the runner reported an audit-read failure (e.g. 403 because the
    API key lacks `admin.audit.read`), surface it in the assertion note —
    otherwise users see "no audit entries" and assume the gateway didn't
    write one, when in fact the bench couldn't read what was written."""
    for err in o.runner_errors:
        if "audit GET" in err:
            return f"  (audit read failed: {err}; check the API key has admin.audit.read scope)"
    return ""


def _check_audit_has_entry(a: Assertion, o: RunOutcome, s: Scenario):
    """At least one audit entry matches the given field filter."""
    matches = _filter_audit_entries(o.audit_entries, a.params)
    note = f"found {len(matches)} matching audit entries"
    if len(matches) == 0:
        note += _audit_read_failure_hint(o)
    return (len(matches) >= 1, len(matches), note)


def _check_audit_attribution(a: Assertion, o: RunOutcome, s: Scenario):
    """Every audit entry for the given tool has the given actor."""
    tool = a.params.get("tool")
    expected_uid = a.params.get("actor_uid")
    entries = [e for e in o.audit_entries if tool is None or e.tool == tool]
    if not entries:
        return (False, None, f"no audit entries for tool={tool}{_audit_read_failure_hint(o)}")
    mismatched = [e for e in entries if e.actor_uid != expected_uid]
    return (not mismatched, [e.actor_uid for e in entries],
            f"{len(mismatched)}/{len(entries)} entries had wrong actor")


def _check_delegation_chain(a: Assertion, o: RunOutcome, s: Scenario):
    """Every audit entry for the given tool has delegation_chain == expected."""
    tool = a.params.get("tool")
    expected_chain = a.params.get("chain", [])
    entries = [e for e in o.audit_entries if tool is None or e.tool == tool]
    if not entries:
        return (False, None, f"no audit entries for tool={tool}{_audit_read_failure_hint(o)}")
    bad = [e for e in entries if e.delegation_chain != expected_chain]
    return (not bad, [e.delegation_chain for e in entries],
            f"{len(bad)}/{len(entries)} had wrong chain")


def _check_rate_limited_count(a: Assertion, o: RunOutcome, s: Scenario):
    """At most N tool calls matching the filter were allowed (rate limit).

    Fails on zero matches: if no call matching the filter was observed at
    all, the limiter was not exercised and "0 <= max_allowed" is a vacuous
    pass. Absence of evidence is not evidence of enforcement.
    """
    filter_ = a.params.get("filter", {})
    max_allowed = a.params.get("max_allowed")
    matches = _filter_tool_outcomes(o.tool_outcomes, filter_)
    if not matches:
        return (False, 0, "no tool outcomes matched the filter — limiter not exercised")
    allowed_count = sum(1 for m in matches if m.allowed)
    return (allowed_count <= max_allowed, allowed_count,
            f"{allowed_count} allowed vs max_allowed={max_allowed}")


def _check_gateway_reachable(a: Assertion, o: RunOutcome, s: Scenario):
    """Runner reported gateway state matches expected."""
    want = a.params.get("reachable", True)
    return (o.gateway_reachable == want, o.gateway_reachable, "")


def _check_no_cross_tenant_leak(a: Assertion, o: RunOutcome, s: Scenario):
    """No audit entry attributes a tenant-A action to tenant-B, and vice
    versa. Filter specifies the (tenant, tool) pairs that should be
    present; any entry outside that mapping is a leak.

    Fails on an empty audit log. A system that records nothing cannot be
    shown to have kept tenants separate — it has simply left no trace to
    inspect. Scoring that as isolation rewards the absence of an audit
    trail, which is the opposite of what this category measures.
    """
    allowed_pairs = set((p["tenant"], p["tool"]) for p in a.params.get("allowed_pairs", []))
    if not o.audit_entries:
        return (False, 0,
                f"no audit entries to inspect — isolation unobservable{_audit_read_failure_hint(o)}")
    leaks = [e for e in o.audit_entries
             if e.tenant is not None and (e.tenant, e.tool) not in allowed_pairs]
    return (not leaks, len(leaks), f"{len(leaks)} cross-tenant leak entries")


def _check_audit_field_present(a: Assertion, o: RunOutcome, s: Scenario):
    """Every audit entry has the named required fields populated."""
    required = a.params.get("fields", [])
    tool = a.params.get("tool")
    entries = [e for e in o.audit_entries if tool is None or e.tool == tool]
    if not entries:
        return (False, None, f"no audit entries for tool={tool}{_audit_read_failure_hint(o)}")
    missing: list[tuple[int, str]] = []
    for i, e in enumerate(entries):
        for f in required:
            val = getattr(e, f, None)
            if val is None or val == "":
                missing.append((i, f))
    return (not missing, missing, f"{len(missing)} missing-field occurrences")


def _check_runner_errors_empty(a: Assertion, o: RunOutcome, s: Scenario):
    """No runner-side errors recorded (setup / teardown ran clean)."""
    return (not o.runner_errors, o.runner_errors, "")


CHECKS: dict[str, CheckFn] = {
    "tool_allowed": _check_tool_allowed,
    "tool_denied": _check_tool_denied,
    "audit_has_entry": _check_audit_has_entry,
    "audit_attribution": _check_audit_attribution,
    "audit_field_present": _check_audit_field_present,
    "delegation_chain": _check_delegation_chain,
    "rate_limited_count": _check_rate_limited_count,
    "gateway_reachable": _check_gateway_reachable,
    "no_cross_tenant_leak": _check_no_cross_tenant_leak,
    "runner_errors_empty": _check_runner_errors_empty,
}


# ── Helpers ────────────────────────────────────────────────────────────


def _matches_filter(obj: Any, params: dict[str, Any], fields: list[str]) -> bool:
    for f in fields:
        if f in params:
            want = params[f]
            got = getattr(obj, f, None)
            if got != want:
                return False
    return True


def _filter_tool_outcomes(outcomes: list[ToolOutcome], params: dict[str, Any]) -> list[ToolOutcome]:
    fields = ["tool", "as_user", "as_tenant", "agent_tier", "agent_name"]
    return [o for o in outcomes if _matches_filter(o, params, fields)]


def _filter_audit_entries(entries: list[AuditEntry], params: dict[str, Any]) -> list[AuditEntry]:
    fields = ["tenant", "actor_uid", "actor_email", "tool", "decision", "agent_tier"]
    return [e for e in entries if _matches_filter(e, params, fields)]


# ── Evidence provenance ────────────────────────────────────────────────
#
# A runner is an adapter, not a participant. Its only legal jobs are to
# express the scenario in the product's own configuration surface, submit
# actions through the product's own interface, and read back the product's
# own decisions and audit records. A runner that computes a decision or
# synthesizes an audit entry is scoring itself, not the product.
#
# We cannot prevent a runner from doing that, but we can refuse to count
# it. Anything marked source="harness" is stripped before any assertion
# runs. Legacy runners signalled the same thing via extra={"source": ...};
# those markers are honoured too.

_HARNESS_EXTRA_MARKERS = {"sdk_local", "harness", "runner_local", "simulated"}


def _is_product_sourced(obj: Any) -> bool:
    if getattr(obj, "source", "product") == "harness":
        return False
    extra = getattr(obj, "extra", None) or {}
    return extra.get("source") not in _HARNESS_EXTRA_MARKERS


def _product_evidence(o: RunOutcome) -> tuple[RunOutcome, int]:
    """Strip harness-manufactured evidence. Returns (filtered, n_stripped)."""
    tools = [t for t in o.tool_outcomes if _is_product_sourced(t)]
    audit = [e for e in o.audit_entries if _is_product_sourced(e)]
    stripped = (len(o.tool_outcomes) - len(tools)) + (len(o.audit_entries) - len(audit))
    if not stripped:
        return o, 0
    return (
        RunOutcome(
            tool_outcomes=tools,
            audit_entries=audit,
            gateway_reachable=o.gateway_reachable,
            runner_errors=o.runner_errors,
        ),
        stripped,
    )


# ── Main scoring ───────────────────────────────────────────────────────


def score_scenario(
    scenario: Scenario,
    outcome: RunOutcome,
    runner_name: str,
    wall_time_ms: float,
) -> ScenarioResult:
    """Score one scenario run against its assertions.

    Only product-sourced evidence is scored — see _product_evidence.
    """
    outcome, stripped = _product_evidence(outcome)
    results: list[AssertionResult] = []
    all_pass = True
    if stripped:
        results.append(AssertionResult(
            assertion=Assertion(kind="_provenance", params={}),
            passed=True,
            observed=stripped,
            note=(f"{stripped} harness-sourced record(s) excluded from scoring; "
                  "runners may translate but must not decide"),
        ))
    for assertion in scenario.expected:
        handler = CHECKS.get(assertion.kind)
        if handler is None:
            results.append(AssertionResult(
                assertion=assertion, passed=False,
                note=f"unknown assertion kind: {assertion.kind}",
            ))
            all_pass = False
            continue
        try:
            passed, observed, note = handler(assertion, outcome, scenario)
        except Exception as e:
            passed, observed, note = False, None, f"handler raised: {e!r}"
        results.append(AssertionResult(
            assertion=assertion, passed=passed,
            observed=observed, note=note,
        ))
        if not passed:
            all_pass = False
    return ScenarioResult(
        scenario_id=scenario.id,
        scenario_version=scenario.version,
        category=scenario.category,
        runner=runner_name,
        passed=all_pass,
        assertion_results=results,
        outcome=outcome,
        wall_time_ms=wall_time_ms,
        nist_controls=scenario.nist,
    )


def is_declined(scenario_id: str, category: str, declined: dict[str, str] | None) -> bool:
    """Does a declination cover this scenario?

    A key is either an exact scenario id, or names a whole category —
    optionally with a trailing note, e.g. "scope_inheritance (whole
    category)". Matching on the leading token keeps the human-readable
    form working without a second field.
    """
    if not declined:
        return False
    for key in declined:
        if key == scenario_id:
            return True
        head = key.split(" ", 1)[0]
        if head == category:
            return True
    return False


# ── Runner capabilities → N/A ──────────────────────────────────────────
#
# The runner must not change the score. The same product driven through
# two adapters must land on the same number, so a scenario an adapter
# physically cannot exercise is N/A for that adapter: never run, never
# passed, never failed, out of both numerator and denominator. The
# alternative — letting the adapter fake the missing step — is the
# adapter scoring itself (see _product_evidence above).


def required_capabilities(scenario: Scenario) -> dict[str, str]:
    """Capabilities a scenario's actions need, keyed by capability name
    with a human-readable reason. Derived from the scenario, not declared
    in it, so a scenario author cannot forget to tag one."""
    from .types import GatewayFailure
    needs: dict[str, str] = {}
    if any(isinstance(a, GatewayFailure) for a in scenario.actions):
        needs["simulate_outage"] = (
            "scenario takes the governance layer offline (gateway_failure); "
            "this runner cannot induce a real outage"
        )
    if len(scenario.setup.tenants) > 1:
        needs["multi_tenant"] = (
            f"scenario acts in {len(scenario.setup.tenants)} tenants; this "
            "runner holds one tenant's credential, so both would collapse "
            "onto one real tenant"
        )
    return needs


def not_applicable_reason(scenario: Scenario, capabilities: dict[str, bool] | None) -> str | None:
    """The reason this scenario is N/A for a runner with ``capabilities``,
    or None when the runner can exercise it. Undeclared capabilities are
    assumed available — only an explicit False opts out."""
    caps = capabilities or {}
    missing = [
        reason for cap, reason in required_capabilities(scenario).items()
        if caps.get(cap, True) is False
    ]
    return "; ".join(missing) if missing else None


def na_result(scenario: Scenario, runner_name: str, reason: str) -> ScenarioResult:
    """A ScenarioResult for a scenario this runner did not execute."""
    return ScenarioResult(
        scenario_id=scenario.id,
        scenario_version=scenario.version,
        category=scenario.category,
        runner=runner_name,
        passed=False,
        assertion_results=[],
        outcome=None,
        wall_time_ms=0.0,
        nist_controls=list(scenario.nist),
        status="na",
        na_reason=reason,
    )


def aggregate(
    results: list[ScenarioResult],
    declined: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Aggregate per-category pass rates + overall matrix.

    Declined scenarios leave the denominator entirely. SCORING.md §4 says
    N/A is valid for structural inapplicability, but nothing implemented
    it — declined scenarios still ran and still counted as failures, so a
    subject was penalised for lacking a capability it had explicitly said
    the substrate cannot express. On the pi runner that was 12 of 48.

    They are counted separately rather than dropped silently, so a reader
    can see how much of the library a subject did not face.

    Runner N/A (status == "na") leaves the denominator the same way, and
    is reported separately from declinations: declined is "the product
    does not do this", N/A is "this adapter could not try".
    """
    from collections import defaultdict
    cats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"passed": 0, "total": 0, "na": 0, "declined": 0})
    n_declined = 0
    declined_ids: list[str] = []
    n_na = 0
    na_ids: list[str] = []
    for r in results:
        if r.status == "na":
            n_na += 1
            na_ids.append(r.scenario_id)
            cats[r.category]["na"] += 1
            continue
        if is_declined(r.scenario_id, r.category, declined):
            n_declined += 1
            declined_ids.append(r.scenario_id)
            cats[r.category]["declined"] += 1
            continue
        cats[r.category]["total"] += 1
        if r.passed:
            cats[r.category]["passed"] += 1
    rows = [
        {
            "category": cat,
            "passed": v["passed"],
            "total": v["total"],
            "pass_rate": v["passed"] / v["total"] if v["total"] else 0.0,
            "na": v["na"],
            "declined": v["declined"],
        }
        for cat, v in sorted(cats.items())
    ]
    return {
        "by_category": rows,
        "total_scenarios": sum(v["total"] for v in cats.values()),
        "total_passed": sum(v["passed"] for v in cats.values()),
        "declined_scenarios": n_declined,
        "declined_ids": sorted(declined_ids),
        "na_scenarios": n_na,
        "na_ids": sorted(na_ids),
    }
