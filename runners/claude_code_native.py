"""Claude Code native runner — Claude Code with no hooks configured.

Represents what Claude Code looks like out of the box without the ACP
PreToolUse/PostToolUse hooks installed (or with --dangerously-skip-permissions).

Claude Code OOTB has:
  - Interactive permission prompts (user-approved per call)
  - Tool input/output displayed in the TTY
  - No structured audit log (TTY output isn't audit)
  - No machine-readable governance API

It does NOT have:
  - Per-user identity (Claude Code is single-user-per-process)
  - Workspace-level policy enforcement
  - Per-tool scope checking
  - Rate limits
  - An audit log a SIEM can ingest
  - Cross-session attribution

Specific Claude Code failure modes:

  1. **`--dangerously-skip-permissions` disables ALL hooks.** Including
     ACP's. There's no server-side detection — the hook simply doesn't
     fire. Audit goes silent. This is by design (Anthropic ships an
     escape hatch) but it's a known governance gap.

  2. **Subagent attribution is partial.** Claude Code's Agent tool
     spawns subagents (Explore, Plan, etc.) but the hook payload doesn't
     propagate parent agent context. Subagent calls show as generic
     `subagent` tier without parent name.

  3. **No persistent identity.** Each Claude Code session is a fresh
     process. There's no SSO, no per-user attribution unless the hook
     provides it.

Scorecard expectation: vanilla floor (13/48). Claude Code without ACP's
hook is structurally vanilla — interactive permissions don't satisfy
audit completeness assertions, identity propagation, or per-user policy.

Version: tested against Claude Code 1.x (any), no hooks
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
    """Claude Code with no PreToolUse/PostToolUse hooks installed."""

    def __init__(self) -> None:
        super().__init__()
        self._chain_by_agent: dict[str, list[str]] = {}

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="claude_code_native",
            version="0.1.0",
            product="Claude Code (no hooks)",
            vendor="anthropic.com",
            notes=(
                "Claude Code without ACP's PreToolUse/PostToolUse hooks "
                "installed. Equivalent governance state to running with "
                "--dangerously-skip-permissions: interactive permission "
                "prompts exist but produce no structured audit data and "
                "enforce no policy beyond per-call user approval."
            ),
            declined_categories={
                "cross_tenant_isolation": (
                    "Claude Code is single-user-per-process. Multi-tenancy "
                    "is outside the framework's scope."
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
            allowed=True, reason="claude_code_native_allows_all",
            agent_tier=a.agent_tier, agent_name=a.agent_name,
        )
        self._tool_outcomes.append(outcome)
        # NO audit. Claude Code without hooks emits TTY output, not audit data.
        return outcome
