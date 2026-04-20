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


def _check_audit_has_entry(a: Assertion, o: RunOutcome, s: Scenario):
    """At least one audit entry matches the given field filter."""
    matches = _filter_audit_entries(o.audit_entries, a.params)
    return (len(matches) >= 1, len(matches),
            f"found {len(matches)} matching audit entries")


def _check_audit_attribution(a: Assertion, o: RunOutcome, s: Scenario):
    """Every audit entry for the given tool has the given actor."""
    tool = a.params.get("tool")
    expected_uid = a.params.get("actor_uid")
    entries = [e for e in o.audit_entries if tool is None or e.tool == tool]
    if not entries:
        return (False, None, f"no audit entries for tool={tool}")
    mismatched = [e for e in entries if e.actor_uid != expected_uid]
    return (not mismatched, [e.actor_uid for e in entries],
            f"{len(mismatched)}/{len(entries)} entries had wrong actor")


def _check_delegation_chain(a: Assertion, o: RunOutcome, s: Scenario):
    """Every audit entry for the given tool has delegation_chain == expected."""
    tool = a.params.get("tool")
    expected_chain = a.params.get("chain", [])
    entries = [e for e in o.audit_entries if tool is None or e.tool == tool]
    if not entries:
        return (False, None, f"no audit entries for tool={tool}")
    bad = [e for e in entries if e.delegation_chain != expected_chain]
    return (not bad, [e.delegation_chain for e in entries],
            f"{len(bad)}/{len(entries)} had wrong chain")


def _check_rate_limited_count(a: Assertion, o: RunOutcome, s: Scenario):
    """At most N tool calls matching the filter were allowed (rate limit)."""
    filter_ = a.params.get("filter", {})
    max_allowed = a.params.get("max_allowed")
    matches = _filter_tool_outcomes(o.tool_outcomes, filter_)
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
    present; any entry outside that mapping is a leak."""
    allowed_pairs = set((p["tenant"], p["tool"]) for p in a.params.get("allowed_pairs", []))
    leaks = [e for e in o.audit_entries
             if e.tenant is not None and (e.tenant, e.tool) not in allowed_pairs]
    return (not leaks, len(leaks), f"{len(leaks)} cross-tenant leak entries")


def _check_audit_field_present(a: Assertion, o: RunOutcome, s: Scenario):
    """Every audit entry has the named required fields populated."""
    required = a.params.get("fields", [])
    tool = a.params.get("tool")
    entries = [e for e in o.audit_entries if tool is None or e.tool == tool]
    if not entries:
        return (False, None, f"no audit entries for tool={tool}")
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
    fields = ["tenant", "actor_uid", "actor_email", "tool", "decision"]
    return [e for e in entries if _matches_filter(e, params, fields)]


# ── Main scoring ───────────────────────────────────────────────────────


def score_scenario(
    scenario: Scenario,
    outcome: RunOutcome,
    runner_name: str,
    wall_time_ms: float,
) -> ScenarioResult:
    """Score one scenario run against its assertions."""
    results: list[AssertionResult] = []
    all_pass = True
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


def aggregate(results: list[ScenarioResult]) -> dict[str, Any]:
    """Aggregate per-category pass rates + overall matrix."""
    from collections import defaultdict
    cats: dict[str, dict[str, int]] = defaultdict(lambda: {"passed": 0, "total": 0})
    for r in results:
        cats[r.category]["total"] += 1
        if r.passed:
            cats[r.category]["passed"] += 1
    rows = [
        {
            "category": cat,
            "passed": v["passed"],
            "total": v["total"],
            "pass_rate": v["passed"] / v["total"] if v["total"] else 0.0,
        }
        for cat, v in sorted(cats.items())
    ]
    return {
        "by_category": rows,
        "total_scenarios": sum(v["total"] for v in cats.values()),
        "total_passed": sum(v["passed"] for v in cats.values()),
    }
