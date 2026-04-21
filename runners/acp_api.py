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
from runners.acp import Runner as AcpRunner, UID_MAP, REVERSE_UID_MAP, TENANT_SLUG_MAP, REVERSE_TENANT_SLUG_MAP


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
        # With the impersonation endpoint, the "token" is literally the
        # API key for every call. The target uid rides in the body as
        # impersonate_uid.
        return self._api_key

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
        # Gateway writes audit async; sleep briefly so GET /admin/audit
        # reflects the scenario's recent calls.
        time.sleep(1.5)
        since_iso = datetime.fromtimestamp(
            self._scenario_start_ts - 1, tz=timezone.utc,
        ).isoformat()
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
            # Reuse the parent's email reverse-map by importing it lazily
            # if the user wants — for now pass through since the scenario
            # assertions generally match on uid, not email.
            entries.append(AuditEntry(
                timestamp=str(data.get("ts", "")),
                tenant=scenario_tenant,
                actor_uid=uid,
                actor_email=raw_email,
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
        StatefulRunner.setup(self, scenario)

        self._simulated_unreachable_until = 0.0
        self._simulated_5xx_until = 0.0
        self._chain_by_agent = {}
        self._delegated_scopes_by_agent = {}
        self._local_audit_entries = []
        self._scenario_start_ts = time.time()
        self._tenants_used = {self._tenant_slug}

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
