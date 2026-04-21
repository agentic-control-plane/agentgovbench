"""Anthropic Agent SDK + ACP runner — governHandlers wraps the handler map.

The TypeScript integration is `@agenticcontrolplane/governance-anthropic`,
which exports `governHandlers(handlers)` and `withContext(token, fn)`.
Same governance pipeline as the decorator-pattern Python SDKs — same
/govern/tool-use endpoint.

For benchmark purposes, this runner produces the same gateway-side
observables as routing the call directly: identical pre/post hooks
fire with identical payload. Reuses acp.Runner's machinery.

The runner exists to substantiate the claim "Anthropic Agent SDK + ACP
scores X" — exact behavior matches the `acp` runner with only minor
divergences for SDK-specific request-shape differences.

Required environment: same as acp runner.
"""
from __future__ import annotations

from benchmark.runner import RunnerMetadata
from benchmark.types import DirectToolCall, ToolOutcome

from runners.acp import Runner as AcpRunner


class Runner(AcpRunner):
    """Anthropic Agent SDK with @agenticcontrolplane/governance-anthropic."""

    @property
    def metadata(self) -> RunnerMetadata:
        base = super().metadata
        return RunnerMetadata(
            name="anthropic_agent_sdk_acp",
            version="0.1.0",
            product="Anthropic Agent SDK + ACP (governHandlers)",
            vendor="agenticcontrolplane.com",
            notes=(
                "Anthropic Agent SDK with handler map wrapped via "
                "@agenticcontrolplane/governance-anthropic governHandlers. "
                "Same governance pipeline as the `acp` runner; "
                "withContext binds end-user JWT per request, governHandlers "
                "wraps each tool handler for pre/post checks."
            ),
            declined_categories=dict(base.declined_categories),
        )

    # Inherits _do_direct from AcpRunner. Identical gateway observables.
