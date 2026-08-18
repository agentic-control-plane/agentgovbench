"""pi + Microsoft Agent Governance Toolkit.

The competitor column. Same substrate, same scripted calls, same scenarios
as pi_acp — only the governance layer differs, so the comparison is between
control planes rather than between harnesses or drivers.

APPLES-TO-APPLES, and what that required
----------------------------------------
AGT attaches in-process; ACP attaches at a harness hook. Comparing them at
their own attachment points would compare integration surfaces, not
enforcement. So both decide at pi's `tool_call`, via drivers/pi/agt-extension.ts.
There is precedent: pi-dcg does the same for dcg, unaffiliated with either
of us.

Three choices deliberately favour AGT, because the adapter is written by
ACP's team and should lean the other way:

  * The RICH evaluation path (`evaluatePolicy`) with the full policy
    document model — conflict resolution, rate limits, approvals — not the
    legacy flat `evaluate(action)`.
  * Policy is generated into AGT's own documented schema from the same
    scenario the other subjects get. Nothing hand-tuned per scenario.
  * warn and log verdicts PROCEED, matching AGT's own definition of them as
    advisory. Treating them as gating would inflate its score.

Known translation seams, stated rather than hidden:

  * AGT scopes policy per agent (`agent` / `agents` on a Policy document).
    A benchmark user maps to an agentDid, so per-user policy is per-agent
    policy here.
  * `require_approval` blocks, because the benchmark is headless and nobody
    can approve. ACP's plugin takes the identical posture, so the substrate
    disadvantages neither.
  * AGT has no tenant concept. Cross-tenant scenarios are declined rather
    than scored zero — penalising a product for lacking a dimension it
    never claimed is exactly the N/A case SCORING.md reserves.

Environment:
  AGB_AGT_POLICY   written per scenario by this runner
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from benchmark.runner import RunnerMetadata

from runners.pi_base import SUBSTRATE_DECLINED, PiRunner

EXT = Path(__file__).resolve().parent.parent / "drivers" / "pi" / "agt-extension.ts"


def _limit(per_minute) -> str | None:
    return f"{per_minute}/minute" if per_minute else None


class Runner(PiRunner):
    tier_env_var = "AGB_AGT_TIER"

    def __init__(self) -> None:
        super().__init__()
        if not EXT.exists():
            raise RuntimeError(f"AGT extension not found at {EXT}")
        self.extensions = [str(EXT)]
        self._policy_file: str | None = None

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="pi_agt",
            version="0.1.0",
            product="pi + Microsoft Agent Governance Toolkit",
            vendor="microsoft/agent-governance-toolkit",
            notes=(
                "@microsoft/agent-governance-sdk 5.0.0 deciding at pi's "
                "tool_call event via a thin adapter, so it sits at the same "
                "interception point as every other subject. Uses the rich "
                "evaluatePolicy path with the full policy-document model. "
                "Adapter and generated policy are published; corrections "
                "from Microsoft welcome and will be re-run."
            ),
            declined_categories={
                **{f"{cat} (whole category)": why for cat, why in SUBSTRATE_DECLINED.items()},
                "cross_tenant_isolation (whole category)": (
                    "AGT has no tenant concept — policies scope to agents, "
                    "not tenants. Scoring zero would penalise it for lacking "
                    "a dimension it never claimed; this is the structural "
                    "inapplicability SCORING.md reserves N/A for."
                ),
                "fail_mode_discipline (whole category)": (
                    "AGT evaluates IN-PROCESS. There is no control plane to "
                    "become unreachable, so a fail mode is not a property it "
                    "has. Note the flip side, which belongs in the writeup "
                    "rather than the score: in-process evaluation cannot be "
                    "cut off by a network partition at all."
                ),
                "per_user_policy_enforcement.06_revoked_scope_immediate": (
                    "Needs a mid-scenario policy change, which this adapter "
                    "does not yet apply — AGT policy is loaded once at "
                    "session start. Unmeasured rather than failed; the "
                    "product may well handle revocation fine."
                ),
                "audit_completeness (whole category)": (
                    "The npm SDK exposes AuditLogger, but this adapter does "
                    "not wire it — reading an audit trail we did not "
                    "configure would score our integration rather than the "
                    "product. Declined until wired, and it should be wired."
                ),
            },
        )

    # ── Scenario policy → AGT policy documents ─────────────────────────

    def setup(self, scenario) -> None:
        super().setup(scenario)
        if self._declined:
            return
        docs = self._to_agt_policies(scenario)
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(docs, fh)
        fh.close()
        self._policy_file = fh.name

    def _to_agt_policies(self, scenario) -> list[dict]:
        """Translate the scenario's Policy into AGT's documented schema.

        One document per user, because AGT scopes to an agent. Tier defaults
        become rules conditioned on tier; tool overrides become
        higher-priority rules conditioned on tool AND tier; a user missing a
        tool's required scope becomes an explicit deny.
        """
        docs: list[dict] = []
        for t in scenario.setup.tenants:
            tools_by_name = {tl.name: tl for tl in scenario.setup.tools}
            for user in t.users:
                rules: list[dict] = []

                # Most specific first: AGT sorts by priority, higher first.
                for tool_name, tiers in (t.policy.tools or {}).items():
                    for tier, tp in tiers.items():
                        rules.append({
                            "name": f"tool-{tool_name}-{tier}",
                            "condition": {"tool": tool_name, "tier": tier},
                            "ruleAction": "deny" if tp.permission == "deny" else "allow",
                            # Workspace-scoped: beats defaults, loses to
                            # anything user-specific.
                            "priority": 70,
                            **({"limit": _limit(tp.rate_limit_per_minute)}
                               if tp.rate_limit_per_minute else {}),
                        })

                # Per-user PER-TOOL overrides — the most specific policy the
                # scenario can express. Omitting these entirely was why
                # .03_user_override_beats_workspace failed: the rule it
                # tests was never translated, so AGT was scored on a policy
                # that did not contain it.
                for tool_name, tiers in (t.policy.user_tools or {}).get(user.uid, {}).items():
                    for tier, tp in tiers.items():
                        rules.append({
                            "name": f"usertool-{user.uid}-{tool_name}-{tier}",
                            "condition": {"tool": tool_name, "tier": tier},
                            "ruleAction": "deny" if tp.permission == "deny" else "allow",
                            "priority": 110,
                        })

                for tier, tp in (t.policy.users or {}).get(user.uid, {}).items():
                    rules.append({
                        "name": f"user-{user.uid}-{tier}",
                        "condition": {"tier": tier},
                        "ruleAction": "deny" if tp.permission == "deny" else "allow",
                        # User-scoped beats workspace — the most-specific-wins
                        # precedence both products document.
                        "priority": 90,
                    })

                # A user lacking a tool's required scope is denied that tool.
                for tool_name, tool in tools_by_name.items():
                    if tool.required_scopes and not all(
                        s in user.scopes for s in tool.required_scopes
                    ):
                        rules.append({
                            "name": f"scope-{user.uid}-{tool_name}",
                            "condition": {"tool": tool_name},
                            "ruleAction": "deny",
                            # User AND tool specific: the most specific fact
                            # in the scenario, so it outranks everything.
                            "priority": 100,
                        })

                for tier, tp in (t.policy.defaults or {}).items():
                    rules.append({
                        "name": f"default-{tier}",
                        "condition": {"tier": tier},
                        "ruleAction": "deny" if tp.permission == "deny" else "allow",
                        "priority": 10,
                        **({"limit": _limit(tp.rate_limit_per_minute)}
                           if tp.rate_limit_per_minute else {}),
                    })

                docs.append({
                    "apiVersion": "governance.toolkit/v1",
                    "name": f"agb-{t.id}-{user.uid}",
                    "agent": user.uid,
                    "rules": rules,
                    # Mirrors ACP: a tenant in enforce mode with no matching
                    # rule allows. Defaulting to deny here would make AGT
                    # look stricter than the scenario asks for.
                    "default_action": "allow",
                })
        return docs

    def _env_for_group(self, user: str, tier: str):
        return {
            **self.driver_env,
            "AGB_AGT_POLICY": self._policy_file or "",
            "AGB_AGT_AGENT": user,
            "AGB_AGT_USER": user,
            self.tier_env_var: tier,
        }

    def _apply_non_call_action(self, action) -> None:
        from benchmark.types import PolicyChange
        if isinstance(action, PolicyChange):
            self._errors.append(
                "mid-scenario PolicyChange not yet wired for AGT — this "
                "scenario is unmeasured, not failed"
            )
            return
        self._errors.append(f"{type(action).__name__} not handled by pi_agt")
