"""Cursor native runner — IDE with no ACP MCP server attached.

Represents Cursor running with its default tool stack and no ACP MCP
server connected. Cursor uses Model Context Protocol (MCP) for external
tools and has its own internal tools (file edits, terminal, search) that
don't go through MCP at all.

Cursor OOTB has:
  - Internal IDE tools (Edit, Read, Bash terminal) invoked through
    Cursor's own permission UI
  - MCP server connections — user-configured remote tool servers
  - "Allow once" / "Always allow" / "Deny" interactive prompts
  - No structured audit log (logs are TTY/session output)

It does NOT have:
  - Per-end-user identity (Cursor is single-user-per-process,
    workspace-scoped at most)
  - Centralized policy enforcement
  - Audit log a SOC can ingest
  - Per-tool scope checking beyond the user's allow/deny choice
  - Cross-session attribution

Specific Cursor failure modes:

  1. **Internal tools never touch MCP.** Cursor's built-in Edit/Read/
     Bash tools dispatch through Cursor's own engine, not through any
     MCP server. An ACP MCP server can only govern MCP-exposed tools,
     never the IDE's primitive operations. Substantial governance gap
     for code-editing agents.

  2. **MCP "Always allow" persists across sessions.** Once a user
     approves an MCP tool with "Always allow," that approval is
     persisted in Cursor's settings. Subsequent calls bypass the
     governance prompt. Without ACP, no audit fires.

  3. **No identity envelope on MCP requests.** MCP requests Cursor sends
     don't include the end user's identity. They include the MCP
     server's session token (if any). Identity propagation requires
     the MCP server itself to look up identity from its own context.

Scorecard expectation: vanilla floor (13/48). Cursor without ACP is
structurally vanilla — interactive prompts ≠ audit data.

Note: Like Anthropic Agent SDK, Cursor is benchmarked via a Python runner
representing its dispatch pattern. The real integration target is an
ACP MCP server reachable at https://api.agenticcontrolplane.com/mcp.

Version: tested against Cursor 0.45+
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
    """Cursor without an ACP MCP server connection."""

    def __init__(self) -> None:
        super().__init__()
        self._chain_by_agent: dict[str, list[str]] = {}

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="cursor_native",
            version="0.1.0",
            product="Cursor (no ACP MCP server)",
            vendor="cursor.com",
            notes=(
                "Cursor IDE with default tool stack and no ACP MCP "
                "server attached. Internal tools (Edit/Read/Bash) "
                "dispatch through Cursor's engine; user-configured MCP "
                "servers run with their own permission prompts. No "
                "centralized audit, no per-user identity propagation."
            ),
            declined_categories={
                "cross_tenant_isolation": (
                    "Cursor is single-user-per-process. Multi-tenancy "
                    "is outside the IDE's scope."
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
            allowed=True, reason="cursor_native_allows_all",
            agent_tier=a.agent_tier, agent_name=a.agent_name,
        )
        self._tool_outcomes.append(outcome)
        return outcome
