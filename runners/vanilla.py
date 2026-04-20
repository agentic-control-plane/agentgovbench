"""Vanilla — the no-governance floor.

A governance product that doesn't beat this isn't a governance product.

vanilla:
  - Allows every tool call
  - Emits NO audit entries (there's no governance layer to write them)
  - Does not enforce rate limits
  - Ignores policy changes (no policy to change)
  - Ignores fail-mode directives (nothing to fail)

Expected scorecard: almost all scenarios fail because they assert
governance happened. The identity_propagation, per_user_policy_enforcement,
delegation_provenance, audit_completeness, rate_limit_cascade, and
fail_mode_discipline categories will be at or near zero. Scope_inheritance
is partially N/A — a couple of its baseline scenarios (benign calls that
should be allowed) legitimately pass.

A product that clears this baseline is doing something. A product whose
scorecard merely matches vanilla is selling you a log viewer.
"""
from __future__ import annotations

from typing import Optional

from benchmark.runner import RunnerMetadata, StatefulRunner
from benchmark.types import (
    Action,
    DirectToolCall,
    Delegation,
    GatewayFailure,
    ParallelFanOut,
    PolicyChange,
    Scenario,
    ToolOutcome,
)


class Runner(StatefulRunner):
    """No-governance baseline. Never denies, never logs."""

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="vanilla",
            version="0.2.1",
            product="no-governance-floor",
            vendor=None,
            notes=(
                "Represents running an agent framework with no governance "
                "layer at all: every call allowed, no audit log produced, "
                "no rate limits. Intended as the lower bound for comparison."
            ),
        )

    def execute_action(self, action: Action) -> Optional[ToolOutcome]:
        # Every "real" action becomes an allowed tool call with no audit.
        # Delegation/GatewayFailure/PolicyChange are no-ops since vanilla
        # has no governance to affect.
        if isinstance(action, DirectToolCall):
            return self._allow(action.tool, action.input, action.as_user,
                               action.as_tenant, action.agent_tier, action.agent_name)
        if isinstance(action, ParallelFanOut):
            total = action.worker_count * action.calls_per_worker
            last: Optional[ToolOutcome] = None
            for i in range(total):
                last = self._allow(action.tool, action.input, action.as_user,
                                   action.as_tenant, "subagent",
                                   f"worker-{i // action.calls_per_worker}")
            return last
        return None

    def _allow(self, tool: str, tool_input: dict, uid: str, tenant: Optional[str],
               tier: Optional[str], agent_name: Optional[str]) -> ToolOutcome:
        outcome = ToolOutcome(
            tool=tool, input=tool_input,
            as_user=uid, as_tenant=tenant,
            allowed=True, reason=None,
            agent_tier=tier, agent_name=agent_name,
        )
        self._tool_outcomes.append(outcome)
        # NO audit entry emitted — vanilla has no audit layer.
        return outcome
