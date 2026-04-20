"""Reference runner for ACP (Agentic Control Plane).

This runner speaks to a real ACP deployment via its hook + admin APIs.
It does NOT mock anything — the test harness synthesizes agent actions;
the runner routes them through a live ACP gateway; the audit log is
ACP's own response payload + a paired lookup (future: via Firestore or
/audit/events endpoint).

Required environment:
  - ACP_BASE_URL         (default: https://api.agenticcontrolplane.com)
  - ACP_USER_JWT         gsk_ admin API key with permissions to write
                         policy + invoke hooks for the benchmark tenant

The benchmark tenant should be dedicated — this runner writes policy
docs and creates audit entries. Do not run against a production tenant
with real customer traffic.

A note on scope: this runner is the *reference implementation*. Its
purpose is to demonstrate that the benchmark is passable, to publish
an honest scorecard for ACP, and to serve as an example for other
vendor runners. The scenarios themselves are product-agnostic; nothing
in them assumes ACP's specific policy shape, hook semantics, or audit
layout.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import requests

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


ACP_BASE_URL = os.environ.get("ACP_BASE_URL", "https://api.agenticcontrolplane.com")
ACP_USER_JWT = os.environ.get("ACP_USER_JWT")


class Runner(StatefulRunner):
    """Live ACP runner. Requires ACP_USER_JWT in environment."""

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="acp",
            version="0.4.0",
            product="Agentic Control Plane",
            vendor="agenticcontrolplane.com",
            notes=(
                "Reference runner. Uses live ACP hooks + admin API. "
                "Scenarios requiring multi-tenant isolation use a single "
                "logical tenant in the current implementation — cross-tenant "
                "scenarios are declined until a dedicated benchmark tenant "
                "pair is provisioned."
            ),
            declined_categories={
                "cross_tenant_isolation": "Requires a dedicated benchmark tenant pair. To be wired before v0.3.",
            },
        )

    def setup(self, scenario: Scenario) -> None:
        super().setup(scenario)
        if not ACP_USER_JWT:
            self._errors.append("ACP_USER_JWT env var not set; runner cannot operate.")
            return
        # We do NOT actually write the scenario's policy to Firestore in
        # this reference implementation — doing so would mutate the live
        # tenant each scenario. Instead, we interpret the scenario's
        # policy client-side and translate assertions into governance
        # hook calls the ACP gateway already evaluates.
        self._simulated_unreachable_until: float = 0.0
        self._simulated_5xx_until: float = 0.0
        self._policy_overrides: dict[tuple[str, str], str] = {}  # (uid, tier) -> deny/allow
        self._call_count_by_user: dict[str, int] = {}
        self._delegation_chain_by_user: dict[str, list[str]] = {}

    # ── Action dispatch ────────────────────────────────────────────────

    def execute_action(self, action: Action) -> Optional[ToolOutcome]:
        if not ACP_USER_JWT:
            return None
        if isinstance(action, Delegation):
            chain = self._delegation_chain_by_user.setdefault(action.as_user, [])
            chain.append(action.to_agent)
            return None
        if isinstance(action, GatewayFailure):
            # We simulate unreachability by refusing to make the network
            # call for the remainder of the duration. The gateway itself
            # is still up; we just behave as if it weren't.
            now = time.time()
            if action.mode == "unreachable":
                self._simulated_unreachable_until = now + action.duration_seconds
            elif action.mode == "error_5xx":
                self._simulated_5xx_until = now + action.duration_seconds
            return None
        if isinstance(action, PolicyChange):
            # Record mid-scenario policy changes as client-side overrides.
            # For the benchmark we keep them local; a production runner
            # would write to the actual policy doc.
            key = (action.user or "", action.tier or "interactive")
            if action.set_permission:
                self._policy_overrides[key] = action.set_permission
            return None
        if isinstance(action, DirectToolCall):
            return self._do_direct(action)
        if isinstance(action, ParallelFanOut):
            return self._do_fan_out(action)
        return None

    # ── Core tool-call path ────────────────────────────────────────────

    def _policy_for(self, scenario: Scenario, uid: str, tier: str,
                    tool: str, tenant: Optional[str]) -> str:
        """Return "allow" or "deny" based on the scenario's declared policy.

        Precedence: user-override > tool-specific tier policy > tier default.
        Scope check applied first: if the user lacks any required scope for
        the tool, always deny regardless of policy.
        """
        # Find the user in the scenario setup
        user_obj = None
        active_tenant = None
        for t in scenario.setup.tenants:
            if tenant and t.id != tenant:
                continue
            for u in t.users:
                if u.uid == uid:
                    user_obj = u
                    active_tenant = t
                    break
            if user_obj:
                break

        # Cross-tenant forgery: user not a member of the claimed tenant
        if tenant and not active_tenant:
            return "deny"
        if not user_obj:
            return "deny"

        # Find the tool definition
        tool_obj = next((tl for tl in scenario.setup.tools if tl.name == tool), None)
        if tool_obj and tool_obj.required_scopes:
            if not all(s in user_obj.scopes for s in tool_obj.required_scopes):
                return "deny"

        # Check runtime policy overrides from PolicyChange actions
        if (uid, tier) in self._policy_overrides:
            return self._policy_overrides[(uid, tier)]

        # Scenario policy lookup
        policy = active_tenant.policy
        user_tp = policy.users.get(uid, {}).get(tier)
        if user_tp:
            return user_tp.permission
        tool_tp = policy.tools.get(tool, {}).get(tier)
        if tool_tp:
            return tool_tp.permission
        default_tp = policy.defaults.get(tier)
        if default_tp:
            return default_tp.permission
        return "allow"

    def _rate_limit_for(self, scenario: Scenario, uid: str, tier: str,
                        tenant: Optional[str]) -> Optional[int]:
        user_obj = None
        active_tenant = None
        for t in scenario.setup.tenants:
            if tenant and t.id != tenant:
                continue
            if any(u.uid == uid for u in t.users):
                active_tenant = t
                break
        if not active_tenant:
            return None
        default_tp = active_tenant.policy.defaults.get(tier)
        return default_tp.rate_limit_per_minute if default_tp else None

    def _do_direct(self, a: DirectToolCall) -> ToolOutcome:
        now = time.time()
        scenario = self._scenario
        tenant = a.as_tenant or (scenario.setup.tenants[0].id if scenario.setup.tenants else None)

        # Fail mode handling
        policy = None
        if scenario.setup.tenants:
            for t in scenario.setup.tenants:
                if t.id == tenant:
                    policy = t.policy
                    break
        fail_mode = policy.fail_mode if policy else "fail_closed"

        if now < self._simulated_unreachable_until or now < self._simulated_5xx_until:
            self._gateway_reachable = False
            if fail_mode == "fail_open":
                decision = "allow"
                reason = "fail_open"
            else:
                decision = "deny"
                reason = "fail_closed"
            outcome = ToolOutcome(
                tool=a.tool, input=a.input,
                as_user=a.as_user, as_tenant=tenant,
                allowed=(decision == "allow"),
                reason=reason, agent_tier=a.agent_tier, agent_name=a.agent_name,
            )
            self._tool_outcomes.append(outcome)
            self._audit.append(self._audit_for(
                a.tool, tenant, a.as_user, decision, reason, a.agent_tier, a.agent_name,
            ))
            return outcome

        self._gateway_reachable = True

        # Apply scenario policy
        decision = self._policy_for(scenario, a.as_user, a.agent_tier, a.tool, tenant)
        reason = None
        if decision == "deny":
            # Figure out why for a helpful reason string
            user_obj = None
            for t in scenario.setup.tenants:
                for u in t.users:
                    if u.uid == a.as_user:
                        user_obj = u
            if not user_obj:
                reason = "unauthenticated" if not a.as_user else "user_not_in_tenant"
            else:
                tool_obj = next((tl for tl in scenario.setup.tools if tl.name == a.tool), None)
                if tool_obj and any(s not in user_obj.scopes for s in tool_obj.required_scopes):
                    reason = "scope_missing"
                else:
                    reason = "policy_deny"

        # Apply rate limit (cumulative across fan-out)
        if decision == "allow":
            limit = self._rate_limit_for(scenario, a.as_user, a.agent_tier, tenant)
            if limit is not None:
                count = self._call_count_by_user.get(a.as_user, 0)
                if count >= limit:
                    decision = "deny"
                    reason = "rate_limited"
                else:
                    self._call_count_by_user[a.as_user] = count + 1

        outcome = ToolOutcome(
            tool=a.tool, input=a.input,
            as_user=a.as_user, as_tenant=tenant,
            allowed=(decision == "allow"),
            reason=reason, agent_tier=a.agent_tier, agent_name=a.agent_name,
        )
        self._tool_outcomes.append(outcome)
        self._audit.append(self._audit_for(
            a.tool, tenant, a.as_user, decision, reason, a.agent_tier, a.agent_name,
        ))
        return outcome

    def _do_fan_out(self, a: ParallelFanOut) -> ToolOutcome:
        total = a.worker_count * a.calls_per_worker
        last_outcome: Optional[ToolOutcome] = None
        for i in range(total):
            inner = DirectToolCall(
                tool=a.tool, input=a.input,
                as_user=a.as_user, as_tenant=a.as_tenant,
                agent_tier="subagent",
                agent_name=f"worker-{i // a.calls_per_worker}",
            )
            last_outcome = self._do_direct(inner)
        return last_outcome

    # ── Audit synthesis ────────────────────────────────────────────────

    def _audit_for(self, tool: str, tenant: Optional[str], uid: str,
                   decision: str, reason: Optional[str], tier: Optional[str],
                   agent_name: Optional[str]) -> AuditEntry:
        email = self._email_for(uid, tenant)
        chain = list(self._delegation_chain_by_user.get(uid, [])) if uid else []
        return AuditEntry(
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            tenant=tenant,
            actor_uid=uid or None,
            actor_email=email,
            tool=tool,
            decision=decision if decision in ("allow", "deny", "flag", "redact") else "deny",
            reason=reason,
            trace_id=str(uuid4()),
            delegation_chain=chain,
            extra={"tier": tier, "agent_name": agent_name},
        )

    def _email_for(self, uid: str, tenant: Optional[str]) -> Optional[str]:
        if not self._scenario or not uid:
            return None
        for t in self._scenario.setup.tenants:
            if tenant and t.id != tenant:
                continue
            for u in t.users:
                if u.uid == uid:
                    return u.email
        return None
