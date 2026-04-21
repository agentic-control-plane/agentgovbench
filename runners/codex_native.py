"""Codex CLI native runner — OpenAI's coding agent CLI with no ACP hook.

Codex CLI is OpenAI's terminal coding agent — analogous to Claude Code,
similar PreToolUse hook semantics. Native means no ACP hook installed.

Codex CLI ships with:
  - Interactive permission prompts per tool call
  - MCP server connections (configurable)
  - PreToolUse / PostToolUse hook system (similar to Claude Code)
  - Session-scoped tool tracking
  - TTY output of all tool inputs/outputs

It does NOT ship with:
  - Per-end-user identity (single-user-per-process)
  - Workspace policy
  - SIEM-ingestible audit log
  - Cross-session attribution
  - Per-tool scope checking beyond user approval

Specific Codex CLI failure modes:

  1. **Permission auto-approve in `--auto` mode.** Like Claude Code's
     `--dangerously-skip-permissions`, Codex CLI has an auto-approve
     mode that suppresses interactive prompts. ACP hooks would still
     fire in auto mode (unlike Claude Code, where they don't), but
     governance has to be wired explicitly.

  2. **MCP server tools have no native attribution.** Tools served via
     MCP carry session tokens but no end-user identity unless the MCP
     server itself injects it.

  3. **Code-execution tools never touch hooks in some configs.** Like
     Cursor's internal tools, Codex CLI has primitive operations
     (write file, run shell) that bypass the MCP layer.

Scorecard expectation: vanilla floor (13/48). Codex CLI without ACP
hook is structurally vanilla.

Version: tested against Codex CLI v0.x (install from npm)
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
    """Codex CLI with no ACP PreToolUse/PostToolUse hooks installed."""

    def __init__(self) -> None:
        super().__init__()
        self._chain_by_agent: dict[str, list[str]] = {}

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="codex_native",
            version="0.1.0",
            product="OpenAI Codex CLI (no hooks)",
            vendor="openai.com",
            notes=(
                "Codex CLI without ACP PreToolUse/PostToolUse hooks. "
                "Equivalent governance state to Claude Code without "
                "hooks: interactive permission prompts produce no "
                "structured audit data and enforce no policy beyond "
                "per-call user approval."
            ),
            declined_categories={
                "cross_tenant_isolation": (
                    "Codex CLI is single-user-per-process. Multi-"
                    "tenancy is outside the framework's scope."
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
            allowed=True, reason="codex_native_allows_all",
            agent_tier=a.agent_tier, agent_name=a.agent_name,
        )
        self._tool_outcomes.append(outcome)
        return outcome
