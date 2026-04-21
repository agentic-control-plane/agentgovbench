"""CrewAI native runner — CrewAI OSS with no governance adapter.

Represents what you get running CrewAI out of the box with no ACP
integration, no custom callback handler, no `task_callback`, no
`step_callback`. Just the framework defaults.

CrewAI OSS ships with:
  - @tool decorator for defining tools (`crewai.tools.tool`)
  - Agent / Crew / Task composition
  - Sequential and Hierarchical process modes
  - `allow_delegation` flag on agents
  - Optional user-supplied callbacks (None by default)

It does NOT ship with:
  - Per-user identity on tool calls (tools are invoked from agent context,
    not user context — the end user's identity doesn't flow to the tool)
  - Per-tool policy enforcement (any tool assigned to an agent can be
    called by that agent without scope checking)
  - Rate limits per user or per agent
  - Audit log by default (must be wired via task_callback / step_callback)
  - Policy document (no notion of workspace policy at all)
  - Fail-mode handling (nothing to fail)

So crewai_native scores at the vanilla floor on categories requiring
these features. The interesting per-framework failure modes:

  1. **Hierarchical Process masks worker identity.** When a manager agent
     delegates to a worker via the built-in "Delegate work to coworker"
     tool, the worker's subsequent tool calls originate from a fresh
     Agent context with no reference to the user or the manager. Without
     a callback wired to synthesize a chain, audit is lost entirely.

  2. **Task handoffs carry no audit.** `Task N.output → Task N+1.context`
     is a pure Python assignment. No event fires. No governance sees it.

  3. **No per-user scopes on tools.** The `Agent(tools=[...])` constructor
     decides what an agent can call. There's no way to say "this agent can
     call tool X only when invoked on behalf of user alice."

Scorecard expectation: near vanilla (13/48) — because CrewAI OSS's default
is structurally vanilla. The runner EXISTS to substantiate that claim by
actually invoking CrewAI's tool dispatch (not simulating it).

Version: tested against crewai>=0.70.0
"""
from __future__ import annotations

from typing import Any, Optional

try:
    from crewai.tools import tool as crewai_tool
    CREWAI_AVAILABLE = True
except ImportError:
    CREWAI_AVAILABLE = False

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


def _mk_crewai_tool(name: str) -> Optional[Any]:
    """Create a no-op CrewAI tool bound to the given name. Returns the
    decorated `BaseTool` instance, or None if CrewAI isn't installed."""
    if not CREWAI_AVAILABLE:
        return None

    @crewai_tool(name)
    def _t(**kwargs: Any) -> str:
        """Benchmark tool stub — captured by crewai_native runner."""
        return f"(crewai_native) {name} called with {kwargs}"

    return _t


class Runner(StatefulRunner):
    """CrewAI OSS defaults — no callback handlers, no ACP, no audit."""

    def __init__(self) -> None:
        super().__init__()
        self._tool_cache: dict[str, Any] = {}
        # Track delegation chain purely for our own bookkeeping; CrewAI
        # OSS doesn't expose this to governance without a callback.
        self._chain_by_agent: dict[str, list[str]] = {}

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="crewai_native",
            version="0.1.0",
            product="CrewAI OSS (no governance adapter)",
            vendor="crewai.com",
            notes=(
                "CrewAI out of the box: @tool decorator, Agent/Crew/Task "
                "composition, no callback handlers wired, no ACP. "
                "Represents what a new CrewAI deployment looks like before "
                "any governance work. Invokes real crewai.tools.tool dispatch."
            ),
            declined_categories={
                "cross_tenant_isolation": (
                    "CrewAI OSS has no tenant concept. Multi-tenancy is a "
                    "deployment-level concern outside the framework."
                ),
            },
        )

    def setup(self, scenario: Scenario) -> None:
        super().setup(scenario)
        self._tool_cache = {}
        self._chain_by_agent = {}

    def execute_action(self, action: Action) -> Optional[ToolOutcome]:
        if isinstance(action, Delegation):
            # Track chain internally — but emit NO audit. CrewAI's
            # "Delegate work to coworker" tool does not fire any event
            # that a governance layer could observe. This is the gap.
            base = list(self._chain_by_agent.get(
                action.from_agent, [action.from_agent],
            ))
            self._chain_by_agent[action.to_agent] = base + [action.to_agent]
            return None
        if isinstance(action, (GatewayFailure, PolicyChange)):
            # Nothing to fail, nothing to change. CrewAI OSS has no
            # governance layer to affect.
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
        """Actually drive CrewAI's tool-dispatch path.

        We look up (or create) a @crewai_tool-decorated function with the
        scenario's tool name, then invoke its run method. No governance
        is in the loop — this is what CrewAI itself does when its agent
        calls a tool, minus the LLM's decision to call it.
        """
        tenant = a.as_tenant or (
            self._scenario.setup.tenants[0].id
            if self._scenario and self._scenario.setup.tenants
            else None
        )

        if CREWAI_AVAILABLE:
            crew_tool = self._tool_cache.get(a.tool) or _mk_crewai_tool(a.tool)
            if crew_tool is not None:
                self._tool_cache[a.tool] = crew_tool
                try:
                    # Call through CrewAI's BaseTool.run() when possible,
                    # else fall back to direct invocation.
                    if hasattr(crew_tool, "run"):
                        crew_tool.run(**a.input) if a.input else crew_tool.run()
                    else:
                        crew_tool(**a.input) if a.input else crew_tool()
                except Exception as e:
                    # Tool execution errors don't change governance
                    # semantics — the call was still dispatched (allowed).
                    self._errors.append(f"crewai_tool.run({a.tool}): {e}")

        outcome = ToolOutcome(
            tool=a.tool,
            input=a.input,
            as_user=a.as_user,
            as_tenant=tenant,
            allowed=True,
            reason="crewai_native_allows_all",
            agent_tier=a.agent_tier,
            agent_name=a.agent_name,
        )
        self._tool_outcomes.append(outcome)
        # NO audit entry. CrewAI OSS emits no audit without a callback.
        return outcome
