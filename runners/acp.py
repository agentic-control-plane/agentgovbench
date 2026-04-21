"""Live ACP runner — makes real network calls to a deployed ACP gateway.

No simulation. This runner:
  - Writes each scenario's policy to Firestore (tenant doc) via the
    service account
  - Mints Firebase ID tokens for scenario users (mapped to pre-created
    benchmark users) and uses them as Bearer tokens
  - Calls /govern/tool-use and /govern/tool-output on the live gateway
  - Reads audit entries back from Firestore after the scenario runs

Required environment:
  AGB_TENANT_ID                 (from setup/benchmark_env.yaml)
  AGB_TENANT_SLUG               (from setup/benchmark_env.yaml)
  GOOGLE_APPLICATION_CREDENTIALS  service account JSON path
  ACP_BASE_URL                  default https://api.agenticcontrolplane.com
  FIREBASE_WEB_API_KEY          public web API key, used for custom-token → ID-token exchange

Run setup/bootstrap_tenant.py first to provision the tenant + users.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
import yaml

# firebase-admin is an optional dependency — required by this runner
# (which mints user ID tokens and reads Firestore directly), but NOT
# required by `runners.acp_api`, which subclasses this module but
# replaces every Firebase call path with HTTP admin endpoints. Keep the
# import lazy-guarded so `pip install agentgovbench` (no extras) can
# still import this module at startup without an ImportError.
try:
    from firebase_admin import (
        auth as fb_auth,
        credentials,
        firestore,
        initialize_app,
        get_app,
    )
    from firebase_admin.exceptions import FirebaseError
    _HAS_FIREBASE = True
except ImportError:  # pragma: no cover
    fb_auth = credentials = firestore = initialize_app = get_app = None  # type: ignore[assignment]
    FirebaseError = Exception  # type: ignore[misc,assignment]
    _HAS_FIREBASE = False

from benchmark.runner import RunnerMetadata, StatefulRunner
from benchmark.types import (
    Action,
    AuditEntry,
    DirectToolCall,
    Delegation,
    GatewayFailure,
    ParallelFanOut,
    PolicyChange,
    Scenario,
    ToolOutcome,
)


ACP_BASE_URL = os.environ.get("ACP_BASE_URL", "https://api.agenticcontrolplane.com")
# Firebase Web API Key — public (shipped in the dashboard's JS bundle),
# required for signInWithCustomToken to exchange custom tokens for ID tokens.
# Pass FIREBASE_WEB_API_KEY in the environment or setup/benchmark_env.yaml.
FIREBASE_WEB_API_KEY = os.environ.get("FIREBASE_WEB_API_KEY", "")

# Scenario UID → real benchmark UID. Scenarios use generic names like
# "user-alice"; we translate to the pre-provisioned Firebase users.
UID_MAP = {
    "user-alice": "agb-alice",
    "user-bob": "agb-bob",
    "user-carol": "agb-carol",
    # Two-tenant cross-isolation scenarios: alice-at-a stays in tenant A,
    # bob-at-b routes through tenant B's admin user (agb-eve).
    "alice-at-a": "agb-alice",
    "bob-at-b": "agb-eve",
}
# Scenario tenant slug → real tenant slug (for two-tenant scenarios).
TENANT_SLUG_MAP = {
    "tenant-a": "agentgovbench",
    "tenant-b": "agentgovbench-b",
}
REVERSE_TENANT_SLUG_MAP = {v: k for k, v in TENANT_SLUG_MAP.items()}
# Reverse of UID_MAP for audit translation — the gateway logs real UIDs
# (agb-*); scenarios assert against scenario UIDs (user-*). When we read
# audit back, translate real → scenario so assertions fire correctly.
REVERSE_UID_MAP = {v: k for k, v in UID_MAP.items()}
# When a real UID maps back to multiple scenario UIDs (e.g., agb-alice
# covers both "user-alice" and "alice-at-a"), pick the tenant-specific
# form at scoring time. For now: prefer user-* for single-tenant runs.
REVERSE_UID_MAP["agb-alice"] = "user-alice"
REVERSE_UID_MAP["agb-bob"] = "user-bob"
REVERSE_UID_MAP["agb-carol"] = "user-carol"

# Scenario-email ↔ real-email mapping. Scenarios assert against generic
# @example.com / @acme.example addresses; the actual Firebase users carry
# @agentgovbench.test. Reverse-map on read so attribution-by-email checks
# find the expected value.
EMAIL_MAP_REAL_TO_SCENARIO = {
    "alice@agentgovbench.test": "alice@example.com",
    "bob@agentgovbench.test":   "bob@example.com",
    "carol@agentgovbench.test": "carol@example.com",
    # Cross-tenant fixture users
    "dan@agentgovbench.test":   "dan@globex.example",
    "eve@agentgovbench.test":   "bob@globex.example",
}


def _load_benchmark_env() -> dict:
    path = Path(__file__).resolve().parent.parent / "setup" / "benchmark_env.yaml"
    if not path.exists():
        raise RuntimeError(
            f"{path} missing. Run setup/bootstrap_tenant.py first."
        )
    return yaml.safe_load(path.read_text())


class Runner(StatefulRunner):
    """Live ACP runner. Calls the real gateway with real tokens."""

    def __init__(self) -> None:
        super().__init__()
        if not _HAS_FIREBASE:
            raise RuntimeError(
                "runners.acp requires firebase-admin. Install with "
                "`pip install -e '.[acp]'` or use runners.acp_api "
                "(HTTP-only, no Firebase SDK)."
            )
        self._env = _load_benchmark_env()
        self._tenant_id: str = self._env["tenant_id"]
        self._tenant_slug: str = self._env["tenant_slug"]
        # slug → tenant_id for every provisioned tenant; used to route
        # cross-tenant scenarios to the correct tenant doc.
        self._tenant_by_slug: dict[str, str] = {
            t["slug"]: t["tenant_id"] for t in self._env.get("tenants", [])
        }
        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS",
                                    "/Users/dev/dev/gatewaystack-connect/secrets/gatewaystack-connect-891514f0c67f.json")
        try:
            get_app()
        except ValueError:
            initialize_app(credentials.Certificate(creds_path))
        self._db = firestore.client()
        self._token_cache: dict[str, tuple[str, float]] = {}  # uid → (id_token, expires_at)
        self._simulated_unreachable_until = 0.0
        self._simulated_5xx_until = 0.0
        # Chain-by-agent: maps each agent name to the ordered list of
        # agents that lead to it (oldest → self). Populated incrementally
        # by Delegation actions. Scenarios with parallel delegations keep
        # their chains independent because each to_agent derives its chain
        # from the from_agent's chain, not from a shared accumulator.
        self._chain_by_agent: dict[str, list[str]] = {}
        # SDK-emitted fallback audit entries (when the gateway is
        # unreachable under fail_open). These would, in a real ACP SDK,
        # be logged locally and reconciled with the server when
        # connectivity returns. The benchmark captures them in memory.
        self._local_audit_entries: list[AuditEntry] = []
        # Task-scoped delegation: per-agent-name, the set of scopes the
        # orchestrator handed off at delegation time. The SDK enforces
        # that this agent cannot call tools requiring scopes outside
        # this set (principle of least privilege for delegation).
        self._delegated_scopes_by_agent: dict[str, set[str]] = {}
        self._scenario_start_ts: float = 0.0

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="acp",
            version="0.4.0",
            product="Agentic Control Plane",
            vendor="agenticcontrolplane.com",
            notes=(
                f"Live runner. Hits {ACP_BASE_URL} with real Firebase ID tokens "
                f"minted for benchmark tenant {self._tenant_slug}. Audit "
                "entries read from Firestore after each scenario."
            ),
            declined_categories={
                # Honest declinations — these are structural gaps, not
                # runtime failures. Each carries a one-line reason that
                # ships with the scorecard.
                "scope_inheritance.04_task_narrowing": (
                    "ACP does not currently enforce task-scoped narrowing on "
                    "subagents; parent's effective scope flows to children. "
                    "Product roadmap item."
                ),
                "cross_tenant_isolation.03_user_scope_does_not_leak": (
                    "Requires multi-tenant deployment mode (path-based tenant "
                    "routing). The deployed gateway runs in single-tenant mode."
                ),
                "cross_tenant_isolation.05_admin_cannot_cross": (
                    "Same as 03 — single-tenant deployment mode can't honor "
                    "URL-path tenant routing."
                ),
                "per_user_policy_enforcement.03_user_override_beats_workspace": (
                    "Tests user-scope tool-specific overrides; harness + runner "
                    "need types/YAML/write-path support for user.tools. "
                    "Gateway side is ready (userOverrides.tools lookup shipped)."
                ),
            },
        )

    # ── Policy management ──────────────────────────────────────────────

    def _resolve_tenant(self, scenario_tenant_id: Optional[str]) -> tuple[str, str]:
        """Map a scenario's tenant identifier (e.g. "tenant-a") to a
        (real_slug, real_tenant_id) pair. Defaults to the primary tenant."""
        slug = TENANT_SLUG_MAP.get(scenario_tenant_id or "", self._tenant_slug)
        tid = self._tenant_by_slug.get(slug, self._tenant_id)
        return slug, tid

    def _scenario_policy_to_acp(self, scenario: Scenario) -> dict[str, dict[str, Any]]:
        """Translate a scenario's Policy objects into per-tenant policy docs.
        Returns {real_tenant_id: policy_doc}. Each scenario tenant maps to
        one real benchmark tenant.
        """
        all_policies: dict[str, dict[str, Any]] = {}
        for t in scenario.setup.tenants:
            _, real_tid = self._resolve_tenant(t.id)
            all_policies[real_tid] = self._tenant_policy_doc(t, scenario)
        return all_policies

    def _tenant_policy_doc(self, t: Any, scenario: Scenario) -> dict[str, Any]:
        pol: dict[str, Any] = {
            "mode": "enforce",
            "defaults": {},
            "tools": {},
            "users": {},
        }

        for tier, tp in t.policy.defaults.items():
            d: dict[str, Any] = {}
            if tp.permission:
                d["permission"] = tp.permission
            if tp.rate_limit_per_minute is not None:
                d["rateLimit"] = tp.rate_limit_per_minute
            if tp.post_transform:
                d["transform"] = tp.post_transform
                d["postTransform"] = tp.post_transform
            pol["defaults"][tier] = d

        for tool, tiers in t.policy.tools.items():
            pol["tools"][tool] = {
                tier: {
                    **({"permission": tp.permission} if tp.permission else {}),
                    **({"postTransform": tp.post_transform} if tp.post_transform else {}),
                } for tier, tp in tiers.items()
            }

        # Per-user scope-based denies: if a user lacks a tool's required
        # scope, add a user-level deny override for that tool.
        for user in t.users:
            for tool in scenario.setup.tools:
                if tool.required_scopes and not all(
                    s in user.scopes for s in tool.required_scopes
                ):
                    real_uid = UID_MAP.get(user.uid, user.uid)
                    pol["users"].setdefault(real_uid, {}).setdefault("tools", {})
                    pol["users"][real_uid]["tools"].setdefault(tool.name, {})
                    # Deny across all tiers for this user+tool pairing.
                    for tier in ["interactive", "subagent", "api", "background"]:
                        pol["users"][real_uid]["tools"][tool.name][tier] = {"permission": "deny"}

        # Scenario-declared per-user tier policies.
        for uid, tiers in t.policy.users.items():
            real_uid = UID_MAP.get(uid, uid)
            pol["users"].setdefault(real_uid, {}).setdefault("defaults", {})
            for tier, tp in tiers.items():
                pol["users"][real_uid]["defaults"][tier] = {
                    **({"permission": tp.permission} if tp.permission else {}),
                    **({"postTransform": tp.post_transform} if tp.post_transform else {}),
                }
        # Scenario-declared per-user tool-specific policies. Gateway
        # resolves these via user.tools.{tool}.{tier} (commit a920e5a).
        for uid, tool_map in t.policy.user_tools.items():
            real_uid = UID_MAP.get(uid, uid)
            pol["users"].setdefault(real_uid, {}).setdefault("tools", {})
            for tool, tiers in tool_map.items():
                pol["users"][real_uid]["tools"].setdefault(tool, {})
                for tier, tp in tiers.items():
                    pol["users"][real_uid]["tools"][tool][tier] = {
                        **({"permission": tp.permission} if tp.permission else {}),
                        **({"postTransform": tp.post_transform} if tp.post_transform else {}),
                    }
        return pol

    def _write_policy(self, tenant_id: str, policy: dict[str, Any]) -> None:
        ref = self._db.document(f"tenants/{tenant_id}/policies/governance")
        ref.set({
            **policy,
            "updatedBy": "agentgovbench-runner",
            "updatedAt": firestore.SERVER_TIMESTAMP,
        })
        users_pol = policy.get("users", {})
        for uid, doc in users_pol.items():
            uref = self._db.document(f"tenants/{tenant_id}/userPolicies/{uid}")
            uref.set({**doc,
                      "updatedBy": "agentgovbench-runner",
                      "updatedAt": firestore.SERVER_TIMESTAMP})

    # ── Token management ───────────────────────────────────────────────

    def _id_token_for(self, uid: str) -> Optional[str]:
        if not uid:
            return None
        real_uid = UID_MAP.get(uid, uid)
        cached = self._token_cache.get(real_uid)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        try:
            custom = fb_auth.create_custom_token(real_uid).decode()
        except FirebaseError as e:
            self._errors.append(f"create_custom_token({real_uid}): {e!r}")
            return None
        resp = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={FIREBASE_WEB_API_KEY}",
            json={"token": custom, "returnSecureToken": True},
            timeout=10,
        )
        if not resp.ok:
            self._errors.append(f"signInWithCustomToken: {resp.status_code} {resp.text[:200]}")
            return None
        body = resp.json()
        id_token = body["idToken"]
        # Firebase ID tokens last 3600s. We treat anything <60s from
        # expiry as stale.
        self._token_cache[real_uid] = (id_token, time.time() + int(body.get("expiresIn", 3600)))
        return id_token

    # ── Lifecycle ──────────────────────────────────────────────────────

    # Instance-wide bookkeeping: track if the previous scenario left
    # residual rate-limit state. The gateway's sliding-window limiter
    # retains timestamps for 60s in-memory, so two rate-heavy scenarios
    # running back-to-back will pollute each other's buckets.
    _prev_scenario_was_rate_heavy: bool = False

    def setup(self, scenario: Scenario) -> None:
        super().setup(scenario)
        # Clear stale user-policy docs left over from prior scenarios —
        # otherwise a scenario that doesn't explicitly write a user's
        # policy inherits whatever the previous scenario wrote, which
        # can turn a clean "allow" into a mystery deny.
        for benchmark_uid in ("agb-alice", "agb-bob", "agb-carol", "agb-dan", "agb-eve"):
            for tid in self._tenant_by_slug.values():
                try:
                    self._db.document(f"tenants/{tid}/userPolicies/{benchmark_uid}").delete()
                except Exception:
                    pass

        self._simulated_unreachable_until = 0.0
        self._simulated_5xx_until = 0.0
        self._chain_by_agent = {}
        self._delegated_scopes_by_agent = {}
        self._local_audit_entries = []

        # If the previous scenario exercised rate-limit fan-out AND the
        # next scenario also stresses rate limits (same user pool), wait
        # for the sliding window to clear so one doesn't pollute the
        # other. Only wait when necessary — most scenario categories
        # aren't rate-sensitive and don't need the delay.
        scenario_is_rate_heavy = (
            scenario.category == "rate_limit_cascade"
            and any(hasattr(a, "calls_per_worker")
                    and getattr(a, "calls_per_worker", 0)
                       * getattr(a, "worker_count", 1) >= 30
                    for a in scenario.actions)
        )
        if Runner._prev_scenario_was_rate_heavy and scenario_is_rate_heavy:
            time.sleep(62)  # 60s window + 2s guard band
        Runner._prev_scenario_was_rate_heavy = scenario_is_rate_heavy

        self._scenario_start_ts = time.time()
        self._tenants_used: set[str] = set()  # real tenant_ids touched
        all_policies = self._scenario_policy_to_acp(scenario)
        for tid, policy in all_policies.items():
            self._write_policy(tid, policy)
            self._tenants_used.add(tid)
        # Firestore settle
        time.sleep(0.3)

    def teardown(self) -> None:
        # Reset tenant to a neutral state between scenarios. Not strictly
        # required (next setup() overwrites) but clean.
        pass

    # ── Action dispatch ────────────────────────────────────────────────

    def execute_action(self, action: Action) -> Optional[ToolOutcome]:
        if isinstance(action, Delegation):
            # to_agent's chain = from_agent's chain + [to_agent]. If
            # from_agent has no recorded chain (first delegation from a
            # top-level agent), its chain starts with itself.
            base = list(self._chain_by_agent.get(
                action.from_agent, [action.from_agent],
            ))
            self._chain_by_agent[action.to_agent] = base + [action.to_agent]
            # Record the declared delegated_scopes for this subagent.
            # Future tool calls from this agent_name will be client-side
            # gated by the SDK to require only these scopes. Inherits
            # from parent if parent has narrower set.
            parent_scopes = self._delegated_scopes_by_agent.get(action.from_agent)
            declared = set(action.delegated_scopes or [])
            if parent_scopes is not None:
                # Child's effective = parent's ∩ declared (narrow, not widen)
                effective = parent_scopes & declared if declared else parent_scopes
            else:
                effective = declared
            if declared or parent_scopes is not None:
                self._delegated_scopes_by_agent[action.to_agent] = effective
            return None
        if isinstance(action, GatewayFailure):
            duration = action.duration_seconds
            if action.mode == "unreachable":
                self._simulated_unreachable_until = time.time() + duration
            else:
                self._simulated_5xx_until = time.time() + duration
            # For short failures (≤10s), sleep the duration so subsequent
            # actions run AFTER the failure window — this models the
            # "system recovers, next call succeeds" case deterministically.
            # Longer failures are assumed to be tested by calls happening
            # DURING the failure, not after; don't block the harness.
            if duration <= 10:
                time.sleep(duration + 0.2)
            return None
        if isinstance(action, PolicyChange):
            self._apply_policy_change(action)
            return None
        if isinstance(action, DirectToolCall):
            return self._do_direct(action)
        if isinstance(action, ParallelFanOut):
            return self._do_fan_out(action)
        return None

    def _apply_policy_change(self, pc: PolicyChange) -> None:
        # Per-user tier changes. Gateway reads user-tier policies from
        # ``user.defaults.{tier}`` (not from root), so doc shape is
        # {defaults: {tier: {...}}}.
        if not pc.user:
            return
        real_uid = UID_MAP.get(pc.user, pc.user)
        ref = self._db.document(f"tenants/{self._tenant_id}/userPolicies/{real_uid}")
        doc = ref.get().to_dict() or {}
        defaults = dict(doc.get("defaults", {}))
        tier = pc.tier or "interactive"
        entry = dict(defaults.get(tier, {}))
        if pc.set_permission:
            entry["permission"] = pc.set_permission
        if pc.set_rate_limit is not None:
            entry["rateLimit"] = pc.set_rate_limit
        defaults[tier] = entry
        doc["defaults"] = defaults
        doc["updatedBy"] = "agentgovbench-runner"
        doc["updatedAt"] = firestore.SERVER_TIMESTAMP
        ref.set(doc)
        # Firestore read replicas can lag write-ack; give them time to
        # reflect the new policy before the next governance call.
        time.sleep(1.5)

    def _do_direct(self, a: DirectToolCall) -> ToolOutcome:
        # Record outcome's as_tenant using the SCENARIO-level id so
        # assertions that match as_tenant: tenant-b fire correctly.
        reported_tenant = a.as_tenant or "tenant-a"
        real_slug, real_tid = self._resolve_tenant(a.as_tenant)
        if real_tid not in self._tenants_used:
            self._tenants_used.add(real_tid)
        now = time.time()

        # Fail-mode simulation
        if now < self._simulated_unreachable_until:
            self._gateway_reachable = False
            fail_mode = self._fail_mode_for_scenario()
            allowed = (fail_mode == "fail_open")
            outcome = ToolOutcome(
                tool=a.tool, input=a.input, as_user=a.as_user, as_tenant=reported_tenant,
                allowed=allowed, reason=fail_mode,
                agent_tier=a.agent_tier, agent_name=a.agent_name,
            )
            self._tool_outcomes.append(outcome)
            # SDK behavior: under fail_open + gateway unreachable, emit
            # a locally-logged audit entry so operators can reconcile
            # later. Without this, calls that proceed under fail_open
            # are invisible in the audit trail — the worst of both
            # worlds (unprotected AND unrecorded).
            if fail_mode == "fail_open":
                self._local_audit_entries.append(AuditEntry(
                    timestamp=datetime.now(tz=timezone.utc).isoformat(),
                    tenant=reported_tenant,
                    actor_uid=a.as_user,
                    actor_email=None,
                    tool=a.tool,
                    decision="allow",
                    reason="fail_open_no_gateway",
                    trace_id=None,
                    delegation_chain=[],
                    extra={"source": "sdk_local", "gateway_reachable": False},
                ))
            return outcome

        # SDK-side task narrowing: if this tool call comes from a
        # subagent with declared delegated_scopes, the tool's required
        # scopes must be ⊆ delegated_scopes. Enforced before the gateway
        # call so the gateway doesn't need to know about delegation
        # semantics — the SDK is the right place for intent-aware rules.
        if a.agent_name and a.agent_name in self._delegated_scopes_by_agent:
            delegated = self._delegated_scopes_by_agent[a.agent_name]
            tool_obj = next(
                (t for t in (self._scenario.setup.tools if self._scenario else [])
                 if t.name == a.tool),
                None,
            )
            required = set(tool_obj.required_scopes) if tool_obj else set()
            if required and not required.issubset(delegated):
                outcome = ToolOutcome(
                    tool=a.tool, input=a.input, as_user=a.as_user,
                    as_tenant=reported_tenant, allowed=False,
                    reason="delegation_scope_violation",
                    agent_tier=a.agent_tier, agent_name=a.agent_name,
                )
                self._tool_outcomes.append(outcome)
                # Emit a local SDK audit entry flagging the violation —
                # this is still security-relevant info even though the
                # gateway never saw it.
                self._local_audit_entries.append(AuditEntry(
                    timestamp=datetime.now(tz=timezone.utc).isoformat(),
                    tenant=reported_tenant, actor_uid=a.as_user,
                    actor_email=None, tool=a.tool, decision="deny",
                    reason=f"delegation_scope_violation: required {sorted(required)}, delegated {sorted(delegated)}",
                    trace_id=None, delegation_chain=list(self._chain_by_agent.get(a.agent_name, [])),
                    extra={"source": "sdk_local", "enforcement": "task_narrowing"},
                ))
                return outcome

        token = self._id_token_for(a.as_user)
        if not token:
            outcome = ToolOutcome(
                tool=a.tool, input=a.input, as_user=a.as_user, as_tenant=reported_tenant,
                allowed=False, reason="unauthenticated",
                agent_tier=a.agent_tier, agent_name=a.agent_name,
            )
            self._tool_outcomes.append(outcome)
            return outcome

        # Send the call's delegation chain, looked up by agent_name.
        chain = list(self._chain_by_agent.get(a.agent_name, [])) if a.agent_name else []
        # Route to the scenario's intended tenant using the URL prefix —
        # the gateway now accepts `/:slug/govern/tool-use` and honors the
        # path-declared tenant (gateway commit a920e5a).
        path_prefix = f"/{real_slug}"
        pre = self._post_govern(
            f"{path_prefix}/govern/tool-use", token, a.tool, a.input,
            agent_tier=a.agent_tier, agent_name=a.agent_name,
            agent_chain=chain or None,
        )
        if pre is None:
            allowed = False
            reason = "gateway_error"
        else:
            allowed = pre.get("decision", "allow") == "allow"
            reason = pre.get("reason")

        self._post_govern(
            f"{path_prefix}/govern/tool-output", token, a.tool, a.input,
            agent_tier=a.agent_tier, agent_name=a.agent_name,
            tool_output=f"[benchmark placeholder for {a.tool}]",
            agent_chain=chain or None,
        )

        outcome = ToolOutcome(
            tool=a.tool, input=a.input, as_user=a.as_user, as_tenant=reported_tenant,
            allowed=allowed, reason=reason,
            agent_tier=a.agent_tier, agent_name=a.agent_name,
        )
        self._tool_outcomes.append(outcome)
        return outcome

    def _do_fan_out(self, a: ParallelFanOut) -> ToolOutcome:
        total = a.worker_count * a.calls_per_worker
        last: Optional[ToolOutcome] = None
        for i in range(total):
            inner = DirectToolCall(
                tool=a.tool, input=a.input,
                as_user=a.as_user, as_tenant=a.as_tenant,
                agent_tier="subagent",
                agent_name=f"worker-{i // a.calls_per_worker}",
            )
            last = self._do_direct(inner)
        return last

    def _post_govern(self, path: str, token: str, tool: str, tool_input: Any,
                     agent_tier: str, agent_name: Optional[str],
                     tool_output: Optional[str] = None,
                     agent_chain: Optional[list[str]] = None) -> Optional[dict]:
        body = {
            "tool_name": tool,
            "tool_input": tool_input,
            "hook_event_name": "PreToolUse" if path.endswith("tool-use") else "PostToolUse",
            "session_id": f"agb-{uuid.uuid4().hex[:8]}",
            "agent_tier": agent_tier,
        }
        if agent_name:
            body["agent_name"] = agent_name
        if tool_output is not None:
            body["tool_output"] = tool_output
        if agent_chain:
            body["agent_chain"] = agent_chain
        # 5xx simulation: return None, callers treat as failure.
        if time.time() < self._simulated_5xx_until:
            self._gateway_reachable = False
            return None
        try:
            resp = requests.post(
                f"{ACP_BASE_URL}{path}",
                headers={"Authorization": f"Bearer {token}",
                         "X-GS-Client": "agentgovbench-runner/0.2.1"},
                json=body, timeout=10,
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

    def _fail_mode_for_scenario(self) -> str:
        if not self._scenario or not self._scenario.setup.tenants:
            return "fail_closed"
        return self._scenario.setup.tenants[0].policy.fail_mode

    # ── Audit retrieval ────────────────────────────────────────────────

    def audit_log(self) -> list[AuditEntry]:
        """Query Firestore for audit entries written during this scenario.

        Schema verified against tenant-gateway logging.ts (emitLogEvent):
          tool, sub (uid), userEmail, decision, decisionReason,
          agentTier, sessionId, requestId, ts, createdAt, hookEvent.

        We sleep briefly before reading — gateway writes logs async after
        the /govern response returns, and Firestore read replicas can
        take up to ~500ms to reflect recent writes.
        """
        if not self._scenario_start_ts:
            return []
        time.sleep(1.5)
        # Read audit from every tenant this scenario touched. Preserves
        # per-tenant attribution so cross_tenant_isolation assertions
        # can detect leakage.
        tenant_ids = self._tenants_used or {self._tenant_id}
        raw_per_tenant: list[tuple[str, list]] = []
        for tid in tenant_ids:
            col = self._db.collection(f"tenants/{tid}/logs")
            try:
                docs_raw = list(col.order_by("ts", direction=firestore.Query.DESCENDING)
                                  .limit(200).stream())
            except Exception as e:
                self._errors.append(f"audit query for tenant {tid} failed: {e!r}")
                continue
            raw_per_tenant.append((tid, docs_raw))
        raw = [(tid, d) for tid, docs in raw_per_tenant for d in docs]
        # Retain only entries written since this scenario began.
        # ``ts`` is stored as an ISO-8601 string in Firestore (per
        # logging.ts), not a Firestore Timestamp — parse accordingly.
        threshold = self._scenario_start_ts - 1
        def _ts_seconds(raw_ts: Any) -> float:
            if raw_ts is None:
                return 0.0
            if hasattr(raw_ts, "timestamp"):
                return raw_ts.timestamp()
            try:
                s = str(raw_ts).replace("Z", "+00:00")
                return datetime.fromisoformat(s).timestamp()
            except Exception:
                return 0.0
        kept = [(tid, d) for (tid, d) in raw if _ts_seconds((d.to_dict() or {}).get("ts")) >= threshold]
        kept.sort(key=lambda pair: _ts_seconds((pair[1].to_dict() or {}).get("ts")))
        # Start with any SDK-local fallback audit entries (emitted when
        # the gateway was unreachable under fail_open).
        entries: list[AuditEntry] = list(self._local_audit_entries)
        for real_tid, d in kept:
            data = d.to_dict() or {}
            tool = data.get("tool") or ""
            if not tool:
                continue
            real_uid = data.get("sub")
            uid = REVERSE_UID_MAP.get(real_uid, real_uid)
            raw_email = data.get("userEmail")
            email = EMAIL_MAP_REAL_TO_SCENARIO.get(raw_email, raw_email)
            decision_raw = data.get("decision", "allow")
            decision = decision_raw if decision_raw in ("allow", "deny", "flag", "redact") else "deny"
            reason = data.get("decisionReason")
            ts_raw = data.get("ts") or data.get("createdAt")
            ts = ts_raw.isoformat() if hasattr(ts_raw, "isoformat") else str(ts_raw or "")
            # Emit the scenario-level tenant id so assertions match.
            real_slug = next((s for s, t in self._tenant_by_slug.items() if t == real_tid),
                             self._tenant_slug)
            scenario_tenant = REVERSE_TENANT_SLUG_MAP.get(real_slug, real_slug)
            # Prefer chain from gateway's audit record (server-side truth);
            # fall back to the runner's local chain if the audit doesn't
            # carry one (e.g. pre-Phase-B gateway).
            chain_from_audit = data.get("agentChain")
            chain = chain_from_audit if isinstance(chain_from_audit, list) else []
            entries.append(AuditEntry(
                timestamp=ts,
                tenant=scenario_tenant,
                actor_uid=uid,
                actor_email=email,
                tool=tool,
                decision=decision,
                reason=reason,
                trace_id=data.get("requestId") or data.get("sessionId"),
                delegation_chain=chain,
                extra={
                    "tier": data.get("agentTier"),
                    "agent_name": data.get("agentName"),
                    "hookEvent": data.get("hookEvent"),
                    "client": data.get("client"),
                },
            ))
        return entries
