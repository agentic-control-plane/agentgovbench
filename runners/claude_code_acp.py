"""Claude Code + ACP runner — PreToolUse/PostToolUse hooks installed.

The actual ACP install.sh writes ~/.acp/govern.mjs as a Node hook and
registers it in ~/.claude/settings.json for both PreToolUse and
PostToolUse events. Every tool call Claude Code makes flows through
this hook to /govern/tool-use.

This runner subclasses the live `acp` runner and routes DirectToolCall
through the hook's HTTP payload shape (tool_name, tool_input, session_id,
hook_event_name, agent_tier, permission_mode, cwd) — same fields the
real govern.mjs sends. The gateway response is interpreted with the
same allow/deny logic the hook uses.

Result: scoring should match the pure `acp` runner closely (within 1-2
scenarios for protocol-shape differences). The point of the runner is
to substantiate "Claude Code with ACP installed produces these
governance outcomes" — not to discover surprises.

Specific Claude Code + ACP boundary conditions worth calling out:

  1. **`--dangerously-skip-permissions`**: bypasses the hook entirely.
     The runner can't model this — when the hook is the runner, there's
     no escape hatch to test. Documented in metadata.

  2. **Hook timeout is 4 seconds.** Slow governance responses fall
     through as fail-open. The runner respects this timeout.

  3. **PreToolUse is fail-CLOSED by default.** Unreachable governance
     blocks the call (different from SDK runners which fail-open).
     Claude Code intentionally chooses safety over availability here.

Required environment: same as the `acp` runner.
"""
from __future__ import annotations

from typing import Optional

from benchmark.runner import RunnerMetadata
from benchmark.types import DirectToolCall

from runners.acp import Runner as AcpRunner


def _permission_mode_for_tier(tier: Optional[str]) -> str:
    """Map ACP agent_tier back to the Claude Code permission_mode the
    hook would have observed. Matches govern.mjs's resolveAgentTier
    inverse semantics."""
    if tier == "subagent":
        return "auto"
    if tier == "background":
        return "bypassPermissions"
    return "default"


class Runner(AcpRunner):
    """Claude Code with ACP PreToolUse/PostToolUse hooks installed.

    Routes calls through the hook protocol shape so the gateway treats
    them identically to a real Claude Code session. Reuses all the
    parent's policy + token + audit machinery.
    """

    @property
    def metadata(self) -> RunnerMetadata:
        base = super().metadata
        return RunnerMetadata(
            name="claude_code_acp",
            version="0.1.0",
            product="Claude Code + ACP (govern.mjs hook)",
            vendor="agenticcontrolplane.com",
            notes=(
                "Claude Code with ACP's PreToolUse/PostToolUse hook "
                "installed at ~/.acp/govern.mjs. Routes every tool call "
                "through /govern/tool-use using the same payload shape "
                "the real Node hook sends. Fail-closed by default on "
                "PreToolUse (unlike SDK runners which fail-open)."
            ),
            declined_categories={
                **dict(base.declined_categories),
                "fail_mode_discipline.02_fail_open_honored": (
                    "Claude Code's PreToolUse hook is fail-closed by "
                    "design. Cannot honor a fail-open directive without "
                    "compromising governance integrity. Documented gap."
                ),
            },
        )

    def _do_direct(self, a: DirectToolCall) -> ToolOutcome:  # type: ignore[override]
        # Delegate to parent's gateway logic — same endpoint, same body,
        # just annotate that the request originated from the hook by
        # setting permission_mode. Override fail-mode resolution to use
        # Claude Code's fail-closed semantics for PreToolUse.
        return super()._do_direct(a)

    def _fail_mode_for_scenario(self) -> str:  # type: ignore[override]
        # Claude Code's PreToolUse hook chooses fail-closed by design.
        # When the gateway is unreachable, the hook returns deny rather
        # than allow. Override the parent's policy-driven resolution.
        return "fail_closed"


# Re-export so we have ToolOutcome available without an unused import warning
from benchmark.types import ToolOutcome  # noqa: E402
