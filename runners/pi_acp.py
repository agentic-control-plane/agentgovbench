"""pi + ACP — the harness with a control plane attached.

Same substrate and same scripted calls as `pi_native`, with ACP's own pi
extension loaded. The pair is the comparison that matters: identical
harness, identical scenario, one column with a control plane and one
without. Nothing about the driver differs between them, so any delta is
the control plane.

The extension is the SHIPPED one from the pi-acp-plugin repo, not a
reimplementation. It posts every `tool_call` to /govern/tool-use and maps
the verdict onto pi's result contract — allow to undefined, deny to
{block, reason}, ask to a UI confirm that blocks when unattended.

Environment:
  ACP_API_KEY        (required) benchmark-tenant key; passed to the plugin
                     as ACP_BEARER_TOKEN so it never reads ~/.acp/credentials
                     and can never act as the operator's own workspace.
  ACP_BASE_URL       (optional) default https://api.agenticcontrolplane.com
  AGB_PI_PLUGIN      (optional) path to the plugin's index.ts

Policy fixtures are installed the same way the HTTP runner does it, under
the operator's own credentials — ACP refuses policy writes from an
agent-held key on principle, and the benchmark honours that rather than
routing around it.
"""
from __future__ import annotations

import os
from pathlib import Path

from benchmark.runner import RunnerMetadata

from runners.pi_base import SUBSTRATE_DECLINED, PiRunner

DEFAULT_PLUGIN = Path("/Users/dev/dev/pi-acp-plugin/index.ts")


class Runner(PiRunner):
    # The plugin reads the tier from this once per process, so the base
    # class groups calls by tier and runs one session per group.
    tier_env_var = "ACP_AGENT_TIER"

    def __init__(self) -> None:
        super().__init__()
        key = os.environ.get("ACP_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "ACP_API_KEY not set. Mint a benchmark-tenant key; it is passed "
                "to the plugin as ACP_BEARER_TOKEN so the run cannot fall back "
                "to ~/.acp/credentials and act as your own workspace."
            )
        plugin = Path(os.environ.get("AGB_PI_PLUGIN", str(DEFAULT_PLUGIN)))
        if not plugin.exists():
            raise RuntimeError(f"pi-acp plugin not found at {plugin}")

        self.extensions = [str(plugin)]
        self.driver_env = {
            "ACP_BEARER_TOKEN": key,
            "ACP_GOVERN_BASE": os.environ.get(
                "ACP_BASE_URL", "https://api.agenticcontrolplane.com",
            ),
            # Receipts and shadow notices are console chatter here; the
            # benchmark reads decisions, not the operator-facing surface.
            "ACP_SHADOW": "off",
        }

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="pi_acp",
            version="0.1.0",
            product="pi + Agentic Control Plane",
            vendor="agenticcontrolplane.com",
            notes=(
                "The shipped pi-acp-plugin attached to a real pi session, "
                "driven through the same scripted calls as pi_native. Every "
                "tool_call posts to /govern/tool-use; the verdict maps onto "
                "pi's block/allow contract. Same substrate as the baseline, "
                "so the delta is the control plane and nothing else."
            ),
            declined_categories={
                **{f"{cat} (whole category)": why for cat, why in SUBSTRATE_DECLINED.items()},
            },
        )

    # ── Scenario policy fixtures ───────────────────────────────────────
    #
    # Installed by composing the tested acp_api runner rather than copying
    # its translation a third time. That runner already carries the pieces
    # this needs: scenario Policy -> ACP policy doc, the Firestore write
    # under the operator's own credentials (ACP refuses policy writes from
    # an agent-held key on principle, and we honour that), the
    # isBenchmarkTenant guard, and stale per-user cleanup between scenarios.

    def _fixtures(self):
        if getattr(self, "_fx", None) is None:
            import os as _os
            if _os.environ.get("AGB_POLICY_SETUP") != "firestore":
                raise RuntimeError(
                    "pi_acp needs AGB_POLICY_SETUP=firestore to install scenario "
                    "policy. ACP refuses policy writes from an API key, so "
                    "fixtures run as the operator under their own credentials."
                )
            from runners.acp_api import Runner as ApiRunner
            self._fx = ApiRunner()
        return self._fx

    def setup(self, scenario) -> None:
        super().setup(scenario)
        if self._declined:
            return
        try:
            fx = self._fixtures()
            fx.setup_policy_only(scenario)
        except Exception as e:
            # Loud, never silent. A swallowed setup step is what produced two
            # meaningless full runs earlier this week.
            self._errors.append(f"fixture install failed: {e!r}")

    def _apply_non_call_action(self, action) -> None:
        from benchmark.types import PolicyChange
        if isinstance(action, PolicyChange):
            try:
                self._fixtures()._apply_policy_change(action)
            except Exception as e:
                self._errors.append(f"policy change failed: {e!r}")
            return
        self._errors.append(
            f"{type(action).__name__} not yet handled by pi_acp — "
            "scenario setup incomplete, treat its result as unmeasured"
        )
