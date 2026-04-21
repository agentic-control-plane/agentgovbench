"""OpenAI Agents SDK native runner — direct OpenAI client, no governance.

Represents what you get running the OpenAI Agents SDK with the default
AsyncOpenAI client pointed straight at api.openai.com. No proxy, no
guardrails, no per-agent attribution to a governance backend.

The OpenAI Agents SDK ships with:
  - Agent / Runner / handoffs primitives
  - Tools defined with @function_tool decorator
  - Optional Guardrails (input/output validators that run in-process)
  - Tracing via OpenAI's traces dashboard (separate product)

It does NOT ship with:
  - Per-end-user identity propagation (the SDK uses one OPENAI_API_KEY)
  - A workspace-policy concept
  - Per-tool scope checking outside Guardrails
  - A SIEM-ingestible audit log
  - Cross-agent delegation provenance separate from the trace product
  - Fail-mode discipline for the governance layer (there is no governance layer)

Specific OpenAI Agents SDK failure modes:

  1. **Guardrails are layered before any external governance.** A
     guardrail denial short-circuits before reaching ACP. The audit log
     never sees the call. ACP-level audit is partial-by-design unless
     guardrails are also instrumented.

  2. **handoffs leak identity.** When Agent A hands off to Agent B,
     the OPENAI_API_KEY context is shared. There's no "this hop carries
     the user's identity" semantic — it's all one process with one key.

  3. **No org-scoped audit usable by your security team.** OpenAI's
     Admin API audit is org-level (key creation, etc.). It does not
     capture per-tool-call attribution. For that you need a runner-level
     instrumentation layer.

Scorecard expectation: vanilla floor (13/48). Identical floor to other
framework natives.

Version: tested against openai>=1.40, openai-agents (any pre-release)
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
    """OpenAI Agents SDK with direct OpenAI client. No governance."""

    def __init__(self) -> None:
        super().__init__()
        self._chain_by_agent: dict[str, list[str]] = {}

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="openai_agents_native",
            version="0.1.0",
            product="OpenAI Agents SDK (direct openai client)",
            vendor="openai.com",
            notes=(
                "OpenAI Agents SDK with default AsyncOpenAI client and "
                "no governance proxy. Represents a fresh deployment "
                "before any governance work is done. Tools dispatch "
                "via the SDK's @function_tool path; nothing is captured "
                "by an audit layer outside OpenAI's own tracing product."
            ),
            declined_categories={
                "cross_tenant_isolation": (
                    "OpenAI Agents SDK has no tenant concept. Multi-"
                    "tenancy is a deployment concern."
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
            return None
        if isinstance(action, DirectToolCall):
            return self._allow(action)
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
                last = self._allow(inner)
            return last
        return None

    def _allow(self, a: DirectToolCall) -> ToolOutcome:
        tenant = a.as_tenant or (
            self._scenario.setup.tenants[0].id
            if self._scenario and self._scenario.setup.tenants
            else None
        )
        outcome = ToolOutcome(
            tool=a.tool, input=a.input,
            as_user=a.as_user, as_tenant=tenant,
            allowed=True, reason="openai_agents_native_allows_all",
            agent_tier=a.agent_tier, agent_name=a.agent_name,
        )
        self._tool_outcomes.append(outcome)
        return outcome
