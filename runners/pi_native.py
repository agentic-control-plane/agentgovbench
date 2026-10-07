"""pi with no governance layer — the harness-native baseline.

pi ships four tools and, by explicit design, no permission system, no
sandbox, and no MCP layer. Its own security docs say so: "pi does not
include a built-in sandbox", and project trust "is not a sandbox … only an
input-loading guard", resolved at startup and never consulted for a tool
call.

That makes it the cleanest baseline in the set. Where a Claude Code
baseline measures Claude Code's classifier as much as anything else, this
measures an agent with nothing in front of it — every tool call runs.

It is also a known-answer subject: it should behave like the permissive
floor in tests/test_discrimination.py. If it ever passes an enforcement
scenario, either the scenario is hollow or the driver is lying, and both
are worth failing over.
"""
from __future__ import annotations

from benchmark.runner import RunnerMetadata

from runners.pi_base import SUBSTRATE_DECLINED, PiRunner


class Runner(PiRunner):
    extensions: list[str] = []          # nothing attached
    driver_env: dict[str, str] = {}

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="pi_native",
            version="0.1.0",
            governance_source="none",
            product="pi (no governance layer)",
            vendor="earendil-works/pi",
            notes=(
                "A real pi agent session driven through a scripted tool-call "
                "sequence with no model in the loop and no governance "
                "extension attached. pi has no permission system or sandbox "
                "by design, so every call executes. Baseline for what the "
                "harness does on its own."
            ),
            declined_categories={
                **{f"{cat} (whole category)": why for cat, why in SUBSTRATE_DECLINED.items()},
                "fail_mode_discipline (whole category)": (
                    "There is no governance layer to become unreachable. A "
                    "fail mode is a property of a control plane, and this "
                    "subject has none."
                ),
            },
        )

    def _apply_non_call_action(self, action) -> None:
        # No governance to configure, no outage to induce. Policy changes and
        # gateway failures are no-ops rather than errors — recording them as
        # runner errors would flag a subject for lacking a thing it never
        # claimed to have.
        return None
