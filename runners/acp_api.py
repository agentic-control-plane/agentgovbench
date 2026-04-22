"""ACP API-key runner — Firebase-free reproducibility path.

Same scoring behavior as `runners/acp.py` but reaches the gateway using
ONLY public HTTP + an ACP API key. Zero Firebase Admin SDK. Zero service
account JSON. Dropping those dependencies is what turns the benchmark
from "you need our infrastructure to reproduce" into "you need one env
var to reproduce."

Requirements on the API key:
  - Must be a `gsk_` key minted on the target ACP deployment.
  - Must have `bench.impersonate` and `admin.audit.read` scopes
    (or `*` for full reproducibility).
  - The key's tenant must be the one the benchmark runs against.

Environment:
  ACP_API_KEY       (required) Bearer token used for all endpoints.
  ACP_BASE_URL      (optional) Default https://api.agenticcontrolplane.com
  ACP_TENANT_SLUG   (optional) Target tenant slug. Defaults to `agentgovbench`.

Design: subclass runners/acp.Runner. Override the handful of methods
that touch Firebase Admin SDK so governance/audit/policy writes route
through /admin/* endpoints instead. Everything else — chain tracking,
fail-mode simulation, per-action dispatch — is reused verbatim, so this
runner's scorecard should match the `acp` runner's to within one or two
scenarios that exercise very specific Firebase behaviors.

This runner exists to answer "I want to verify ACP's published benchmark
against my own deployment without getting anywhere near Firebase." Hand
someone an API key, they run one command, they get a scorecard.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from benchmark.runner import RunnerMetadata
from benchmark.types import (
    Action,
    AuditEntry,
    DirectToolCall,
    Delegation,
    GatewayFailure,
    ParallelFanOut,
    PolicyChange,
    ToolOutcome,
)
from runners.acp import (
    Runner as AcpRunner,
    UID_MAP,
    REVERSE_UID_MAP,
    TENANT_SLUG_MAP,
    REVERSE_TENANT_SLUG_MAP,
    EMAIL_MAP_REAL_TO_SCENARIO,
)


class Runner(AcpRunner):
    """ACP runner that uses only the public admin HTTP API + an API key."""

    def __init__(self) -> None:
        # Skip AcpRunner.__init__'s Firebase init; we don't need it.
        # Reach up two levels to StatefulRunner.__init__ for the shared
        # outcome/error tracking state.
        from benchmark.runner import StatefulRunner
        StatefulRunner.__init__(self)

        self._api_key = os.environ.get("ACP_API_KEY", "")
        if not self._api_key:
            raise RuntimeError(
                "ACP_API_KEY not set. Mint a gsk_ API key on your ACP "
                "deployment with scopes `bench.impersonate` and "
                "`admin.audit.read`, then export it."
            )

        self._acp_base_url = os.environ.get(
            "ACP_BASE_URL", "https://api.agenticcontrolplane.com",
        )
        # Tenant slug the key was minted for — used to build /:slug/ paths.
        self._tenant_slug = os.environ.get("ACP_TENANT_SLUG", "agentgovbench")
        # Map known scenario tenant ids to real slugs. Cross-tenant
        # scenarios expect tenant-a and tenant-b to map somewhere;
        # without an explicit second slug we use the primary for both
        # (cross-tenant isolation scenarios may score differently as a
        # result, same declination as the parent runner).
        self._tenant_by_slug: dict[str, str] = {
            self._tenant_slug: self._tenant_slug,
        }
        self._simulated_unreachable_until = 0.0
        self._simulated_5xx_until = 0.0
        self._chain_by_agent: dict[str, list[str]] = {}
        self._delegated_scopes_by_agent: dict[str, set[str]] = {}
        self._local_audit_entries: list[AuditEntry] = []
        self._scenario_start_ts: float = 0.0
        self._tenants_used: set[str] = set()

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="acp_api",
            version="0.1.0",
            product="Agentic Control Plane (API-key runner)",
            vendor="agenticcontrolplane.com",
            notes=(
                f"Reaches {self._acp_base_url} using ACP_API_KEY only. "
                "No Firebase Admin SDK. Policies written via "
                "/admin/workspacePolicy + /admin/userPolicies; user-scope "
                "calls impersonated via body param with bench.impersonate "
                "scope; audit read via /admin/audit. Use this to reproduce "
                "the ACP scorecard against a deployment you control."
            ),
            declined_categories={
                "scope_inheritance.04_task_narrowing": (
                    "ACP does not currently enforce task-scoped narrowing "
                    "on subagents; parent's effective scope flows to "
                    "children. Product roadmap item."
                ),
                "cross_tenant_isolation.02_audit_log_separation": (
                    "Requires writing to two tenants to test separation. "
                    "API-key runner is scoped to a single tenant — the one "
                    "the key was minted for. The reference Firebase-backed "
                    "runner tests this by writing to both tenants directly."
                ),
                "cross_tenant_isolation.03_user_scope_does_not_leak": (
                    "Requires multi-tenant deployment mode; API-key runner "
                    "talks to a single tenant."
                ),
                "cross_tenant_isolation.05_admin_cannot_cross": (
                    "Same as 03 — single-tenant API-key runner."
                ),
                "per_user_policy_enforcement.03_user_override_beats_workspace": (
                    "Tests user-scope tool-specific overrides; harness + "
                    "runner need types/YAML/write-path support for "
                    "user.tools. Gateway side is ready."
                ),
            },
        )

    # ── HTTP helpers ───────────────────────────────────────────────────

    def _admin_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-GS-Client": "agentgovbench-acp-api/0.1.0",
        }

    def _resolve_tenant(self, scenario_tenant_id: Optional[str]) -> tuple[str, str]:
        # API runner talks to exactly one tenant (the key's). All scenario
        # tenant ids resolve to that single slug. Cross-tenant scenarios
        # are declined above.
        return self._tenant_slug, self._tenant_slug

    # ── Policy write — via /admin endpoints ────────────────────────────

    def _write_policy(self, tenant_id: str, policy: dict[str, Any]) -> None:
        """Write workspace + per-user policies via the admin REST API."""
        base = f"{self._acp_base_url}/{self._tenant_slug}"

        # Workspace policy (defaults + tools).
        workspace_body = {
            "mode": policy.get("mode", "enforce"),
            "defaults": policy.get("defaults", {}),
            "tools": policy.get("tools", {}),
        }
        try:
            r = requests.put(
                f"{base}/admin/workspacePolicy",
                headers=self._admin_headers(),
                json=workspace_body,
                timeout=10,
            )
            if not r.ok:
                self._errors.append(f"workspacePolicy PUT {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            self._errors.append(f"workspacePolicy PUT failed: {e!r}")

        # Per-user policies (defaults + tools under user).
        users_pol = policy.get("users", {}) or {}
        for uid, user_doc in users_pol.items():
            body = {
                "defaults": user_doc.get("defaults", {}),
                "tools": user_doc.get("tools", {}),
            }
            try:
                r = requests.put(
                    f"{base}/admin/userPolicies/{uid}",
                    headers=self._admin_headers(),
                    json=body,
                    timeout=10,
                )
                if not r.ok:
                    self._errors.append(
                        f"userPolicies PUT {uid} {r.status_code}: {r.text[:200]}",
                    )
            except requests.RequestException as e:
                self._errors.append(f"userPolicies PUT {uid} failed: {e!r}")

    def _apply_policy_change(self, pc: PolicyChange) -> None:
        """Mid-scenario per-user tier policy change. Writes through the
        userPolicies admin endpoint, preserving whatever's already there
        via explicit merge semantics on the gateway side."""
        if not pc.user:
            return
        real_uid = UID_MAP.get(pc.user, pc.user)
        tier = pc.tier or "interactive"
        entry: dict[str, Any] = {}
        if pc.set_permission:
            entry["permission"] = pc.set_permission
        if pc.set_rate_limit is not None:
            entry["rateLimit"] = pc.set_rate_limit
        body = {"defaults": {tier: entry}}

        try:
            r = requests.put(
                f"{self._acp_base_url}/{self._tenant_slug}/admin/userPolicies/{real_uid}",
                headers=self._admin_headers(),
                json=body,
                timeout=10,
            )
            if not r.ok:
                self._errors.append(
                    f"apply_policy_change {real_uid} {r.status_code}: {r.text[:200]}",
                )
        except requests.RequestException as e:
            self._errors.append(f"apply_policy_change {real_uid} failed: {e!r}")
        # Firestore read replicas can lag write-ack; give them time.
        time.sleep(1.5)

    # ── Per-tool call — impersonation via body param ───────────────────

    def _id_token_for(self, uid: str) -> Optional[str]:
        # Empty/missing uid → unauthenticated call. Return None so the
        # parent's _do_direct bails with allowed=False,
        # reason="unauthenticated" without reaching the gateway. Matches
        # the reference `acp` runner's behavior for anonymous scenarios.
        if not uid:
            return None
        # Otherwise: the "token" is the API key for every impersonated
        # call; the target uid rides in the body as impersonate_uid.
        return self._api_key

    def execute_action(self, action: Action) -> Optional[ToolOutcome]:  # type: ignore[override]
        # Skip scenarios in declined_categories — we can't test them
        # honestly, and executing their actions pollutes audit (e.g.
        # two-tenant scenarios collapsed onto one tenant). Declined
        # scenarios return empty audit, which vacuously passes
        # no_cross_tenant_leak and fails any positive assertion with
        # an honest "couldn't run" signal.
        if getattr(self, "_skip_scenario", False):
            return None
        return super().execute_action(action)

    def _post_govern(
        self,
        path: str,
        token: str,
        tool: str,
        tool_input: Any,
        agent_tier: str,
        agent_name: Optional[str],
        tool_output: Optional[str] = None,
        agent_chain: Optional[list[str]] = None,
    ) -> Optional[dict]:
        # Shadow the parent implementation but inject impersonate_uid
        # derived from the currently-in-flight user (tracked below).
        impersonate_uid = getattr(self, "_current_impersonate_uid", None)

        # Rewrite `/govern/tool-use` → `/admin/bench/tool-use` so the
        # gateway's impersonation middleware fires. The parent builds the
        # path assuming JWT-authenticated user calls; we're an API key
        # that needs to impersonate, so we live on the admin/bench mount.
        if impersonate_uid:
            path = path.replace("/govern/", "/admin/bench/")

        body: dict[str, Any] = {
            "tool_name": tool,
            "tool_input": tool_input,
            "hook_event_name": "PreToolUse" if path.endswith("tool-use") else "PostToolUse",
            "session_id": f"agb-{os.urandom(4).hex()}",
            "agent_tier": agent_tier,
        }
        if agent_name:
            body["agent_name"] = agent_name
        if tool_output is not None:
            body["tool_output"] = tool_output
        if agent_chain:
            body["agent_chain"] = agent_chain
        if impersonate_uid:
            body["impersonate_uid"] = impersonate_uid

        # 5xx simulation: return None, callers treat as failure.
        if time.time() < self._simulated_5xx_until:
            self._gateway_reachable = False
            return None
        try:
            resp = requests.post(
                f"{self._acp_base_url}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-GS-Client": "agentgovbench-acp-api/0.1.0",
                },
                json=body,
                timeout=10,
            )
        except requests.RequestException as e:
            self._errors.append(f"{path}: {e!r}")
            self._gateway_reachable = False
            return None
        self._gateway_reachable = True
        if resp.status_code == 401:
            return {"decision": "deny", "reason": "unauthenticated"}
        if resp.status_code == 429:
            return {"decision": "deny", "reason": "rate_limited"}
        if not resp.ok:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _do_direct(self, a: DirectToolCall) -> ToolOutcome:
        # Thread the scenario uid → benchmark uid mapping through the
        # impersonation field so the parent method calls _post_govern
        # with the right body.
        self._current_impersonate_uid = UID_MAP.get(a.as_user, a.as_user) if a.as_user else None
        try:
            return super()._do_direct(a)
        finally:
            self._current_impersonate_uid = None

    # ── Audit read — via /admin/audit ──────────────────────────────────

    def audit_log(self) -> list[AuditEntry]:
        if not self._scenario_start_ts:
            return []
        # Declined scenarios didn't execute — no audit to look up, and
        # reading the tenant-wide log window could surface unrelated
        # entries that look like leaks on no_cross_tenant_leak checks.
        if getattr(self, "_skip_scenario", False):
            return []
        # Gateway writes audit async; sleep briefly so GET /admin/audit
        # reflects the scenario's recent calls.
        time.sleep(1.5)
        # Gateway writes `ts` as JS-style ISO with `Z` suffix (toISOString).
        # Python's .isoformat() emits `+00:00` instead, which sorts lower than
        # `Z` lexicographically (`+` 0x2B < `Z` 0x5A). Firestore's >= compare
        # on a `+00:00`-formatted `since` can skip legitimate Z-suffix rows
        # with ties in microsecond precision. Normalize to Z-format so string
        # comparison is consistent with the gateway's writes.
        since_iso = (
            datetime.fromtimestamp(self._scenario_start_ts - 1, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )
        try:
            r = requests.get(
                f"{self._acp_base_url}/{self._tenant_slug}/admin/audit",
                params={"since": since_iso, "limit": 500},
                headers=self._admin_headers(),
                timeout=15,
            )
        except requests.RequestException as e:
            self._errors.append(f"audit GET failed: {e!r}")
            return list(self._local_audit_entries)
        if not r.ok:
            self._errors.append(f"audit GET {r.status_code}: {r.text[:200]}")
            return list(self._local_audit_entries)

        try:
            payload = r.json() or {}
        except ValueError:
            return list(self._local_audit_entries)

        raw_entries = payload.get("entries", []) or []
        entries: list[AuditEntry] = list(self._local_audit_entries)
        scenario_tenant = REVERSE_TENANT_SLUG_MAP.get(self._tenant_slug, self._tenant_slug)

        for data in raw_entries:
            tool = data.get("tool") or ""
            if not tool:
                continue
            real_uid = data.get("sub")
            uid = REVERSE_UID_MAP.get(real_uid, real_uid)
            raw_email = data.get("userEmail")
            # Translate real benchmark-user email back to the scenario's
            # generic @example.com form so email-based assertions match
            # (same translation the reference `acp` runner does).
            email = EMAIL_MAP_REAL_TO_SCENARIO.get(raw_email, raw_email)
            entries.append(AuditEntry(
                timestamp=str(data.get("ts", "")),
                tenant=scenario_tenant,
                actor_uid=uid,
                actor_email=email,
                tool=tool,
                decision=data.get("decision", "allow"),
                reason=data.get("decisionReason"),
                trace_id=data.get("requestId") or data.get("sessionId"),
                delegation_chain=data.get("agentChain") or [],
                extra={
                    "tier": data.get("agentTier"),
                    "agent_name": data.get("agentName"),
                    "hookEvent": data.get("hookEvent"),
                    "client": data.get("client"),
                },
            ))
        return entries

    # ── Setup — reset stale state between scenarios ────────────────────

    def setup(self, scenario) -> None:  # type: ignore[override]
        """Clear stale policies, then write the scenario's.

        Matches the reset semantics of `runners.acp` (which deletes user
        policy docs between scenarios via Firestore). The admin REST
        endpoints use `{ merge: true }` semantics, so a bare PUT won't
        clear fields the previous scenario wrote — we DELETE first,
        then PUT, to guarantee the gateway sees a clean policy
        corresponding to this scenario.
        """
        from benchmark.runner import StatefulRunner
        from runners.acp import Runner as _AcpRunner
        StatefulRunner.setup(self, scenario)

        self._simulated_unreachable_until = 0.0
        self._simulated_5xx_until = 0.0
        self._chain_by_agent = {}
        self._delegated_scopes_by_agent = {}
        self._local_audit_entries = []
        self._tenants_used = {self._tenant_slug}

        # Rate-limit scenario cool-down. The gateway's per-tier rate
        # limiter keeps a sliding window of timestamps per
        # `${tenantId}:${sub}:${tier}` in-memory on each Cloud Run
        # instance. Two rate-heavy scenarios back-to-back pollute each
        # other's buckets — the second scenario starts with a partially
        # full bucket and its expected deny count doesn't land. Parent
        # `AcpRunner.setup()` has this logic but this override skipped
        # parent; restore it here.
        scenario_is_rate_heavy = (
            scenario.category == "rate_limit_cascade"
            and any(
                hasattr(a, "calls_per_worker")
                and getattr(a, "calls_per_worker", 0) * getattr(a, "worker_count", 1) >= 30
                for a in scenario.actions
            )
        )
        if _AcpRunner._prev_scenario_was_rate_heavy and scenario_is_rate_heavy:
            time.sleep(62)  # 60s sliding window + 2s guard band
        _AcpRunner._prev_scenario_was_rate_heavy = scenario_is_rate_heavy

        self._scenario_start_ts = time.time()

        # Only ONE scenario needs early-skip: cross_tenant_isolation.02
        # collapses two tenants onto the runner's single tenant and
        # registers false cross-tenant leaks. Other declined scenarios
        # still run — their assertions happen to pass on a single
        # tenant or just get counted as documented declinations in the
        # scorecard. Skipping them breaks positive assertions that
        # require outcomes.
        self._skip_scenario = scenario.id == "cross_tenant_isolation.02_audit_log_separation"

        self._reset_stale_policies()

        all_policies = self._scenario_policy_to_acp(scenario)
        for _tid, policy in all_policies.items():
            self._write_policy(self._tenant_slug, policy)

        time.sleep(0.3)  # let writes settle

    def _reset_stale_policies(self) -> None:
        """DELETE workspace policy and per-user policy docs for every
        benchmark user so prior-scenario state can't leak. Mirrors the
        cleanup loop at the top of acp.Runner.setup().
        """
        base = f"{self._acp_base_url}/{self._tenant_slug}"
        # Workspace — clear any tools/defaults the prior scenario wrote.
        try:
            requests.delete(
                f"{base}/admin/workspacePolicy",
                headers=self._admin_headers(),
                timeout=10,
            )
        except requests.RequestException:
            pass

        # Per-user — the set of uids the benchmark ever impersonates.
        # Kept in sync with UID_MAP in runners/acp.py.
        for uid in ("agb-alice", "agb-bob", "agb-carol", "agb-dan", "agb-eve"):
            try:
                requests.delete(
                    f"{base}/admin/userPolicies/{uid}",
                    headers=self._admin_headers(),
                    timeout=10,
                )
            except requests.RequestException:
                pass
