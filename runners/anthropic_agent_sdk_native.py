"""Anthropic Agent SDK native runner — direct Anthropic client, no governance.

Represents what you get running the Anthropic SDK / Claude Agent SDK
with the default Anthropic client and no governance wrapper. Tool
handlers run directly with no policy check, no audit, no per-user
identity beyond the SDK's single API key.

The Anthropic Agent SDK ships with:
  - Tool-use loops around Claude (messages.create with tools)
  - Handler maps for tool dispatch
  - Optional thinking/extended-thinking blocks
  - Session/state primitives in newer SDK versions

It does NOT ship with:
  - Per-end-user identity propagation
  - Workspace policy
  - Per-tool scope enforcement
  - SIEM-ingestible audit log
  - Rate-limit cascade discipline
  - Fail-mode semantics

Specific Anthropic SDK failure modes:

  1. **One ANTHROPIC_API_KEY per process.** All tool calls attribute to
     the deployment's API key. The end user's identity exists nowhere
     in the request envelope.

  2. **Handler exceptions don't audit.** A handler that throws (real
     bug or deliberate signal) is invisible to a governance layer
     unless the application catches and emits an event itself.

  3. **Extended-thinking blocks aren't governed.** When Claude reasons
     about a tool call before invoking it, the reasoning passes through
     no policy check. Today this is fine (thinking ≠ acting); becomes
     interesting if/when thinking can drive non-tool side effects.

Scorecard expectation: vanilla floor (13/48). Same pattern as the other
framework natives — without explicit governance wiring, you get nothing.

Note: this is the **TypeScript** SDK. We benchmark via a Python runner
(this file) that represents the SDK's tool dispatch path in a language-
agnostic way. The integration target is `@agenticcontrolplane/governance-anthropic`
on npm.

Version: tested against @anthropic-ai/sdk@0.50+
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
    """Anthropic Agent SDK with direct Anthropic client. No governance."""

    def __init__(self) -> None:
        super().__init__()
        self._chain_by_agent: dict[str, list[str]] = {}

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="anthropic_agent_sdk_native",
            version="0.1.0",
            product="Anthropic Agent SDK (no governance wrapper)",
            vendor="anthropic.com",
            notes=(
                "Anthropic Agent SDK / Claude Agent SDK with default "
                "Anthropic client and no governHandlers wrapper. "
                "Represents a fresh deployment before any governance "
                "work. Tool handlers run directly; no audit layer outside "
                "the deployment's own logging."
            ),
            declined_categories={
                "cross_tenant_isolation": (
                    "Anthropic Agent SDK has no tenant concept. Multi-"
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
            allowed=True, reason="anthropic_native_allows_all",
            agent_tier=a.agent_tier, agent_name=a.agent_name,
        )
        self._tool_outcomes.append(outcome)
        return outcome
