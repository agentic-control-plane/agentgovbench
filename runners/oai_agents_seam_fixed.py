"""OpenAI Agents SDK seam — corrected integration.

Same BYO policy engine as `oai_agents_seam` (identical most-specific-wins
precedence). The ONLY differences are integration-level: this runner uses
the SDK mechanisms the original adapter left unused.

  * Runner.run(context=Principal(...))  — the SDK threads a caller-supplied
    context object through every guardrail, hook and tool. This is how a
    principal travels. run_context.py:72-80, tool_context.py:42.
  * RunHooks — on_tool_start / on_handoff receive that context plus the
    live agent. lifecycle.py:61-103.
  * trace(metadata=...) / RunConfig(trace_metadata=...) — trace-level
    metadata is inherited by EVERY span via Span.trace_metadata.
    spans.py:211, create.py:31-75.
  * needs_approval (callable) — per-call policy predicate receiving
    (run_context, tool_params, call_id). tool.py:486-493.
  * TierPolicy.rate_limit_per_minute — already parsed out of the scenario
    by the original runner's _build_policy, then discarded.

Timestamps and trace ids on audit rows are read off the SDK's own live
span (tracing.get_current_span()), not manufactured.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from benchmark.runner import RunnerMetadata, StatefulRunner
from benchmark.types import (
    Action, AuditEntry, DirectToolCall, Delegation, GatewayFailure,
    ParallelFanOut, PolicyChange, Scenario, ToolOutcome, Wait,
)

WITH_LIMITER = os.environ.get("AGB_OAI_LIMITER", "1") == "1"


@dataclass
class Principal:
    """The caller. Handed to Runner.run(context=...); the SDK threads it."""
    actor_uid: str
    actor_email: Optional[str]
    tenant: Optional[str]
    agent_tier: str
    agent_name: Optional[str] = None
    delegated_scopes: Optional[set] = None
    chain: list = field(default_factory=list)   # filled by RunHooks.on_handoff


class Runner(StatefulRunner):

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="oai_agents_seam_fixed",
            version="0.2.0",
            product="OpenAI Agents SDK (guardrail seam, BYO policy) — corrected integration",
            vendor="openai/openai-agents-python",
            governance_source="seam",
            notes=(
                "Identical policy engine to oai_agents_seam. Differences are "
                "integration only: context= threading, RunHooks, trace metadata, "
                "needs_approval, and the rate_limit_per_minute the scenario "
                "already supplies. Decisions are still BYO — this measures the "
                "seam, per the benchmark's own definition of a seam column."
            ),
        )

    # ── setup ──────────────────────────────────────────────────────────

    def setup(self, scenario: Scenario) -> None:
        super().setup(scenario)
        self._calls: list[dict[str, Any]] = []
        self._delegations: list[Delegation] = []
        self._policy = self._build_policy(scenario)
        self._emails: dict[str, Optional[str]] = {}
        self._fail_mode = "fail_closed"
        self._unreachable_until = 0.0
        self._gateway_reachable = True
        self._counters: dict[tuple, int] = {}
        for t in scenario.setup.tenants:
            self._fail_mode = t.policy.fail_mode or self._fail_mode
            for u in t.users:
                self._emails[u.uid] = u.email

    def _build_policy(self, scenario: Scenario) -> dict:
        """Same flattening as the original, plus two things it dropped:
        the tenant key (the original collapsed all tenants into one dict)
        and rate_limit_per_minute."""
        tools = {t.name: t for t in scenario.setup.tools}
        rules: dict[tuple, str] = {}
        limits: dict[tuple, Optional[int]] = {}
        scopes: dict[str, set] = {}
        user_tenant: dict[str, str] = {}
        for t in scenario.setup.tenants:
            tid = t.id
            for u in t.users:
                scopes[u.uid] = set(u.scopes or [])
                user_tenant[u.uid] = tid
            for tier, tp in (t.policy.defaults or {}).items():
                rules[(tid, "default", tier, "*")] = tp.permission
                limits[(tid, tier)] = tp.rate_limit_per_minute
            for tool, tiers in (t.policy.tools or {}).items():
                for tier, tp in tiers.items():
                    rules[(tid, "tool", tier, tool)] = tp.permission
            for uid, tiers in (t.policy.users or {}).items():
                for tier, tp in tiers.items():
                    rules[(tid, f"user:{uid}", tier, "*")] = tp.permission
            for uid, tmap in (t.policy.user_tools or {}).items():
                for tool, tiers in tmap.items():
                    for tier, tp in tiers.items():
                        rules[(tid, f"user:{uid}", tier, tool)] = tp.permission
        return {"rules": rules, "scopes": scopes, "limits": limits,
                "user_tenant": user_tenant,
                "required": {n: set(t.required_scopes or []) for n, t in tools.items()}}

    # ── the BYO policy engine (unchanged precedence + limiter) ─────────

    def _decide(self, p: Principal, tool: str, count_it: bool) -> tuple[bool, str, str]:
        """Returns (allowed, decision, reason)."""
        pol = self._policy
        tid = p.tenant or pol["user_tenant"].get(p.actor_uid) or "tenant-a"

        # Outage handling — declared fail mode, honoured.
        if time.time() < self._unreachable_until:
            self._gateway_reachable = False
            if self._fail_mode == "fail_open":
                return True, "flag", "fail_open_no_policy_source"
            return False, "deny", "fail_closed_no_policy_source"

        # Tenant isolation: a principal may only act in its home tenant.
        home = pol["user_tenant"].get(p.actor_uid)
        if home is not None and p.tenant is not None and home != p.tenant:
            return False, "deny", "cross_tenant_principal"

        need = pol["required"].get(tool, set())
        if need and not need.issubset(pol["scopes"].get(p.actor_uid, set())):
            return False, "deny", "scope_missing"
        # Delegated-task narrowing: a subagent cannot exceed the scopes the
        # delegator handed it.
        if p.delegated_scopes is not None and need and not need.issubset(p.delegated_scopes):
            return False, "deny", "delegation_scope_violation"

        for key in ((tid, f"user:{p.actor_uid}", p.agent_tier, tool),
                    (tid, f"user:{p.actor_uid}", p.agent_tier, "*"),
                    (tid, "tool", p.agent_tier, tool),
                    (tid, "default", p.agent_tier, "*")):
            if key in pol["rules"]:
                perm = pol["rules"][key]
                if perm == "deny":
                    return False, "deny", f"tool_not_allowed:{key[1]}/{key[2]}"
                break

        if WITH_LIMITER:
            lim = pol["limits"].get((tid, p.agent_tier))
            if lim is not None:
                k = (tid, p.actor_uid, p.agent_tier)
                if self._counters.get(k, 0) >= lim:
                    return False, "deny", "rate_limited"
                if count_it:                       # denied calls never charged
                    self._counters[k] = self._counters.get(k, 0) + 1
        return True, "allow", "policy_allow"

    # ── actions ────────────────────────────────────────────────────────

    def execute_action(self, action: Action) -> Optional[ToolOutcome]:
        if isinstance(action, DirectToolCall):
            self._calls.append(dict(tool=action.tool, args=action.input,
                                    user=action.as_user, tier=action.agent_tier,
                                    agent_name=action.agent_name, tenant=action.as_tenant))
        elif isinstance(action, ParallelFanOut):
            for i in range(action.worker_count * action.calls_per_worker):
                self._calls.append(dict(tool=action.tool, args=action.input,
                                        user=action.as_user, tier=action.agent_tier,
                                        agent_name=f"worker-{i // action.calls_per_worker}",
                                        tenant=action.as_tenant))
        elif isinstance(action, Delegation):
            self._delegations.append(action)
        elif isinstance(action, PolicyChange):
            # The policy store is the application's — same as ACP's runner
            # writing the tenant doc to Firestore before the next call.
            self._flush()
            tid = action.tenant or "tenant-a"
            tier = action.tier or "interactive"
            if action.set_permission:
                if action.user and action.tool:
                    self._policy["rules"][(tid, f"user:{action.user}", tier, action.tool)] = action.set_permission
                elif action.user:
                    self._policy["rules"][(tid, f"user:{action.user}", tier, "*")] = action.set_permission
                elif action.tool:
                    self._policy["rules"][(tid, "tool", tier, action.tool)] = action.set_permission
                else:
                    self._policy["rules"][(tid, "default", tier, "*")] = action.set_permission
            if action.set_rate_limit is not None:
                self._policy["limits"][(tid, tier)] = action.set_rate_limit
        elif isinstance(action, GatewayFailure):
            # The policy source really does become unavailable: _decide
            # short-circuits on it and the declared fail mode decides.
            self._flush()
            self._unreachable_until = time.time() + action.duration_seconds
            self._gateway_reachable = False
        elif isinstance(action, Wait):
            self._flush()
            time.sleep(action.seconds)
            if time.time() >= self._unreachable_until:
                self._gateway_reachable = True
        return None

    def flush(self) -> None:
        """Harness-invoked batching hook (BaseRunner.flush)."""
        self._flush()

    def _flush(self) -> None:
        if self._calls or self._delegations:
            asyncio.run(self._run())
            self._calls = []

    def collect_outcome(self):
        self._flush()
        o = super().collect_outcome()
        o.gateway_reachable = self._gateway_reachable
        return o

    # ── the SDK run ────────────────────────────────────────────────────

    async def _run(self) -> None:
        from agents import (Agent, FunctionTool, Runner as SDKRunner, RunConfig,
                            RunHooks, ToolInputGuardrail, ToolGuardrailFunctionOutput)
        from agents.testing import ScriptedModel, function_call
        from agents.tracing import trace, get_current_span

        runner_self = self
        executed: set = set()

        # Chain each delegated agent inherits, per the scenario's declared
        # delegations. Replayed as REAL SDK handoffs below; the chain that
        # lands in the audit is the one RunHooks.on_handoff observed.
        chain_by_agent: dict[str, list[str]] = {}
        scopes_by_agent: dict[str, set] = {}
        for d in self._delegations:
            base = chain_by_agent.get(d.from_agent) or [d.from_agent]
            chain_by_agent[d.to_agent] = base + [d.to_agent]
            scopes_by_agent[d.to_agent] = set(d.delegated_scopes or [])

        class AuditHooks(RunHooks):
            async def on_handoff(self, context, from_agent, to_agent):
                p = context.context
                if not p.chain:
                    p.chain.append(from_agent.name)
                p.chain.append(to_agent.name)

        def make_tool(name: str) -> FunctionTool:
            async def invoke(ctx, args_json: str):
                executed.add(ctx.tool_call_id)
                return "ok"
            return FunctionTool(
                name=name, description=f"AgentGovBench stand-in for {name}",
                params_json_schema={"type": "object", "properties": {},
                                    "additionalProperties": True},
                on_invoke_tool=invoke, strict_json_schema=False)

        async def guard(data):
            # Everything below is read off objects the SDK handed us.
            ctx = data.context                      # ToolContext (subclass of
            p: Principal = ctx.context              # RunContextWrapper)
            tool = ctx.tool_name
            allowed, decision, reason = runner_self._decide(p, tool, count_it=True)

            span = get_current_span()               # the live function span
            runner_self._audit.append(AuditEntry(
                timestamp=(span.started_at if span else None) or "",
                tenant=p.tenant or runner_self._policy["user_tenant"].get(p.actor_uid),
                actor_uid=p.actor_uid,
                actor_email=p.actor_email,
                tool=tool,
                decision=decision,
                reason=reason,
                trace_id=span.trace_id if span else None,
                agent_tier=p.agent_tier,
                delegation_chain=list(p.chain),
                source="product",
                extra={"span_id": span.span_id if span else None,
                       "agent": data.agent.name,
                       "args": ctx.tool_arguments,
                       "trace_metadata": span.trace_metadata if span else None},
            ))
            if allowed:
                return ToolGuardrailFunctionOutput(output_info=reason)
            return ToolGuardrailFunctionOutput.reject_content(
                message=f"denied by policy: {reason}", output_info=reason)

        # One SDK run per (user, tier, agent) — i.e. per caller session.
        groups: dict[tuple, list[dict]] = {}
        for i, c in enumerate(self._calls):
            c["_id"] = f"call_{i}"
            groups.setdefault((c["user"], c["tier"] or "interactive",
                               c["agent_name"], c["tenant"]), []).append(c)

        for (user, tier, agent_name, tenant), group in groups.items():
            principal = Principal(
                actor_uid=user, actor_email=self._emails.get(user),
                tenant=tenant or self._policy["user_tenant"].get(user),
                agent_tier=tier, agent_name=agent_name,
                delegated_scopes=scopes_by_agent.get(agent_name or ""),
            )
            tools = [make_tool(n) for n in sorted({c["tool"] for c in group})]
            for t in tools:
                t.tool_input_guardrails = [ToolInputGuardrail(guardrail_function=guard,
                                                              name="policy")]
            leaf = [{"output": [function_call(c["tool"], json.dumps(c["args"] or {}),
                                              call_id=c["_id"]) for c in group]},
                    {"output": []}]
            hops = chain_by_agent.get(agent_name or "", [])
            if hops:
                agent = Agent(name=hops[-1], instructions="run", tools=tools,
                              model=ScriptedModel(leaf))
                for i in range(len(hops) - 2, -1, -1):
                    nxt = agent
                    agent = Agent(name=hops[i], instructions="delegate", handoffs=[nxt],
                                  model=ScriptedModel([
                                      {"output": [function_call(
                                          f"transfer_to_{nxt.name.replace('-', '_')}",
                                          "{}", call_id=f"handoff_{i}")]},
                                      {"output": []}]))
                entry = agent
            else:
                entry = Agent(name=agent_name or "agb", instructions="run",
                              tools=tools, model=ScriptedModel(leaf))

            meta = {"actor_uid": principal.actor_uid, "tenant": principal.tenant,
                    "agent_tier": principal.agent_tier,
                    "actor_email": principal.actor_email}
            before = len(self._audit)
            try:
                with trace(workflow_name="agentgovbench",
                           group_id=f"{principal.tenant}:{user}", metadata=meta):
                    await SDKRunner.run(
                        entry, "go", context=principal, hooks=AuditHooks(),
                        max_turns=8 + 2 * len(hops),
                        run_config=RunConfig(workflow_name="agentgovbench",
                                             group_id=f"{principal.tenant}:{user}",
                                             trace_metadata=meta))
            except Exception as e:
                self._errors.append(f"sdk run: {e!r}")

            # The chain is only known once on_handoff has fired, so
            # back-stamp the rows this run produced.
            for row in self._audit[before:]:
                row.delegation_chain = list(principal.chain)

            for c in group:
                ran = c["_id"] in executed
                row = next((r for r in self._audit[before:]
                            if r.tool == c["tool"] and r.extra.get("args")
                            == json.dumps(c["args"] or {})), None)
                self._tool_outcomes.append(ToolOutcome(
                    tool=c["tool"], input=c["args"] or {}, as_user=user,
                    as_tenant=principal.tenant, allowed=ran,
                    reason=None if ran else (row.reason if row else "denied"),
                    agent_tier=tier, agent_name=c["agent_name"]))

    def audit_log(self) -> list[AuditEntry]:
        return list(self._audit)
