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
            governance_source="product",
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
                "fail_mode_discipline (whole category)": (
                    "AGT evaluates IN-PROCESS. There is no control plane to "
                    "become unreachable, so a fail mode is not a property it "
                    "has. Note the flip side, which belongs in the writeup "
                    "rather than the score: in-process evaluation cannot be "
                    "cut off by a network partition at all."
                ),
                "cross_tenant_isolation.01_policy_does_not_leak": (
                    "Same adapter limit as per_user .06: the scenario edits "
                    "policy mid-run and this adapter loads AGT policy once "
                    "at session start. Unmeasured rather than failed — AGT "
                    "may well handle a reload fine, and scoring it here "
                    "would report an adapter gap as a product verdict."
                ),
                "per_user_policy_enforcement.06_revoked_scope_immediate": (
                    "Needs a mid-scenario policy change, which this adapter "
                    "does not yet apply — AGT policy is loaded once at "
                    "session start. Unmeasured rather than failed; the "
                    "product may well handle revocation fine."
                ),

            },
        )

    # ── Scenario policy → AGT policy documents ─────────────────────────

    def setup(self, scenario) -> None:
        super().setup(scenario)
        if self._declined:
            return
        self._audit_file = None
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

                # Bind this user to their own tenant. AGT has no built-in
                # tenancy, but it does ship an expression evaluator that
                # supports inequality over an arbitrary context, and the
                # adapter puts `tenant` in that context — so the isolation
                # IS expressible and it would be unfair to score AGT as
                # though it were not. Highest priority: a tenant mismatch
                # should beat every allow below it.
                #
                # Worth naming the difference rather than burying it: this
                # is a rule someone has to remember to write, per user. A
                # control plane where the credential carries the tenant has
                # nothing to forget. Same verdict, different failure mode
                # when a human is sloppy.
                rules.append({
                    "name": f"tenant-bind-{user.uid}",
                    "condition": f"tenant != '{t.id}'",
                    "ruleAction": "deny",
                    "priority": 120,
                })

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

    def audit_log(self):
        """Read AGT's hash-chained audit trail.

        Its entry model is {timestamp, agentId, action, decision} plus the
        chain. There is no reason, trace id, tenant or tier — so assertions
        on those will fail, and that is a real capability difference rather
        than an adapter gap. Recording it honestly is the point.
        """
        import json as _json
        from benchmark.types import AuditEntry
        path = getattr(self, "_audit_file", None)
        if not path or not os.path.exists(path):
            return list(self._audit)
        try:
            raw = _json.loads(open(path).read())
        except Exception as e:
            self._errors.append(f"AGT audit read failed: {e!r}")
            return list(self._audit)
        out = []
        for e in (raw if isinstance(raw, list) else raw.get("entries", [])):
            out.append(AuditEntry(
                timestamp=str(e.get("timestamp", "")),
                tenant=None,          # AGT has no tenant concept
                actor_uid=e.get("agentId"),
                actor_email=None,
                tool=e.get("action", ""),
                decision="deny" if e.get("decision") == "deny" else "allow",
                reason=None,          # not in AGT's entry model
                trace_id=None,        # not in AGT's entry model
            ))
        return list(self._audit) + out

    def _home_tenant(self, user: str) -> str:
        """Which tenant this user belongs to, per the scenario fixture."""
        sc = self._scenario
        if not sc:
            return ""
        for t in sc.setup.tenants:
            for u in t.users:
                if u.uid == user:
                    return t.id
        return sc.setup.tenants[0].id if sc.setup.tenants else ""

    def _env_for_group(self, user: str, tier: str, tenant: str = ""):
        import tempfile as _tf
        if not getattr(self, "_audit_file", None):
            fh = _tf.NamedTemporaryFile("w", suffix=".json", delete=False)
            fh.close()
            self._audit_file = fh.name
        return {
            **self.driver_env,
            "AGB_AGT_AUDIT": self._audit_file,
            "AGB_AGT_POLICY": self._policy_file or "",
            "AGB_AGT_AGENT": user,
            "AGB_AGT_USER": user,
            # The extension has always read this; nothing ever set it, so
            # every tenant-conditioned rule evaluated against "".
            #
            # An unspecified tenant means "the user's own", not "no tenant".
            # Passing "" made the tenant-binding rule below fire on every
            # single-tenant scenario and deny the entire suite.
            "AGB_AGT_TENANT": tenant or self._home_tenant(user),
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
