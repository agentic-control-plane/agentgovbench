"""OpenAI Agents SDK + ACP runner — OpenAI-compatible proxy pattern.

Different shape from the decorator runners: ACP for OpenAI Agents SDK
is not @governed, it's a base_url swap. The SDK's AsyncOpenAI client
points at api.agenticcontrolplane.com/v1 instead of api.openai.com.
Every LLM call (and the tool calls the LLM emits) passes through the
ACP proxy, which audits them at the proxy layer.

For the benchmark, that means we don't intercept tool functions — we
record what would have hit the gateway as if it had come through the
chat-completions proxy. The action sequence describes "user makes a
tool call"; the runner translates that to the gateway HTTP request the
proxy would have synthesized.

Reuses all of acp.Runner's machinery — same gateway, same auth, same
audit reads. The point of this runner is to claim "OpenAI Agents SDK
through the ACP proxy produces these scores."

Per-agent attribution uses the x-acp-agent-name header (set on the
client at agent-construction time per the integration doc).

Required environment: same as acp runner.
"""
from __future__ import annotations

from benchmark.runner import RunnerMetadata
from benchmark.types import DirectToolCall, ToolOutcome

from runners.acp import Runner as AcpRunner


class Runner(AcpRunner):
    """OpenAI Agents SDK pointed at ACP's OpenAI-compatible proxy."""

    @property
    def metadata(self) -> RunnerMetadata:
        base = super().metadata
        return RunnerMetadata(
            name="openai_agents_acp",
            version="0.1.0",
            product="OpenAI Agents SDK + ACP (proxy)",
            vendor="agenticcontrolplane.com",
            notes=(
                "OpenAI Agents SDK with AsyncOpenAI client base_url set "
                "to https://api.agenticcontrolplane.com/v1. Every LLM "
                "call and emitted tool call passes through ACP's proxy. "
                "Per-agent attribution via x-acp-agent-name header. "
                "Same governance pipeline as the `acp` runner; reaches "
                "the gateway via the chat-completions endpoint instead "
                "of /govern/tool-use directly."
            ),
            declined_categories={
                **dict(base.declined_categories),
                "fail_mode_discipline.02_fail_open_honored": (
                    "Proxy pattern: when the proxy is unreachable the "
                    "OpenAI client itself returns the network error to "
                    "the SDK. Fail-open at the application layer is the "
                    "responsibility of the SDK consumer, not the proxy."
                ),
            },
        )

    # Inherits _do_direct from AcpRunner. The proxy pattern produces
    # identical gateway-side observables to the direct-HTTP pattern;
    # this runner exists to credibly claim "OpenAI Agents SDK + ACP
    # produces these scores" rather than to discover novel behavior.
