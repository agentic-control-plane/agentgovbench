"""Discrimination tests — does the instrument measure anything?

A benchmark scenario is only worth scoring if a system with no governance
fails it. If an ungoverned system passes, the scenario is measuring
something other than governance: a broken assertion, a check that passes
on absent evidence, or a benign action where "allow" is simply correct.

Only the third case is legitimate, and it must be declared in the
scenario with ``negative_control: true``.

These tests run known-answer subjects — systems whose correct score we
know a priori — and assert the library discriminates. They are how we
tell the instrument is sound WITHOUT reference to how any real product
scores on it, which is the only way to avoid tuning the ruler to the
thing being measured.

Run: pytest tests/test_discrimination.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

from benchmark.loader import load_all
from benchmark.scorer import score_scenario
from benchmark.runner import RunnerMetadata, StatefulRunner
from benchmark.types import (
    Action,
    AuditEntry,
    DirectToolCall,
    ParallelFanOut,
    ToolOutcome,
    Wait,
)

SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"


# ── Known-answer subjects ──────────────────────────────────────────────


class NullRunner(StatefulRunner):
    """Does nothing at all. Reports no outcomes and no audit.

    Correct score: zero, including on negative controls (it cannot even
    show that the benign call was permitted, because it ran nothing).
    """

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(name="null", version="1.0", product="null-subject")

    def execute_action(self, action: Action):
        return None


class PermissiveRunner(StatefulRunner):
    """Allows everything, logs nothing, enforces nothing.

    This is what "no governance layer" looks like from the harness's
    point of view. Correct score: negative controls only.
    """

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(name="permissive", version="1.0",
                              product="no-governance-floor")

    def execute_action(self, action: Action):
        if isinstance(action, DirectToolCall):
            return self._allow(action.tool, action.input, action.as_user,
                               action.as_tenant, action.agent_tier, action.agent_name)
        if isinstance(action, ParallelFanOut):
            last = None
            for i in range(action.worker_count * action.calls_per_worker):
                last = self._allow(action.tool, action.input, action.as_user,
                                   action.as_tenant, action.agent_tier,
                                   f"worker-{i // action.calls_per_worker}")
            return last
        return None

    def _allow(self, tool, tool_input, uid, tenant, tier, agent_name) -> ToolOutcome:
        o = ToolOutcome(tool=tool, input=tool_input, as_user=uid, as_tenant=tenant,
                        allowed=True, reason=None, agent_tier=tier,
                        agent_name=agent_name)
        self._tool_outcomes.append(o)
        return o


class LiarRunner(PermissiveRunner):
    """Allows everything, then manufactures a perfect audit trail itself.

    This is the failure mode provenance enforcement exists to catch: an
    adapter that supplies the evidence the assertions are looking for.
    Because every record it emits is marked source="harness", the scorer
    must strip them and this must score exactly like PermissiveRunner.
    """

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(name="liar", version="1.0", product="fabricated-evidence")

    def _allow(self, tool, tool_input, uid, tenant, tier, agent_name) -> ToolOutcome:
        o = super()._allow(tool, tool_input, uid, tenant, tier, agent_name)
        self._audit.append(AuditEntry(
            timestamp="2026-01-01T00:00:00Z", tenant=tenant, actor_uid=uid,
            actor_email=f"{uid}@example.com", tool=tool, decision="allow",
            reason="fabricated by the runner", trace_id="trace-fake",
            delegation_chain=[agent_name] if agent_name else [],
            source="harness",
        ))
        return o


def _run(runner_cls) -> dict[str, bool]:
    """Return {scenario_id: passed} for a known-answer subject."""
    out: dict[str, bool] = {}
    for scn in load_all(SCENARIOS_DIR):
        r = runner_cls()
        r.setup(scn)
        for action in scn.actions:
            if isinstance(action, Wait):
                continue  # no real time needs to pass for known-answer subjects
            r.execute_action(action)
        res = score_scenario(scn, r.collect_outcome(), r.metadata.name, 0.0)
        r.teardown()
        out[scn.id] = res.passed
    return out


# ── The invariants ─────────────────────────────────────────────────────


def test_permissive_fails_every_non_negative_control():
    """THE core invariant.

    A scenario an ungoverned system passes measures nothing about
    governance. Either mark it negative_control: true (and accept that it
    contributes no discrimination), or fix it.
    """
    scenarios = {s.id: s for s in load_all(SCENARIOS_DIR)}
    results = _run(PermissiveRunner)
    offenders = sorted(
        sid for sid, passed in results.items()
        if passed and not scenarios[sid].negative_control
    )
    assert not offenders, (
        f"{len(offenders)} scenario(s) passed with NO governance in place. "
        "Each is either a broken assertion or an undeclared negative control:\n  "
        + "\n  ".join(offenders)
    )


def test_null_subject_scores_zero():
    """A subject that reports nothing cannot pass anything.

    Guards against checks that pass on absent evidence — the failure mode
    that silently rewards products with no audit log.
    """
    results = _run(NullRunner)
    passed = sorted(sid for sid, ok in results.items() if ok)
    assert not passed, (
        f"{len(passed)} scenario(s) passed for a runner that reported nothing at "
        "all. These checks treat absence of evidence as evidence of "
        "enforcement:\n  " + "\n  ".join(passed)
    )


def test_fabricated_evidence_does_not_help():
    """Provenance enforcement: a runner cannot score by supplying evidence.

    LiarRunner emits a flawless audit trail marked source="harness". It
    must score identically to the same runner emitting nothing.
    """
    permissive = _run(PermissiveRunner)
    liar = _run(LiarRunner)
    improved = sorted(sid for sid in liar if liar[sid] and not permissive[sid])
    assert not improved, (
        f"{len(improved)} scenario(s) were rescued by harness-manufactured "
        "evidence. The scorer is counting records the runner invented:\n  "
        + "\n  ".join(improved)
    )


def test_every_negative_control_is_declared_deliberately():
    """Negative controls must carry a rationale comment, not just a flag."""
    undocumented = []
    for scn in load_all(SCENARIOS_DIR):
        if not scn.negative_control:
            continue
        path = next(SCENARIOS_DIR.glob(f"{scn.category}/*{scn.id.split('.')[-1]}.yaml"), None)
        if path is None or "# Negative control:" not in path.read_text():
            undocumented.append(scn.id)
    assert not undocumented, (
        "negative_control: true without a '# Negative control: <why>' comment:\n  "
        + "\n  ".join(sorted(undocumented))
    )


# Scenarios a logging-only subject may legitimately pass. These genuinely
# test whether a record was written and what it contains — a system that
# logs correctly and enforces nothing SHOULD pass them, and that is the
# distinction the audit/identity/provenance categories exist to draw.
#
# Anything NOT on this list that audit_only passes is a hole: an
# enforcement claim that a subject with no enforcement satisfies.
#
# Membership is decided by what a scenario TESTS, not which directory it
# sits in. Two entries below live in enforcement categories but assert
# logging properties — whether tenant tags are correct, whether the tier
# was recorded — and a subject that logs those correctly should pass them.
# Both were verified adversarially: a tag-swapping subject and a
# tier-forgetting subject each fail, so the allowance is not a hole.
AUDIT_ONLY_MAY_PASS = {
    "audit_completeness.01_required_fields",
    "audit_completeness.04_trace_id_present",
    "audit_completeness.06_audit_covers_action_mix",
    "cross_tenant_isolation.02_audit_log_separation",
    "delegation_provenance.06_tier_recorded",
    "delegation_provenance.01_chain_recorded",
    "delegation_provenance.02_direct_call_no_chain",
    "delegation_provenance.03_three_hop_chain",
    "delegation_provenance.05_parallel_chains_distinct",
    "identity_propagation.01_direct_call_attribution",
    "identity_propagation.02_two_hop_delegation",
    "identity_propagation.03_multi_user_no_bleed",
    "identity_propagation.04_api_tier_attribution",
    "identity_propagation.05_anonymous_rejected",
    "identity_propagation.06_email_present",
}


def test_logging_without_enforcement_passes_only_logging_scenarios():
    """The second floor. Guards the hole the permissive floor cannot see.

    PermissiveRunner emits no audit at all, so every audit-shaped
    assertion fails against it for the wrong reason — absence of a log
    rather than absence of enforcement. That blind spot let five broken
    scenarios through: assertions that a subject which logs beautifully
    and enforces NOTHING satisfies.

    audit_only allows everything and produces a structurally complete
    audit trail. It must pass the scenarios that genuinely only test
    logging, and nothing else. A new name appearing here means an
    enforcement assertion was weakened into a logging assertion.
    """
    import importlib
    runner_cls = importlib.import_module("runners.audit_only").Runner

    scenarios = {s.id: s for s in load_all(SCENARIOS_DIR)}
    results = _run(runner_cls)
    leaked = sorted(
        sid for sid, ok in results.items()
        if ok
        and not scenarios[sid].negative_control
        and sid not in AUDIT_ONLY_MAY_PASS
    )
    assert not leaked, (
        f"{len(leaked)} scenario(s) passed for a subject that logs perfectly "
        "and enforces nothing. Each is an enforcement claim satisfied by an "
        "audit trail:\n  " + "\n  ".join(leaked)
    )


@pytest.mark.parametrize("category", sorted(
    {s.category for s in load_all(SCENARIOS_DIR)}
))
def test_category_retains_discriminating_power(category):
    """No category may consist entirely of negative controls."""
    scns = [s for s in load_all(SCENARIOS_DIR) if s.category == category]
    discriminating = [s for s in scns if not s.negative_control]
    assert discriminating, (
        f"category {category} has no discriminating scenarios — every one is a "
        "negative control, so the category measures nothing"
    )
