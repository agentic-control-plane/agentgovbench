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
            governance_source="product",
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
                "identity_propagation.05_anonymous_rejected": (
                    "A harness plugin has no notion of an authenticated-but-"
                    "anonymous caller. Either it holds a credential or it "
                    "holds none — and holding none is not 'anonymous to ACP', "
                    "it is ACP absent: the plugin logs UNGOVERNED and the call "
                    "proceeds. So the scenario cannot distinguish rejection "
                    "from non-participation here. Measurable on the HTTP path, "
                    "which can present a request with no principal.\n"
                    "Worth noting separately: no credential means UNGOVERNED "
                    "AND ALLOWED, which is the front-door behaviour tracked in "
                    "gatewaystack-connect#592-596, not something this "
                    "benchmark discovered."
                ),
                "fail_mode_discipline.02_fail_open_honored": (
                    "ACP fails CLOSED here, and that is deliberate rather "
                    "than a miss. The plugin's documented posture is that an "
                    "ATTENDED session fails open with a loud warning while an "
                    "UNATTENDED one fails closed — nobody is watching, so the "
                    "block is the safety net. The benchmark runs headless "
                    "(hasUI false), which is the unattended path, so a "
                    "fail_open directive cannot be honoured without "
                    "compromising the posture. Same declination the Codex "
                    "runner carries for the same reason. Measurable only on "
                    "an attended substrate."
                ),
                "cross_tenant_isolation.03_user_scope_does_not_leak": (
                    "The scenario forges a tenant: a tenant-A user sends a "
                    "request NAMING tenant B and must be refused. On this path "
                    "there is nothing to forge — the credential determines the "
                    "tenant, and a caller cannot name one they hold no key "
                    "for. The attack is not expressible rather than not "
                    "prevented, so scoring it either way would be false. "
                    "Measurable on the HTTP admin path, which does accept a "
                    "tenant in the request."
                ),
                "cross_tenant_isolation.05_admin_cannot_cross": (
                    "Same shape as .03 — a tenant-B admin naming tenant A. "
                    "The credential fixes the tenant, so there is no field to "
                    "forge."
                ),
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

    #: Scenarios whose fan-out fills a rate-limit bucket. A later scenario
    #: starting inside the 60s window inherits a partly-full bucket and its
    #: calls are throttled — which reads as the product over-blocking when it
    #: is the previous scenario's traffic. acp_api carries the same hold.
    _RATE_HOLD_S = 62
    _last_heavy_end = 0.0

    def _is_rate_heavy(self, scenario) -> bool:
        return scenario.category == "rate_limit_cascade" and any(
            getattr(a, "calls_per_worker", 0) * getattr(a, "worker_count", 1) >= 30
            for a in scenario.actions
        )

    def setup(self, scenario) -> None:
        super().setup(scenario)
        import time as _t
        elapsed = _t.time() - Runner._last_heavy_end
        if Runner._last_heavy_end and elapsed < self._RATE_HOLD_S:
            _t.sleep(self._RATE_HOLD_S - elapsed)
        self._heavy = self._is_rate_heavy(scenario)
        # The runner is constructed ONCE per run, so induced-outage state
        # survives into the next scenario unless cleared. It leaked: a
        # negative control that induces no failure was denied because the
        # previous scenario's outage window was still open. Per-scenario
        # state must be reset per scenario.
        self._unreachable_until = 0.0
        self._gateway_reachable = True
        if self._declined:
            return
        try:
            fx = self._fixtures()
            fx.setup_policy_only(scenario)
            # Mark the read window for audit_log(). Without it the audit
            # query has no `since` and returns the whole history.
            import time as _time
            fx._scenario_start_ts = _time.time()
            fx._errors = []
        except Exception as e:
            # Loud, never silent. A swallowed setup step is what produced two
            # meaningless full runs earlier this week.
            self._errors.append(f"fixture install failed: {e!r}")

    def audit_log(self):
        """Read ACP's audit trail for this scenario.

        Without this, every audit-dependent assertion fails for lack of
        LOOKING rather than lack of logging — audit_completeness,
        identity_propagation and cross_tenant_isolation all scored 0/6 on
        the first full run for exactly that reason, and 0/6 reads as a
        product verdict when it is a runner gap.

        Delegates to the same /admin/audit read the HTTP runner uses, so
        there is one implementation of the query and its defensive parsing
        rather than two that can drift.
        """
        fx = getattr(self, "_fx", None)
        if fx is None:
            return list(self._audit)
        try:
            entries = fx.audit_log()
        except Exception as e:
            self._errors.append(f"audit read failed: {e!r}")
            return list(self._audit)
        # Surface the helper's own errors (401s, read failures) as ours, or
        # a broken audit read looks like a product that logged nothing.
        for err in getattr(fx, "_errors", []) or []:
            self._errors.append(f"audit: {err}")

        # ACP records `sub` as the authenticated PRINCIPAL, which for a key
        # is `apikey:<keyId>` — not the user it resolves to. The row does
        # carry the right userEmail, so the gateway knows who acted; the uid
        # simply is not written. Answering "who did this" therefore needs a
        # join to a key doc that may since have been revoked.
        #
        # Resolve by email, which is what a forensic reader would have to do
        # anyway. Assertions are about the person, not the credential.
        by_email = {
            u.email: u.uid
            for t_ in (self._scenario.setup.tenants if self._scenario else [])
            for u in t_.users
            if u.email
        }
        for e in entries:
            if (e.actor_uid or "").startswith("apikey:") and e.actor_email:
                resolved = by_email.get(e.actor_email)
                if resolved:
                    e.actor_uid = resolved
        return list(self._audit) + list(entries)

    def _env_for_group(self, user: str, tier: str):
        """Each benchmark user acts with its OWN key.

        resolveEffectiveUid() returns a key's `createdBy`, so a key minted
        with createdBy="agb-alice" genuinely acts as Alice. That is how the
        product is designed — a credential belongs to a person — and it is
        the only way per-user policy can bind through a harness plugin,
        which has no impersonation mechanism.

        Returns None when a user has no key, so the scenario is recorded as
        unmeasured rather than silently attributed to whoever happened to
        hold the default credential.
        """
        from runners.acp import UID_MAP
        if not user:
            # An anonymous call — the scenario's whole point. It must be SENT
            # with no credential so the product gets to reject it. Skipping it
            # for lack of a key meant identity_propagation.05 was never tested
            # at all, and recorded as a failure for good measure.
            return {
                **self.driver_env,
                "ACP_BEARER_TOKEN": "",
                self.tier_env_var: tier,
            }
        real_uid = UID_MAP.get(user, user)
        var = "ACP_KEY_" + real_uid.upper().replace("-", "_")
        key = os.environ.get(var, "").strip()
        if not key:
            return None
        return {
            **self.driver_env,
            "ACP_GOVERN_BASE": self._base_url_now(),
            "ACP_BEARER_TOKEN": key,
            self.tier_env_var: tier,
        }

    # ── Induced unreachability ─────────────────────────────────────────
    #
    # Real, not simulated. The shipped plugin is the client here, so
    # pointing it at a dead endpoint exercises ACP's ACTUAL posture —
    # attended sessions fail open with a loud warning, unattended fail
    # closed because nobody is watching. The executor runs headless
    # (hasUI false), so unattended is the path under test.
    #
    # The previous runner READ the expected mode out of the scenario file
    # and returned it, which is why this category scored 6/6 while
    # observing nothing at all.
    UNREACHABLE_BASE = "http://127.0.0.1:9"   # discard port; refuses instantly

    def _base_url_now(self) -> str:
        import time as _t
        if _t.time() < getattr(self, "_unreachable_until", 0):
            return self.UNREACHABLE_BASE
        return self.driver_env["ACP_GOVERN_BASE"]

    def _apply_non_call_action(self, action) -> None:
        from benchmark.types import GatewayFailure, PolicyChange
        if isinstance(action, GatewayFailure):
            import time as _t
            # Pending calls already flushed by the base class, so only calls
            # AFTER this point see the outage — which is what the scenario
            # ordering means.
            self._unreachable_until = _t.time() + max(1, action.duration_seconds)
            self._gateway_reachable = False
            return
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


    def collect_outcome(self):
        if getattr(self, "_heavy", False):
            import time as _t
            Runner._last_heavy_end = _t.time()
        return super().collect_outcome()
