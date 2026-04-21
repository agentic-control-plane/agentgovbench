"""Cursor + ACP runner — MCP connection to api.agenticcontrolplane.com/mcp.

Cursor connects to an ACP MCP server for governed tool calls. Every
tool exposed through the MCP server passes through ACP's pipeline.

Important boundary: **only MCP-exposed tools are governed.** Cursor's
internal tools (Edit/Read/Bash) bypass MCP entirely and remain at the
native level — no governance, no audit, just Cursor's own permission
prompts. This is a structural limitation of the MCP integration shape,
not an ACP gap.

For benchmark scoring, this runner reuses acp.Runner's machinery to
represent the MCP-pipeline outcomes. Scenarios that target MCP-exposable
tools score like the `acp` runner; scenarios that would target internal
IDE tools score like cursor_native.

Required environment: same as acp runner.
"""
from __future__ import annotations

from benchmark.runner import RunnerMetadata
from benchmark.types import DirectToolCall, ToolOutcome

from runners.acp import Runner as AcpRunner


# Tools the benchmark scenarios use that are realistically MCP-exposable
# (vs internal IDE tools that would never be MCP). Inferred from common
# external tool patterns: API calls, database queries, external services.
MCP_EXPOSABLE_TOOLS = {
    "read_email", "write_email", "send_email",
    "db_query", "db_write",
    "api_call", "http_get", "http_post",
    "calendar_read", "calendar_write",
    "search_web", "fetch_url",
    "slack_message", "github_issue",
    "stripe_charge", "stripe_refund",
    # Default-include other tool names; we override the deny-list below
    # rather than enumerating every possible MCP-exposable tool.
}

# Tools that are INTERNAL to Cursor's IDE engine and never touch MCP.
# For these scenarios, governance falls back to cursor_native semantics
# (allow-all, no audit). This represents the structural integration gap.
CURSOR_INTERNAL_TOOLS = {
    "edit_file", "read_file", "bash_exec", "terminal",
    "fs.delete", "fs.write", "fs.read",
    "shell.exec",
}


class Runner(AcpRunner):
    """Cursor with ACP MCP server. Only MCP-exposed tools are governed."""

    @property
    def metadata(self) -> RunnerMetadata:
        base = super().metadata
        return RunnerMetadata(
            name="cursor_acp",
            version="0.1.0",
            product="Cursor + ACP MCP server",
            vendor="agenticcontrolplane.com",
            notes=(
                "Cursor with MCP server set to "
                "https://api.agenticcontrolplane.com/mcp. Only tools "
                "exposed through MCP are governed. Cursor's internal "
                "tools (Edit/Read/Bash/Terminal) bypass MCP and remain "
                "at the native level — structural gap of the MCP "
                "integration shape, not an ACP gap. Scenarios targeting "
                "internal tools fall back to cursor_native semantics."
            ),
            declined_categories=dict(base.declined_categories),
        )

    def _do_direct(self, a: DirectToolCall) -> ToolOutcome:  # type: ignore[override]
        # Internal IDE tools bypass MCP — fall back to native (allow-all,
        # no audit). Captures the real-world boundary of the integration.
        if a.tool in CURSOR_INTERNAL_TOOLS:
            tenant = a.as_tenant or "tenant-a"
            outcome = ToolOutcome(
                tool=a.tool, input=a.input,
                as_user=a.as_user, as_tenant=tenant,
                allowed=True, reason="cursor_internal_bypasses_mcp",
                agent_tier=a.agent_tier, agent_name=a.agent_name,
            )
            self._tool_outcomes.append(outcome)
            # NO audit — internal tools never reach the MCP/ACP pipeline.
            return outcome

        # MCP-exposable tools route through the parent's gateway logic.
        return super()._do_direct(a)
