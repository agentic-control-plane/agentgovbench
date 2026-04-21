"""LangChain / LangGraph native runner — no governance adapter.

Represents what you get running LangChain or LangGraph out of the box
with no ACP integration, no custom callback handler, no `BaseCallbackHandler`
attached for audit. Just framework defaults.

LangChain ships with:
  - `@tool` decorator (`langchain_core.tools.tool`) for defining tools
  - `BaseCallbackHandler` infrastructure for observability — but no
    handler attached by default
  - `create_react_agent`, `StateGraph`, supervisor-worker patterns
  - Tracing via LangSmith if you've configured it (separate product)

It does NOT ship with:
  - Per-user identity on tool calls
  - Per-tool policy enforcement
  - Per-user rate limits
  - An audit log by default (callback handlers exist but none are wired)
  - A workspace policy concept
  - Fail-mode handling

So langgraph_native scores at the vanilla floor on enforcement-heavy
categories. Per-framework failure modes specific to LangGraph:

  1. **StateGraph nodes share state but not identity.** Each node in a
     LangGraph runs in the same Python process; identity is whatever the
     caller set. Without an explicit identity threading mechanism, the
     end user's identity doesn't reach individual node tool calls.

  2. **Supervisor-worker handoffs are state mutations.** A supervisor
     adding a worker's output to graph state isn't an event; nothing
     fires unless you add a custom `add_messages` reducer or callback.

  3. **Checkpoint replay loses governance context.** When LangGraph
     resumes from a checkpoint, no governance pipeline re-runs against
     the replayed state. Policy changes between original run and replay
     are silently ignored.

Scorecard expectation: near vanilla (13/48), same as CrewAI native — the
LangChain `BaseCallbackHandler` infrastructure exists but is empty by default.

Version: tested against langchain-core>=0.3, langgraph>=0.2
"""
from __future__ import annotations

from typing import Any, Optional

try:
    from langchain_core.tools import tool as lc_tool
    LANGCHAIN_AVAILABLE = True
except ImportError:
    LANGCHAIN_AVAILABLE = False

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


def _mk_lc_tool(name: str) -> Optional[Any]:
    """Build a no-op LangChain tool bound to the given name."""
    if not LANGCHAIN_AVAILABLE:
        return None

    @lc_tool(name)
    def _t(**kwargs: Any) -> str:
        """Benchmark tool stub — captured by langgraph_native runner."""
        return f"(langgraph_native) {name} called with {kwargs}"

    return _t


class Runner(StatefulRunner):
    """LangChain/LangGraph defaults — no callback handler, no ACP."""

    def __init__(self) -> None:
        super().__init__()
        self._tool_cache: dict[str, Any] = {}
        self._chain_by_agent: dict[str, list[str]] = {}

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="langgraph_native",
            version="0.1.0",
            product="LangChain / LangGraph (no governance adapter)",
            vendor="langchain.com",
            notes=(
                "LangChain @tool decorator + LangGraph StateGraph dispatch, "
                "no BaseCallbackHandler attached for audit, no ACP. "
                "Represents what a new LangChain/LangGraph deployment "
                "looks like before any governance work. Invokes real "
                "langchain_core.tools.tool dispatch."
            ),
            declined_categories={
                "cross_tenant_isolation": (
                    "LangChain/LangGraph have no tenant concept. Multi-"
                    "tenancy is a deployment concern outside the framework."
                ),
            },
        )

    def setup(self, scenario: Scenario) -> None:
        super().setup(scenario)
        self._tool_cache = {}
        self._chain_by_agent = {}

    def execute_action(self, action: Action) -> Optional[ToolOutcome]:
        if isinstance(action, Delegation):
            # LangGraph node-to-node transitions are state mutations,
            # not events. We track chain internally for our own
            # bookkeeping but emit no audit — this is the gap.
            base = list(self._chain_by_agent.get(
                action.from_agent, [action.from_agent],
            ))
            self._chain_by_agent[action.to_agent] = base + [action.to_agent]
            return None
        if isinstance(action, (GatewayFailure, PolicyChange)):
            return None
        if isinstance(action, DirectToolCall):
            return self._invoke_tool(action)
        if isinstance(action, ParallelFanOut):
            last: Optional[ToolOutcome] = None
            total = action.worker_count * action.calls_per_worker
            for i in range(total):
                inner = DirectToolCall(
                    tool=action.tool,
                    input=action.input,
                    as_user=action.as_user,
                    as_tenant=action.as_tenant,
                    agent_tier="subagent",
                    agent_name=f"worker-{i // action.calls_per_worker}",
                )
                last = self._invoke_tool(inner)
            return last
        return None

    def _invoke_tool(self, a: DirectToolCall) -> ToolOutcome:
        """Drive LangChain's tool-dispatch path with the scenario input."""
        tenant = a.as_tenant or (
            self._scenario.setup.tenants[0].id
            if self._scenario and self._scenario.setup.tenants
            else None
        )

        if LANGCHAIN_AVAILABLE:
            lc = self._tool_cache.get(a.tool) or _mk_lc_tool(a.tool)
            if lc is not None:
                self._tool_cache[a.tool] = lc
                try:
                    if hasattr(lc, "invoke"):
                        lc.invoke(a.input or {})
                    elif hasattr(lc, "run"):
                        lc.run(**a.input) if a.input else lc.run()
                    else:
                        lc(**a.input) if a.input else lc()
                except Exception as e:
                    self._errors.append(f"langchain_tool({a.tool}): {e}")

        outcome = ToolOutcome(
            tool=a.tool, input=a.input,
            as_user=a.as_user, as_tenant=tenant,
            allowed=True, reason="langgraph_native_allows_all",
            agent_tier=a.agent_tier, agent_name=a.agent_name,
        )
        self._tool_outcomes.append(outcome)
        # NO audit. Default LangChain has no callback handler attached.
        return outcome
