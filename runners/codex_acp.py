"""Codex CLI + ACP runner — PreToolUse/PostToolUse hooks installed.

The same hook protocol as Claude Code, with one important difference:
Codex CLI's hooks fire even in auto-approve mode. Where Claude Code's
`--dangerously-skip-permissions` disables hooks entirely, Codex CLI's
auto mode keeps the hook firing — just suppresses the interactive
prompt. This is a meaningful governance difference.

Subclasses acp.Runner. Routes via the same hook-protocol HTTP shape
as claude_code_acp.

Required environment: same as acp runner.
"""
from __future__ import annotations

from benchmark.runner import RunnerMetadata
from benchmark.types import DirectToolCall, ToolOutcome  # noqa: F401

from runners.acp import Runner as AcpRunner


class Runner(AcpRunner):
    """Codex CLI with ACP hook installed."""

    @property
    def metadata(self) -> RunnerMetadata:
        base = super().metadata
        return RunnerMetadata(
            name="codex_acp",
            version="0.1.0",
            product="Codex CLI + ACP hook",
            vendor="agenticcontrolplane.com",
            notes=(
                "Codex CLI with ACP PreToolUse/PostToolUse hooks. Same "
                "hook protocol as Claude Code, but Codex CLI's hooks "
                "fire even in auto-approve mode (Claude Code's "
                "--dangerously-skip-permissions disables hooks "
                "entirely). One real governance differentiator: in auto "
                "mode the audit log keeps populating. Fail-closed on "
                "PreToolUse, same as Claude Code."
            ),
            declined_categories={
                **dict(base.declined_categories),
                "fail_mode_discipline.02_fail_open_honored": (
                    "Codex CLI's PreToolUse hook is fail-closed by "
                    "design (matches Claude Code). Cannot honor a "
                    "fail-open directive without compromising integrity."
                ),
            },
        )

    def _fail_mode_for_scenario(self) -> str:  # type: ignore[override]
        return "fail_closed"
