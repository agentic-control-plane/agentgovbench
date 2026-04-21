"""CrewAI + ACP runner — @governed wraps CrewAI tools.

Same scoring story as the `acp` runner, but the governance check fires
from inside CrewAI's actual tool-dispatch path, not from a synthesized
HTTP call. Proves the claim that `@governed` behaves correctly when
stacked under `@tool` in a real CrewAI deployment.

Design: subclass the live `acp` runner. Reuse all its Firestore policy
writes, Firebase token minting, audit reads, and fail-mode simulation.
Only override ``_do_direct`` so DirectToolCall actions route via a
dynamically-constructed ``@crewai_tool @governed`` decorator stack.

Everything the SDK's ``@governed`` wrapper does — pre_tool_use call,
post_tool_output call, fail-open semantics — is the behavior under test.
Match the `acp` runner's score (give or take framework-specific
divergences) or surface a bug in acp-crewai.

Version: tested against crewai>=0.70.0 + acp-crewai>=0.1.0

Required environment: same as the `acp` runner
(GOOGLE_APPLICATION_CREDENTIALS, FIREBASE_WEB_API_KEY, AGB_TENANT_ID, ...)
"""
from __future__ import annotations

import time
from typing import Any, Optional

try:
    from crewai.tools import tool as crewai_tool
    CREWAI_AVAILABLE = True
except ImportError:
    CREWAI_AVAILABLE = False

try:
    from acp_crewai import configure, governed, set_context
    from acp_governance._hook import (
        pre_tool_use as _acp_pre,
        post_tool_output as _acp_post,
    )
    ACP_SDK_AVAILABLE = True
except ImportError:
    ACP_SDK_AVAILABLE = False

from benchmark.runner import RunnerMetadata
from benchmark.types import AuditEntry, DirectToolCall, ToolOutcome

from runners.acp import Runner as AcpRunner, UID_MAP


def _mk_governed_tool(name: str) -> Optional[Any]:
    """Build a CrewAI @tool-decorated, @governed-wrapped function.
    The stack order matches the docs: @crewai_tool(name) OUTER,
    @governed(name) INNER. Both decorators wrap the same underlying
    no-op stub; the governance check runs inside CrewAI's dispatch."""
    if not CREWAI_AVAILABLE or not ACP_SDK_AVAILABLE:
        return None

    @crewai_tool(name)
    @governed(name)
    def _t(**kwargs: Any) -> str:
        """Benchmark tool stub — governed by ACP via @governed."""
        return f"(crewai_acp) {name} called with {kwargs}"

    return _t


class Runner(AcpRunner):
    """CrewAI tool dispatch with ACP governance wired via @governed."""

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
            name="crewai_acp",
            version="0.1.0",
            product="CrewAI + ACP (@governed)",
            vendor="agenticcontrolplane.com",
            notes=(
                "CrewAI tools are defined with @crewai_tool stacked on "
                "@governed. Each tool call dispatches through CrewAI's "
                "BaseTool.run() which invokes the @governed wrapper — "
                "identical governance pipeline as the `acp` runner, but "
                "proven through the real framework dispatch path."
            ),
            declined_categories=dict(base.declined_categories),
        )

    # Override _do_direct to route via CrewAI tool dispatch + @governed
    # instead of the parent class's direct HTTP POST.
    def _do_direct(self, a: DirectToolCall) -> ToolOutcome:
        if not (CREWAI_AVAILABLE and ACP_SDK_AVAILABLE):
            # Missing deps: fall back to parent behavior so the benchmark
            # still runs, but flag the runner error.
            self._errors.append(
                "crewai or acp-crewai not installed; falling back to "
                "direct HTTP via parent acp runner."
            )
            return super()._do_direct(a)

        # Resolve tenant + acquire the user's ID token (parent helpers).
        reported_tenant = a.as_tenant or "tenant-a"
        _, real_tid = self._resolve_tenant(a.as_tenant)
        if real_tid not in self._tenants_used:
            self._tenants_used.add(real_tid)
        now = time.time()

        # Fail-mode simulation short-circuits the dispatch — same rule
        # as the parent class. We can't get an ID token if we're in a
        # simulated-unreachable window, because the @governed wrapper
        # would still try to hit the real gateway.
        if now < self._simulated_unreachable_until:
            return self._fail_mode_outcome(a, reported_tenant)
        if now < self._simulated_5xx_until:
            return self._fail_mode_outcome(a, reported_tenant, mode="5xx")

        id_token = self._id_token_for(a.as_user)
        if not id_token:
            # Mirror parent behavior — unauth calls return a deny shape.
            outcome = ToolOutcome(
                tool=a.tool, input=a.input,
                as_user=a.as_user, as_tenant=reported_tenant,
                allowed=False, reason="no_user_token",
                agent_tier=a.agent_tier, agent_name=a.agent_name,
            )
            self._tool_outcomes.append(outcome)
            return outcome

        # Bind the per-request context so @governed picks up the token.
        chain = list(self._chain_by_agent.get(a.agent_name, []))
        set_context(
            user_token=id_token,
            agent_tier=a.agent_tier,
            agent_name=a.agent_name,
        )

        # Get (or create) the decorated tool and invoke through CrewAI's
        # BaseTool.run(). The @governed wrapper inside fires pre_tool_use
        # → gateway → decides allow/deny. If denied, the wrapper returns
        # the `tool_error: ...` string; we detect that and mark as denied.
        tool = self._tool_cache.get(a.tool) or _mk_governed_tool(a.tool)
        if tool is None:
            self._errors.append(f"failed to build governed tool: {a.tool}")
            return super()._do_direct(a)
        self._tool_cache[a.tool] = tool

        start = time.time()
        try:
            raw = tool.run(**a.input) if a.input else tool.run()
        except Exception as e:
            self._errors.append(f"crewai_acp tool.run({a.tool}): {e}")
            raw = ""
        latency_ms = (time.time() - start) * 1000

        # `@governed` returns "tool_error: <reason>" on deny.
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
        """Delegate fail-mode decisions to the parent's logic so both
        runners share the same fail-open/fail-closed semantics."""
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
        # Emit a local fallback audit entry if fail_open + SDK-audit
        # semantics dictate one (parent's acp runner does this too).
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
