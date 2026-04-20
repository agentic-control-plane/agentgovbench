"""The vanilla runner — no governance at all.

This is the baseline any governance product must improve upon. It:
  - Allows every tool call
  - Attributes audit entries correctly (because we synthesize them from
    the action's as_user / as_tenant fields) — this is cheating in a
    sense, because a real no-governance system wouldn't have an audit
    log at all, but we want to show that vanilla fails on enforcement
    scenarios even when attribution is free
  - Never rate-limits
  - Ignores gateway_failure actions (no governance, no failure to model)
  - Ignores policy_change actions

The scorecard against vanilla is the "how bad is unprotected" number.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from benchmark.runner import RunnerMetadata, StatefulRunner
from benchmark.types import (
    Action,
    AuditEntry,
    DirectToolCall,
    Delegation,
    GatewayFailure,
    ParallelFanOut,
    PolicyChange,
    Scenario,
    ToolOutcome,
)


class Runner(StatefulRunner):
    """Cheap, deterministic baseline. All calls allowed; audit synthesized."""

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="vanilla",
            version="0.2.0",
            product="no-governance-baseline",
            vendor=None,
            notes=(
                "Synthesized audit entries make this runner artificially generous "
                "on attribution/provenance categories. All enforcement-style "
                "assertions will legitimately fail."
            ),
        )

    def setup(self, scenario: Scenario) -> None:
        super().setup(scenario)
        # Track delegation chain per user so provenance-style scenarios can
        # at least surface what vanilla "sees" (even if it doesn't enforce).
        self._delegation_chain: list[str] = []

    def execute_action(self, action: Action) -> Optional[ToolOutcome]:
        if isinstance(action, Delegation):
            # Track chain so provenance-style scenarios produce meaningful
            # (but unenforced) data.
            self._delegation_chain.append(action.to_agent)
            return None
        if isinstance(action, GatewayFailure):
            return None  # vanilla has no gateway to fail
        if isinstance(action, PolicyChange):
            return None  # vanilla has no policy
        if isinstance(action, DirectToolCall):
            return self._do_direct(action)
        if isinstance(action, ParallelFanOut):
            return self._do_fan_out(action)
        return None

    # ── Helpers ────────────────────────────────────────────────────────

    def _audit_for(self, tool: str, tenant: Optional[str], uid: str,
                   decision: str, reason: Optional[str], tier: Optional[str],
                   agent_name: Optional[str]) -> AuditEntry:
        email = self._email_for(uid)
        return AuditEntry(
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            tenant=tenant,
            actor_uid=uid or None,
            actor_email=email,
            tool=tool,
            decision=decision,
            reason=reason,
            trace_id=str(uuid4()),
            delegation_chain=list(self._delegation_chain),
            extra={"tier": tier, "agent_name": agent_name},
        )

    def _email_for(self, uid: str) -> Optional[str]:
        if not self._scenario or not uid:
            return None
        for t in self._scenario.setup.tenants:
            for u in t.users:
                if u.uid == uid:
                    return u.email
        return None

    def _do_direct(self, a: DirectToolCall) -> ToolOutcome:
        # "No governance" = every call is allowed. If the as_user is empty,
        # we still allow it — that's the whole point of the baseline.
        tenant = a.as_tenant or (
            self._scenario.setup.tenants[0].id if self._scenario and self._scenario.setup.tenants else None
        )
        outcome = ToolOutcome(
            tool=a.tool, input=a.input,
            as_user=a.as_user, as_tenant=tenant,
            allowed=True, reason="vanilla_allows_all",
            agent_tier=a.agent_tier, agent_name=a.agent_name,
        )
        self._tool_outcomes.append(outcome)
        self._audit.append(self._audit_for(
            a.tool, tenant, a.as_user, "allow", "vanilla_allows_all",
            a.agent_tier, a.agent_name,
        ))
        return outcome

    def _do_fan_out(self, a: ParallelFanOut) -> ToolOutcome:
        tenant = a.as_tenant or (
            self._scenario.setup.tenants[0].id if self._scenario and self._scenario.setup.tenants else None
        )
        total = a.worker_count * a.calls_per_worker
        for i in range(total):
            outcome = ToolOutcome(
                tool=a.tool, input=a.input,
                as_user=a.as_user, as_tenant=tenant,
                allowed=True, reason="vanilla_allows_all",
                agent_tier=None,
                agent_name=f"fanout-{i // a.calls_per_worker}",
            )
            self._tool_outcomes.append(outcome)
            self._audit.append(self._audit_for(
                a.tool, tenant, a.as_user, "allow", None, None, outcome.agent_name,
            ))
        return outcome  # last one (mostly for compatibility)
