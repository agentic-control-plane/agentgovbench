"""LangChain/LangGraph + ACP runner — @governed wraps LC tools.

Same scoring story as the `acp` runner, but the governance check fires
from inside LangChain's actual tool-dispatch path. Proves @governed
behaves correctly when stacked under @tool in a real LangChain or
LangGraph deployment.

Design: subclass the live `acp` runner. Override _do_direct so
DirectToolCall actions route via a dynamically-constructed
@lc_tool @governed decorator stack.

Version: tested against langchain-core>=0.3, langgraph>=0.2,
acp-langchain>=0.1.0

Required environment: same as the `acp` runner.
"""
from __future__ import annotations

import time
from typing import Any, Optional

try:
    from langchain_core.tools import tool as lc_tool
    LANGCHAIN_AVAILABLE = True
except ImportError:
    LANGCHAIN_AVAILABLE = False

try:
    from acp_langchain import configure, governed, set_context
    ACP_SDK_AVAILABLE = True
except ImportError:
    ACP_SDK_AVAILABLE = False

from benchmark.runner import RunnerMetadata
from benchmark.types import AuditEntry, DirectToolCall, ToolOutcome

from runners.acp import Runner as AcpRunner


def _mk_governed_tool(name: str) -> Optional[Any]:
    """Build a LangChain @tool-decorated, @governed-wrapped function."""
    if not LANGCHAIN_AVAILABLE or not ACP_SDK_AVAILABLE:
        return None

    @lc_tool(name)
    @governed(name)
    def _t(**kwargs: Any) -> str:
        """Benchmark tool stub — governed by ACP via @governed."""
        return f"(langgraph_acp) {name} called with {kwargs}"

    return _t


class Runner(AcpRunner):
    """LangChain tool dispatch with ACP governance via @governed."""

    def __init__(self) -> None:
        super().__init__()
        self._tool_cache: dict[str, Any] = {}
        if ACP_SDK_AVAILABLE:
            configure(base_url=self._env.get("acp_base_url",
                                             "https://api.agenticcontrolplane.com"))

    @property
    def metadata(self) -> RunnerMetadata:
        base = super().metadata
        return RunnerMetadata(
            name="langgraph_acp",
            version="0.1.0",
            product="LangChain/LangGraph + ACP (@governed)",
            vendor="agenticcontrolplane.com",
            notes=(
                "LangChain tools are @lc_tool stacked on @governed. Each "
                "tool call dispatches through LangChain's tool.invoke() "
                "which invokes the @governed wrapper — same governance "
                "pipeline as the `acp` runner, proven through the real "
                "framework dispatch path."
            ),
            declined_categories=dict(base.declined_categories),
        )

    def _do_direct(self, a: DirectToolCall) -> ToolOutcome:
        if not (LANGCHAIN_AVAILABLE and ACP_SDK_AVAILABLE):
            self._errors.append(
                "langchain or acp-langchain not installed; falling back to "
                "direct HTTP via parent acp runner."
            )
            return super()._do_direct(a)

        reported_tenant = a.as_tenant or "tenant-a"
        _, real_tid = self._resolve_tenant(a.as_tenant)
        if real_tid not in self._tenants_used:
            self._tenants_used.add(real_tid)
        now = time.time()

        if now < self._simulated_unreachable_until:
            return self._fail_mode_outcome(a, reported_tenant)
        if now < self._simulated_5xx_until:
            return self._fail_mode_outcome(a, reported_tenant, mode="5xx")

        id_token = self._id_token_for(a.as_user)
        if not id_token:
            outcome = ToolOutcome(
                tool=a.tool, input=a.input,
                as_user=a.as_user, as_tenant=reported_tenant,
                allowed=False, reason="no_user_token",
                agent_tier=a.agent_tier, agent_name=a.agent_name,
            )
            self._tool_outcomes.append(outcome)
            return outcome

        set_context(
            user_token=id_token,
            agent_tier=a.agent_tier,
            agent_name=a.agent_name,
        )

        tool_obj = self._tool_cache.get(a.tool) or _mk_governed_tool(a.tool)
        if tool_obj is None:
            self._errors.append(f"failed to build governed tool: {a.tool}")
            return super()._do_direct(a)
        self._tool_cache[a.tool] = tool_obj

        start = time.time()
        try:
            raw = tool_obj.invoke(a.input or {})
        except Exception as e:
            self._errors.append(f"langgraph_acp tool.invoke({a.tool}): {e}")
            raw = ""
        latency_ms = (time.time() - start) * 1000

        denied = isinstance(raw, str) and raw.startswith("tool_error:")
        reason = raw.split("tool_error:", 1)[1].strip() if denied else None

        outcome = ToolOutcome(
            tool=a.tool, input=a.input,
            as_user=a.as_user, as_tenant=reported_tenant,
            allowed=(not denied), reason=reason,
            agent_tier=a.agent_tier, agent_name=a.agent_name,
            latency_ms=latency_ms,
        )
        self._tool_outcomes.append(outcome)
        return outcome

    def _fail_mode_outcome(
        self, a: DirectToolCall, reported_tenant: str, mode: str = "unreachable",
    ) -> ToolOutcome:
        self._gateway_reachable = False
        fail_mode = self._fail_mode_for_scenario()
        allowed = (fail_mode == "fail_open")
        outcome = ToolOutcome(
            tool=a.tool, input=a.input,
            as_user=a.as_user, as_tenant=reported_tenant,
            allowed=allowed, reason=f"{fail_mode} ({mode})",
            agent_tier=a.agent_tier, agent_name=a.agent_name,
        )
        self._tool_outcomes.append(outcome)
        if fail_mode == "fail_open":
            self._local_audit_entries.append(AuditEntry(
                timestamp="",
                tenant=reported_tenant,
                actor_uid=a.as_user,
                actor_email=None,
                tool=a.tool,
                decision="allow",
                reason="fail_open_local_audit",
                trace_id=None,
                delegation_chain=list(self._chain_by_agent.get(a.agent_name, [])),
                extra={"source": "sdk_local_fallback"},
            ))
        return outcome
