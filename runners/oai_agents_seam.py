"""OpenAI Agents SDK: how good is its interception seam, given a BYO policy engine?

The framework-native baseline. Not "what does it do out of the box" but
"what is the best a developer can do with what ships in the box" — because
the honest question a buyer asks is whether they need anything beyond the
free thing they already have.

So this uses the strongest primitives the SDK offers:

  * tool_input_guardrails — inspect a tool call before it runs and reject
    it. This is the closest thing to a policy decision point.
  * needs_approval — pause for human approval. Headless here, so an
    approval that cannot be granted blocks, the same posture every other
    subject takes on this benchmark.

Determinism comes free: agents.testing ships ScriptedModel, a first-party
supported harness for driving an agent through fixed steps with no live
model. Unlike pi, no hand-built event stream is needed — the SDK treats
deterministic scripting as a supported use case.

WHAT THIS SUBJECT CAN AND CANNOT SHOW
-------------------------------------
It CAN show delegation, which pi structurally could not: handoffs happen
in-process, so a parent agent passing work to a child is observable and
scriptable. That unlocks the categories pi has to decline.

It CANNOT show a fail mode — guardrails run in-process, so there is no
control plane to become unreachable — and it has no tenant concept.
Declined rather than scored zero, the same treatment AGT gets for the same
structural reasons.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

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
    """Scenarios driven through a real Agents SDK run, no live model."""

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="oai_agents_seam",
            version="0.1.0",
            product="OpenAI Agents SDK (guardrail seam, BYO policy)",
            vendor="openai/openai-agents-python",
            governance_source="seam",
            notes=(
                "openai-agents 0.21.1 driven by agents.testing.ScriptedModel "
                "so no live model is involved. IMPORTANT: the SDK ships a "
                "tool_input_guardrails hook but no policy engine, so the "
                "precedence logic evaluated inside that hook was written by "
                "this runner. A good score here means the seam is expressive "
                "enough to build governance on, not that the SDK governs. "
                "The engine is the buyer's to build and to operate."
            ),
            declined_categories={
                "fail_mode_discipline (whole category)": (
                    "Guardrails run in-process. There is no control plane to "
                    "become unreachable, so a fail mode is not a property "
                    "this subject has — the same structural reason AGT "
                    "declines it."
                ),
                "cross_tenant_isolation (whole category)": (
                    "The SDK has no tenant concept. Scoring zero would "
                    "penalise it for lacking a dimension it never claimed."
                ),
            },
        )

    # ── Scenario → scripted run ────────────────────────────────────────

    def setup(self, scenario: Scenario) -> None:
        super().setup(scenario)
        self._calls: list[dict[str, Any]] = []
        self._delegations: list[Delegation] = []
        self._spans: list[Any] = []
        self._policy = self._build_policy(scenario)

    def _build_policy(self, scenario: Scenario) -> dict:
        """Flatten the scenario's policy into something a guardrail can read.

        Deliberately the same information every other subject gets — the
        guardrail is the framework's decision point, so it should see the
        same rules ACP's gateway or AGT's engine would.
        """
        tools = {t.name: t for t in scenario.setup.tools}
        rules: dict[tuple[str, str, str], str] = {}
        scopes: dict[str, set[str]] = {}
        for t in scenario.setup.tenants:
            for u in t.users:
                scopes[u.uid] = set(u.scopes or [])
            for tier, tp in (t.policy.defaults or {}).items():
                rules[("default", tier, "*")] = tp.permission
            for tool, tiers in (t.policy.tools or {}).items():
                for tier, tp in tiers.items():
                    rules[("tool", tier, tool)] = tp.permission
            for uid, tiers in (t.policy.users or {}).items():
                for tier, tp in tiers.items():
                    rules[(f"user:{uid}", tier, "*")] = tp.permission
            for uid, tmap in (t.policy.user_tools or {}).items():
                for tool, tiers in tmap.items():
                    for tier, tp in tiers.items():
                        rules[(f"user:{uid}", tier, tool)] = tp.permission
        return {"rules": rules, "scopes": scopes,
                "required": {n: set(t.required_scopes or []) for n, t in tools.items()}}

    def _decide(self, user: str, tier: str, tool: str) -> tuple[bool, str]:
        """Most-specific-wins, the same precedence the other subjects use."""
        p = self._policy
        need = p["required"].get(tool, set())
        if need and not need.issubset(p["scopes"].get(user, set())):
            return False, "missing required scope"
        for key in ((f"user:{user}", tier, tool), (f"user:{user}", tier, "*"),
                    ("tool", tier, tool), ("default", tier, "*")):
            if key in p["rules"]:
                perm = p["rules"][key]
                return perm != "deny", f"matched {key[0]}/{key[1]}"
        return True, "no matching rule"

    def execute_action(self, action: Action) -> Optional[ToolOutcome]:
        if isinstance(action, DirectToolCall):
            self._calls.append({
                "tool": action.tool, "args": action.input,
                "user": action.as_user, "tier": action.agent_tier,
                "agent_name": action.agent_name, "tenant": action.as_tenant,
            })
        elif isinstance(action, ParallelFanOut):
            for i in range(action.worker_count * action.calls_per_worker):
                self._calls.append({
                    "tool": action.tool, "args": action.input,
                    "user": action.as_user, "tier": action.agent_tier,
                    "agent_name": f"worker-{i // action.calls_per_worker}",
                    "tenant": action.as_tenant,
                })
        elif isinstance(action, Delegation):
            # Recorded, then replayed as a REAL SDK handoff at run time so
            # the chain in the audit comes from the SDK's own tracing rather
            # than from this list.
            self._delegations.append(action)
        elif isinstance(action, PolicyChange):
            # There is no policy store to change — the rules live in this
            # runner. Re-deriving them would be the runner grading itself,
            # so the scenario goes unmeasured rather than passing for free.
            self._errors.append(
                "PolicyChange is not expressible: this subject has no policy "
                "store to mutate. Unmeasured, not failed."
            )
        elif isinstance(action, GatewayFailure):
            self._errors.append(
                "GatewayFailure is not expressible: guardrails run in-process, "
                "so there is nothing to make unreachable. Unmeasured."
            )
        return None

    def collect_outcome(self):
        if self._calls or self._delegations:
            asyncio.run(self._run())
            self._calls = []
        return super().collect_outcome()

    async def _run(self) -> None:
        from agents import (Agent, FunctionTool, Runner as SDKRunner,
                            ToolInputGuardrail, ToolGuardrailFunctionOutput)
        from agents.testing import ScriptedModel, function_call
        from agents.tracing import TracingProcessor, set_trace_processors
        from agents.tracing.span_data import HandoffSpanData

        executed: set[str] = set()
        collected: list[Any] = []
        runner_self = self

        class _Collector(TracingProcessor):
            """Reads the SDK's own spans. This is the ONLY audit source for
            this subject — nothing here is synthesised by the runner, so
            whatever the SDK does not record simply does not appear."""
            def on_trace_start(self, t): pass
            def on_trace_end(self, t): pass
            def on_span_start(self, s): pass
            def on_span_end(self, s): collected.append(s)
            def shutdown(self): pass
            def force_flush(self): pass

        set_trace_processors([_Collector()])

        def make_tool(name: str) -> FunctionTool:
            async def invoke(ctx, args_json: str):
                executed.add(getattr(ctx, "tool_call_id", "") or name)
                return "ok"
            return FunctionTool(
                name=name,
                description=f"AgentGovBench stand-in for {name}",
                params_json_schema={"type": "object", "properties": {},
                                    "additionalProperties": True},
                on_invoke_tool=invoke,
                strict_json_schema=False,
            )

        groups: dict[tuple[str, str], list[dict]] = {}
        for i, c in enumerate(self._calls):
            c["_id"] = f"call_{i}"
            groups.setdefault((c["user"], c["tier"] or "interactive"), []).append(c)

        # The delegation chain the scenario declared, replayed as real SDK
        # handoffs: orchestrator → worker → … with the tools on the last hop.
        hops: list[str] = []
        for d in self._delegations:
            if not hops:
                hops.append(d.from_agent)
            hops.append(d.to_agent)

        for (user, tier), group in groups.items():
            decisions = {c["_id"]: self._decide(user, tier, c["tool"]) for c in group}

            async def guard(data, _dec=decisions):
                ctx = getattr(data, "context", None)
                cid = getattr(ctx, "tool_call_id", None) or getattr(data, "tool_call_id", None)
                allowed, why = _dec.get(cid, (True, "unknown call"))
                if allowed:
                    return ToolGuardrailFunctionOutput(output_info=why)
                return ToolGuardrailFunctionOutput.reject_content(
                    message=f"denied by policy: {why}", output_info=why)

            tools = [make_tool(n) for n in sorted({c["tool"] for c in group})]
            for t in tools:
                t.tool_input_guardrails = [ToolInputGuardrail(guardrail_function=guard)]

            leaf_steps = [
                {"output": [function_call(c["tool"], json.dumps(c["args"] or {}),
                                          call_id=c["_id"]) for c in group]},
                {"output": []},
            ]

            if hops:
                # Build the chain from the leaf backwards so each agent holds
                # a handoff to the next.
                agent = Agent(name=hops[-1], instructions="run the scripted calls",
                              tools=tools, model=ScriptedModel(leaf_steps))
                for i in range(len(hops) - 2, -1, -1):
                    nxt = agent
                    agent = Agent(
                        name=hops[i], instructions="delegate",
                        handoffs=[nxt],
                        model=ScriptedModel([
                            {"output": [function_call(
                                f"transfer_to_{nxt.name.replace('-', '_')}",
                                "{}", call_id=f"handoff_{i}")]},
                            {"output": []},
                        ]),
                    )
                entry = agent
            else:
                entry = Agent(name="agb", instructions="run the scripted calls",
                              tools=tools, model=ScriptedModel(leaf_steps))

            try:
                await SDKRunner.run(entry, "go", max_turns=8 + 2 * len(hops))
            except Exception as e:
                self._errors.append(f"sdk run: {e!r}")

            for c in group:
                allowed, why = decisions[c["_id"]]
                ran = c["_id"] in executed
                self._tool_outcomes.append(ToolOutcome(
                    tool=c["tool"], input=c["args"] or {}, as_user=c["user"],
                    as_tenant=c["tenant"], allowed=ran,
                    reason=None if ran else why,
                    agent_tier=c["tier"], agent_name=c["agent_name"],
                ))

        self._spans = collected
        self._build_audit_from_spans()

    def _build_audit_from_spans(self) -> None:
        """Turn the SDK's spans into audit entries, adding nothing.

        Every field here is read off a span the SDK emitted on its own.
        Fields the SDK does not record — which principal the call was for,
        why it was allowed, what tier the agent ran at — are left unset, and
        the assertions that need them fail. That failure is the finding:
        tracing tells you who called whom, not who it was for or why it was
        permitted.
        """
        from agents.tracing.span_data import FunctionSpanData, HandoffSpanData

        chain: list[str] = []
        for s in self._spans:
            d = s.span_data
            if isinstance(d, HandoffSpanData):
                if not chain:
                    chain.append(d.from_agent)
                chain.append(d.to_agent)

        for s in self._spans:
            d = s.span_data
            if not isinstance(d, FunctionSpanData):
                continue
            self._audit.append(AuditEntry(
                # The SDK does stamp its spans, so this is real.
                timestamp=getattr(s, "started_at", None) or "",
                # It has no tenant and no principal. Left empty rather than
                # back-filled from the scenario: the whole question these
                # scenarios ask is whether the product knows who it was for,
                # and copying the answer in from the question is how a
                # benchmark scores a subject on the runner's knowledge.
                tenant=None,
                actor_uid=None,
                actor_email=None,
                tool=d.name,
                # Seam-derived, like every decision in this column — see the
                # governance-source banner. The SDK records that a function
                # ran, not that a policy permitted it.
                decision="allow",
                # Read off the SDK's own HandoffSpanData.
                delegation_chain=list(chain),
                trace_id=getattr(s, "trace_id", None),
                agent_tier=None,
                source="product",
                extra={"span_id": getattr(s, "span_id", None)},
            ))

    def audit_log(self) -> list[AuditEntry]:
        # The SDK ships tracing, not an audit trail with decisions. Nothing
        # to read here yet; audit assertions will fail and that is the
        # honest answer until tracing is wired.
        return list(self._audit)
