"""Audit-only runner — "logging without enforcement."

Represents a common operating mode: the framework emits decent audit
records for every tool call (who, what, when, with what chain) but
enforces nothing — every call is allowed, no scopes checked, no rate
limits applied. This is what you get from:

  - Claude Code / Claude Agent SDK default behavior, minus the
    interactive permission prompt (which we simulate as always-approve)
  - OpenAI Agents SDK with tracing enabled (no guardrails)
  - LangChain with a callback handler logging tool calls
  - A lot of CrewAI deployments that wired logging but not enforcement
  - Any agent framework whose team hasn't gotten to governance yet

The audit it produces is *structurally* complete: timestamp, actor UID,
tool name, decision, reason, trace ID, delegation chain. But every
decision is "allow", so the enforcement-style scenarios fail. Passes:
attribution, provenance, audit completeness, basic fail-mode. Fails:
anything requiring a deny or rate-limit response.

This runner is useful as the MIDDLE TIER between vanilla (0/48) and a
real governance product — answering the question "what do I get for
free from my framework?"
"""
from __future__ import annotations

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
    """Audit everything, enforce nothing. Represents framework defaults."""

    def __init__(self) -> None:
        super().__init__()
        self._chain_by_agent: dict[str, list[str]] = {}

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="audit_only",
            version="0.1.0",
            product="framework-default-baseline",
            vendor=None,
            notes=(
                "Represents the common 'log everything, enforce nothing' mode "
                "most agent frameworks ship by default. Audit records are "
                "structurally complete (attribution, provenance, timestamps); "
                "no denials, no rate limits, no scope checking."
            ),
            declined_categories={
                "cross_tenant_isolation": (
                    "Framework defaults don't model multi-tenancy. A single-"
                    "user agent framework has no tenant concept to isolate."
                ),
            },
        )

    def setup(self, scenario: Scenario) -> None:
        super().setup(scenario)
        self._chain_by_agent = {}

    def execute_action(self, action: Action) -> Optional[ToolOutcome]:
        if isinstance(action, Delegation):
            base = list(self._chain_by_agent.get(
                action.from_agent, [action.from_agent],
            ))
            self._chain_by_agent[action.to_agent] = base + [action.to_agent]
            return None
        if isinstance(action, (GatewayFailure, PolicyChange)):
            # No gateway to fail, no policy to change.
            return None
        if isinstance(action, DirectToolCall):
            return self._do_direct(action)
        if isinstance(action, ParallelFanOut):
            last: Optional[ToolOutcome] = None
            total = action.worker_count * action.calls_per_worker
            for i in range(total):
                inner = DirectToolCall(
                    tool=action.tool, input=action.input,
                    as_user=action.as_user, as_tenant=action.as_tenant,
                    agent_tier="subagent",
                    agent_name=f"worker-{i // action.calls_per_worker}",
                )
                last = self._do_direct(inner)
            return last
        return None

    def _do_direct(self, a: DirectToolCall) -> ToolOutcome:
        # Every call allowed. Audit emitted with full structural content.
        tenant = a.as_tenant or (
            self._scenario.setup.tenants[0].id if self._scenario and self._scenario.setup.tenants else None
        )
        email = self._email_for(a.as_user)
        chain = list(self._chain_by_agent.get(a.agent_name, [])) if a.agent_name else []
        outcome = ToolOutcome(
            tool=a.tool, input=a.input,
            as_user=a.as_user, as_tenant=tenant,
            allowed=True, reason="audit_only_allows_all",
            agent_tier=a.agent_tier, agent_name=a.agent_name,
        )
        self._tool_outcomes.append(outcome)
        # A faithful audit-only runner DOES refuse anonymous calls — if
        # there's no user identity, even a log-only system can't attribute
        # anything. Returning a denial here matches what any framework
        # with a usable audit log would do.
        if not a.as_user:
            outcome.allowed = False
            outcome.reason = "unauthenticated (no user identity to attribute)"
            return outcome
        self._audit.append(AuditEntry(
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            tenant=tenant,
            actor_uid=a.as_user,
            actor_email=email,
            tool=a.tool,
            decision="allow",
            reason="audit_only_allows_all",
            trace_id=str(uuid4()),
            delegation_chain=chain,
            extra={"tier": a.agent_tier, "agent_name": a.agent_name,
                   "source": "framework_default"},
        ))
        return outcome

    def _email_for(self, uid: str) -> Optional[str]:
        if not self._scenario or not uid:
            return None
        for t in self._scenario.setup.tenants:
            for u in t.users:
                if u.uid == uid:
                    return u.email
        return None
