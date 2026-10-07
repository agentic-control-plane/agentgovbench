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
  - The key's tenant must be the one the benchmark runs against, and
    that tenant must be a DEDICATED BENCHMARK WORKSPACE (Firestore
    `isBenchmarkTenant: true`). ACP never lets an API key write the
    policy of a real workspace — an agent's key must not be able to
    loosen its own rules — but a `bench.impersonate` key on a benchmark
    workspace MAY write workspacePolicy/userPolicies there. That is what
    the console's "Create benchmark workspace" flow mints (24h key), and
    what the pinned CI reference tenant `agentgovbench` uses. On any
    other workspace the gateway answers `403 human-auth-required` and
    the run aborts in preflight.

Environment:
  ACP_API_KEY       (required) Bearer token used for all endpoints,
                    including scenario policy setup on a benchmark
                    workspace.
  ACP_ADMIN_TOKEN   (optional) Firebase ID token of a signed-in human
                    admin of the tenant. When set it is used INSTEAD of
                    the API key for policy setup — the only way to drive
                    a tenant that is not a benchmark workspace.
  ACP_BASE_URL      (optional) Default https://api.agenticcontrolplane.com
  ACP_TENANT_SLUG   (optional) Target tenant slug. Defaults to `agentgovbench`.
  AGB_POLICY_SETUP  (optional) `firestore` routes scenario policy FIXTURE
                    writes through Firestore Admin under the operator's
                    own credentials instead of /admin/workspacePolicy.
                    Needed on deployments where policy mutation over the
                    API is human-only (ACP's default since 57f814c): the
                    gateway correctly refuses an agent-held key writing
                    policy, benchmark or not. Fixture setup is the test
                    operator's job, so it runs as the operator. Decisions
                    and audit reads still flow through the public API.
                    HARD GUARD: refuses any tenant whose Firestore doc
                    lacks `isBenchmarkTenant: true` — this mode can never
                    touch a real tenant's policies.
  AGB_PROJECT       (optional) GCP project for firestore mode.
                    Default `gatewaystack-connect`.

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

        # Human session token used ONLY for scenario setup (policy writes).
        # Separate from the agent credential by design — see _setup_headers.
        self._admin_token = os.environ.get("ACP_ADMIN_TOKEN", "").strip()
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

        # ── Second tenant (optional) ───────────────────────────────────
        # gsk_ keys are tenant-scoped, so acting in two tenants needs two
        # keys. With ACP_API_KEY_B set, the cross_tenant_isolation
        # scenarios become measurable instead of collapsing both scenario
        # tenants onto one real one — which previously made a correctly
        # isolated deployment look like it leaked, because tenant-a's deny
        # WAS tenant-b's deny.
        self._api_key_b = os.environ.get("ACP_API_KEY_B", "").strip()
        self._tenant_slug_b = os.environ.get(
            "ACP_TENANT_SLUG_B", TENANT_SLUG_MAP.get("tenant-b", "agentgovbench-b"),
        )
        self._multi = bool(self._api_key_b)
        # Slug currently being routed to; set by _resolve_tenant so the
        # per-call credential lookup knows which tenant it is acting in.
        self._current_slug = self._tenant_slug
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

        # Optional Firestore fixture-setup mode. See module docstring.
        self._fs_setup = os.environ.get("AGB_POLICY_SETUP", "") == "firestore"
        self._db = None
        self._tenant_id: Optional[str] = None
        if self._fs_setup:
            self._init_firestore_setup()

    def _init_firestore_setup(self) -> None:
        import firebase_admin
        from firebase_admin import firestore as fb_firestore

        project = os.environ.get("AGB_PROJECT", "gatewaystack-connect")
        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app(options={"projectId": project})
        self._db = fb_firestore.client()

        # Resolve every tenant this run will install fixtures into. The
        # isBenchmarkTenant guard is applied to each one independently —
        # adding a second tenant must not widen what this mode can touch.
        self._tenant_ids_by_slug: dict[str, str] = {}
        wanted = [self._tenant_slug] + ([self._tenant_slug_b] if self._multi else [])
        for slug in wanted:
            slug_snap = self._db.document(f"tenantSlugs/{slug}").get()
            if not slug_snap.exists:
                raise RuntimeError(
                    f"AGB_POLICY_SETUP=firestore: tenant slug "
                    f"{slug!r} not found in project {project!r}. "
                    "Run setup/bootstrap_tenant.py first."
                )
            tid = slug_snap.to_dict()["tenantId"]

            tdoc = self._db.document(f"tenants/{tid}").get().to_dict() or {}
            if tdoc.get("isBenchmarkTenant") is not True:
                raise RuntimeError(
                    f"AGB_POLICY_SETUP=firestore REFUSED: tenant "
                    f"{slug!r} ({tid}) is not marked isBenchmarkTenant in "
                    "Firestore. This mode writes policy fixtures and must "
                    "never touch a real tenant."
                )
            self._tenant_ids_by_slug[slug] = tid

        self._tenant_id = self._tenant_ids_by_slug[self._tenant_slug]

    def preflight(self) -> None:
        """Prove the key can install policy and read audit before scoring.

        Both are prerequisites, not niceties. Without the policy write the
        scenarios run against whatever policy the tenant already had;
        without the audit read, identity_propagation, delegation_provenance
        and audit_completeness cannot be measured at all. A run missing
        both still completes and still prints a scorecard — that is exactly
        how a meaningless 13/48 gets produced and mistaken for a result.
        """
        # Refuse to run against a tenant that is not a declared benchmark
        # tenant. This runner REWRITES workspace governance policy once per
        # scenario — 48 times per run. Against the dedicated benchmark
        # tenant that is the point; against a real one it is a destructive
        # action taken by a program, which is exactly what governance
        # policy is supposed to be protected from. Pointing ACP_TENANT_SLUG
        # at a live tenant should be impossible by accident, so it now
        # requires an explicit, differently-named opt-out.
        allowed = {"agentgovbench", "agentgovbench-b"}
        extra = os.environ.get("AGB_ALLOW_TENANT", "").strip()
        if extra:
            allowed.add(extra)
        if self._tenant_slug not in allowed:
            raise RuntimeError(
                f"refusing to run against tenant '{self._tenant_slug}'.\n"
                f"This runner overwrites workspace governance policy on every "
                f"scenario (48 writes per run). Known benchmark tenants: "
                f"{sorted(allowed)}.\n"
                "If you genuinely intend to run against this tenant and it is "
                "disposable, set AGB_ALLOW_TENANT=<slug>. Do not do this on a "
                "tenant whose policy you rely on."
            )

        base = f"{self._acp_base_url}/{self._tenant_slug}"
        problems: list[str] = []
        for label, url, need in [
            ("audit read", f"{base}/admin/audit?limit=1", "admin.audit.read"),
            ("policy read", f"{base}/admin/workspacePolicy", "admin policy scope"),
        ]:
            try:
                r = requests.get(url, headers=self._admin_headers(), timeout=20)
            except Exception as e:
                problems.append(f"{label}: request failed ({e!r})")
                continue
            if r.status_code >= 400:
                problems.append(
                    f"{label} -> HTTP {r.status_code} {r.text[:120]} "
                    f"(needs {need})"
                )

        # Probe the WRITE path, not just reads. Every scenario's setup
        # depends on installing policy; a runner that can read but not
        # write still completes all 48 scenarios against whatever policy
        # the tenant happened to be carrying, and prints a scorecard.
        # Checking readability was the original mistake here — it passed,
        # and the run that followed was worthless.
        #
        # Skipped in firestore fixture-setup mode: there the operator's own
        # credentials install policy directly and the API is never asked to
        # mutate it, which is the whole point of that mode. Probing the HTTP
        # write here would fail on a deployment that is correctly configured.
        if self._fs_setup:
            if self._db is None or not self._tenant_id:
                problems.append(
                    "AGB_POLICY_SETUP=firestore but Firestore setup did not "
                    "initialise — check ADC and the tenant slug."
                )
        else:
            problems.extend(self._probe_policy_write(base))
        if problems:
            raise RuntimeError(
                f"{self._setup_credential_label()} cannot drive this deployment:\n  - "
                + "\n  - ".join(problems)
                + f"\n\nTenant slug: {self._tenant_slug}. Base: {self._acp_base_url}."
                "\n\nReading the status code:"
                "\n  401 'Invalid or revoked API key' -> the key itself is not "
                "valid. Benchmark keys expire after 24h, so an expired key "
                "is the most common cause. Mint a new one."
                "\n  403 'api key lacks <scope>'       -> the key is valid but "
                "under-scoped. Re-mint with bench.impersonate and "
                "admin.audit.read (or *)."
                f"\n  403 '{self.HUMAN_AUTH_REQUIRED}'         -> the key is valid "
                "but this tenant is not a benchmark workspace. ACP forbids API "
                "keys from writing governance policy on a real workspace on "
                "principle: an agent must never be able to loosen the rules it "
                "runs under. The exception is a dedicated benchmark workspace "
                "(isBenchmarkTenant), where a bench.impersonate key may write "
                "policy. See README -> 'Reproducing the ACP score' -> "
                "'Create benchmark workspace'. Alternatively export "
                "ACP_ADMIN_TOKEN (a signed-in admin's Firebase ID token) and "
                "the runner will use it for policy setup only."
                "\n\nThe dashboard issues empty-scope keys by default; the "
                "'Create benchmark workspace' card mints a correctly scoped "
                "key on a workspace that accepts these writes."
            )

    # Gateway's refusal marker for "no API key may do this; a human must".
    HUMAN_AUTH_REQUIRED = "human-auth-required"

    def _setup_credential_label(self) -> str:
        return "ACP_ADMIN_TOKEN" if self._admin_token else "ACP_API_KEY"

    def _probe_policy_write(self, base: str) -> list[str]:
        """Really try a policy write with the setup credential.

        Reads the workspace policy with the agent key, then PUTs the same
        document back with the setup credential (the API key, or
        ACP_ADMIN_TOKEN when set). On success the write is a no-op — the
        tenant ends the probe carrying exactly what it started with — but
        the gateway has had to actually authorise a policy mutation, which
        is the operation every scenario's setup depends on.

        A `403 human-auth-required` with no ACP_ADMIN_TOKEN means the key is
        fine but the tenant is not a benchmark workspace; that is reported
        with the way out rather than as a generic write failure.
        """
        try:
            cur = requests.get(
                f"{base}/admin/workspacePolicy",
                headers=self._admin_headers(), timeout=20,
            )
        except Exception as e:
            return [f"policy write probe: read-back failed ({e!r})"]
        if not cur.ok:
            # Already reported by the read loop above.
            return []
        try:
            doc = cur.json() or {}
        except ValueError:
            doc = {}
        echo = {
            "mode": doc.get("mode", "enforce"),
            "defaults": doc.get("defaults", {}),
            "tools": doc.get("tools", {}),
        }
        try:
            w = requests.put(
                f"{base}/admin/workspacePolicy",
                headers=self._setup_headers(), json=echo, timeout=20,
            )
        except Exception as e:
            return [f"policy write probe failed: {e!r}"]
        if w.status_code < 400:
            return []
        if (w.status_code == 403 and self.HUMAN_AUTH_REQUIRED in (w.text or "")
                and not self._admin_token):
            return [
                f"policy WRITE with ACP_API_KEY -> HTTP 403 "
                f"{self.HUMAN_AUTH_REQUIRED}: tenant '{self._tenant_slug}' is "
                "not a benchmark workspace, so no API key may write its "
                "policy. Create one (ACP console -> API Keys -> Create "
                "benchmark workspace) and point ACP_TENANT_SLUG at it, or "
                "export ACP_ADMIN_TOKEN for a signed-in admin of this tenant."
            ]
        return [
            f"policy WRITE with {self._setup_credential_label()} -> "
            f"HTTP {w.status_code} {w.text[:200]}"
        ]

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
                "per_user_policy_enforcement.03_user_override_beats_workspace": (
                    "Tests user-scope tool-specific overrides; harness + "
                    "runner need types/YAML/write-path support for "
                    "user.tools. Gateway side is ready."
                ),
                # Cross-tenant isolation is only declinable while this
                # runner has one tenant to act in. With ACP_API_KEY_B set,
                # both scenario tenants map to real, separate tenants and
                # these become genuine measurements — so the declination
                # disappears rather than quietly excusing a category the
                # runner could now test.
                **({} if self._multi else {
                    "cross_tenant_isolation.02_audit_log_separation": (
                        "Requires two tenants to test separation; this run "
                        "has one API key, so both scenario tenants collapse "
                        "onto it. Set ACP_API_KEY_B to measure this."
                    ),
                    "cross_tenant_isolation.03_user_scope_does_not_leak": (
                        "Single-tenant run — set ACP_API_KEY_B to measure."
                    ),
                    "cross_tenant_isolation.05_admin_cannot_cross": (
                        "Single-tenant run — set ACP_API_KEY_B to measure."
                    ),
                }),
            },
        )

    # ── HTTP helpers ───────────────────────────────────────────────────

    def _admin_headers(self, slug: Optional[str] = None) -> dict[str, str]:
        """Agent credential for `slug` (default: the primary tenant).

        This is the principal whose behaviour is being measured. On a real
        workspace it cannot write policy; on a benchmark workspace the
        gateway lets it — see _setup_headers.
        """
        return {
            "Authorization": f"Bearer {self._key_for(slug or self._tenant_slug)}",
            "Content-Type": "application/json",
            "X-GS-Client": "agentgovbench-acp-api/0.1.0",
        }

    def _setup_headers(self, slug: Optional[str] = None) -> dict[str, str]:
        """Credential that installs scenario policy in `slug` (default: primary).

        ACP refuses policy writes from API-key principals on a real
        workspace on principle: an agent must never be able to loosen the
        rules it runs under. The benchmark honours that rather than routing
        around it — it runs in a DEDICATED BENCHMARK WORKSPACE
        (`isBenchmarkTenant`), the one place the gateway lets a
        `bench.impersonate` key write workspacePolicy/userPolicies. That is
        what the console's "Create benchmark workspace" card mints (24h
        key) and what the pinned CI reference tenant `agentgovbench` uses.
        So by default setup uses the same API key as the exercise, and the
        gateway's own tenant check — not this runner — decides whether the
        write is legitimate. Preflight proves it before anything is scored;
        a refusal mid-run aborts via _require_policy_write.

        ACP_ADMIN_TOKEN, when set, is used instead: a short-lived Firebase
        ID token belonging to a human admin/owner of the tenant. It is the
        only way to drive a tenant that is not a benchmark workspace.
        Obtain it from a browser session you actually signed in to; do not
        mint one with a service account, which would re-introduce exactly
        the bypass the benchmark-workspace split exists to avoid.
        """
        if self._admin_token:
            return {
                "Authorization": f"Bearer {self._admin_token}",
                "Content-Type": "application/json",
                "X-GS-Client": "agentgovbench-acp-api-setup/0.1.0",
            }
        return {
            "Authorization": f"Bearer {self._key_for(slug or self._tenant_slug)}",
            "Content-Type": "application/json",
            "X-GS-Client": "agentgovbench-acp-api-setup/0.1.0",
        }

    def _slug_for(self, scenario_tenant_id: Optional[str]) -> str:
        """Map a scenario tenant id onto a real tenant slug.

        Single-key mode collapses everything onto the primary tenant, which
        is why cross-tenant isolation is declined there: with both scenario
        tenants pointing at one real tenant, a correct deployment is
        indistinguishable from a leaking one.
        """
        if self._multi and TENANT_SLUG_MAP.get(scenario_tenant_id or "") == \
                TENANT_SLUG_MAP.get("tenant-b"):
            return self._tenant_slug_b
        return self._tenant_slug

    def _key_for(self, slug: str) -> str:
        """The agent credential valid in `slug`. Keys are tenant-scoped."""
        if self._multi and slug == self._tenant_slug_b:
            return self._api_key_b
        return self._api_key

    def _resolve_tenant(self, scenario_tenant_id: Optional[str]) -> tuple[str, str]:
        slug = self._slug_for(scenario_tenant_id)
        # Remember the target so _id_token_for picks the matching key —
        # the parent computes the tenant before it asks for a token.
        self._current_slug = slug
        return slug, slug

    # ── Policy write — via /admin endpoints ────────────────────────────

    def _require_policy_write(self, r: "requests.Response", what: str) -> None:
        """Abort the run when ACP refuses a policy fixture write.

        Every scenario writes its own policy before it runs. If that write
        is refused (401/403), the scenario runs against whatever policy the
        workspace already had — usually allow-everything — and scores as a
        plausible but meaningless result. That happened silently to real
        users; refuse to produce a scorecard instead."""
        if r.status_code not in (401, 403):
            return
        from benchmark.runner import RunAborted
        if r.status_code == 401:
            raise RunAborted(
                f"ACP did not accept ACP_API_KEY ({what} → 401: {r.text[:200]}).\n\n"
                "The key is wrong, revoked, or expired — benchmark keys last "
                "24 hours. Get a fresh one from the ACP console (API keys → "
                "Create benchmark workspace) and check ACP_TENANT_SLUG matches "
                "the workspace it shows."
            )
        raise RunAborted(
            f"ACP refused the benchmark's policy fixture write "
            f"({what} → {r.status_code}: {r.text[:200]}).\n\n"
            "Each scenario writes its own policy before running; without "
            "that write the results would not mean anything. ACP only "
            "accepts these writes from a 24-hour AgentGovBench key on a "
            "dedicated benchmark workspace — create one from the ACP "
            "console (API keys → Create benchmark workspace) and use the "
            "command it shows. See README → 'Reproducing the ACP score'. "
            "To drive a tenant that is not a benchmark workspace, export "
            "ACP_ADMIN_TOKEN (a signed-in admin's Firebase ID token) for "
            "policy setup."
        )

    def _write_policy(self, tenant_id: str, policy: dict[str, Any]) -> None:
        """Write workspace + per-user policies via the admin REST API,
        or via Firestore Admin when AGB_POLICY_SETUP=firestore."""
        # `tenant_id` is the slug _scenario_policy_to_acp keyed the doc by
        # (see _resolve_tenant, which returns slug for both halves). Route
        # each tenant's policy to that tenant rather than collapsing every
        # scenario tenant onto the primary.
        slug = tenant_id or self._tenant_slug
        if self._fs_setup:
            self._write_policy_firestore(policy, slug)
            return
        base = f"{self._acp_base_url}/{slug}"

        # Workspace policy (defaults + tools).
        workspace_body = {
            "mode": policy.get("mode", "enforce"),
            "defaults": policy.get("defaults", {}),
            "tools": policy.get("tools", {}),
        }
        try:
            r = requests.put(
                f"{base}/admin/workspacePolicy",
                headers=self._setup_headers(slug),
                json=workspace_body,
                timeout=10,
            )
            self._require_policy_write(r, "workspacePolicy PUT")
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
                    headers=self._setup_headers(slug),
                    json=body,
                    timeout=10,
                )
                self._require_policy_write(r, f"userPolicies PUT {uid}")
                if not r.ok:
                    self._errors.append(
                        f"userPolicies PUT {uid} {r.status_code}: {r.text[:200]}",
                    )
            except requests.RequestException as e:
                self._errors.append(f"userPolicies PUT {uid} failed: {e!r}")

    def _write_policy_firestore(self, policy: dict[str, Any],
                                slug: Optional[str] = None) -> None:
        """Mirror of runners/acp._write_policy: full-doc set() replaces
        whatever the prior scenario wrote, so no DELETE pass is needed
        for the workspace doc."""
        from firebase_admin import firestore as fb_firestore

        tid = self._tenant_ids_by_slug.get(slug or self._tenant_slug, self._tenant_id)
        ref = self._db.document(f"tenants/{tid}/policies/governance")
        ref.set({
            **policy,
            "updatedBy": "agentgovbench-runner",
            "updatedAt": fb_firestore.SERVER_TIMESTAMP,
        })
        for uid, user_doc in (policy.get("users", {}) or {}).items():
            uref = self._db.document(f"tenants/{tid}/userPolicies/{uid}")
            uref.set({
                **user_doc,
                "updatedBy": "agentgovbench-runner",
                "updatedAt": fb_firestore.SERVER_TIMESTAMP,
            })

    def _apply_policy_change(self, pc: PolicyChange) -> None:
        """Mid-scenario per-user tier policy change. Writes through the
        userPolicies admin endpoint, preserving whatever's already there
        via explicit merge semantics on the gateway side."""
        tier = pc.tier or "interactive"
        entry: dict[str, Any] = {}
        if pc.set_permission:
            entry["permission"] = pc.set_permission
        if pc.set_rate_limit is not None:
            entry["rateLimit"] = pc.set_rate_limit

        # A change with no user is WORKSPACE-scoped — either a tool
        # override or a tier default. This previously hit `if not pc.user:
        # return` and vanished, so scenarios that revoke a tool at the
        # workspace level (cross_tenant_isolation.01) never installed their
        # deny, and ACP was recorded as allowing a call it was never told
        # to block.
        if not pc.user:
            target_slug = self._slug_for(pc.tenant)
            if not self._fs_setup:
                # Same read-merge-write as the Firestore branch below, over
                # the admin REST API with the setup credential (benchmark
                # workspace key, or ACP_ADMIN_TOKEN). A refusal aborts the
                # run rather than leaving the deny uninstalled.
                base = f"{self._acp_base_url}/{target_slug}"
                try:
                    cur = requests.get(
                        f"{base}/admin/workspacePolicy",
                        headers=self._admin_headers(target_slug), timeout=10,
                    )
                    doc = (cur.json() or {}) if cur.ok else {}
                except (requests.RequestException, ValueError) as e:
                    self._errors.append(
                        f"workspace policy_change read failed: {e!r}")
                    return
                body = {
                    "mode": doc.get("mode", "enforce"),
                    "defaults": dict(doc.get("defaults", {})),
                    "tools": dict(doc.get("tools", {})),
                }
                if pc.tool:
                    per_tool = dict(body["tools"].get(pc.tool, {}))
                    merged = dict(per_tool.get(tier, {}))
                    merged.update(entry)
                    per_tool[tier] = merged
                    body["tools"][pc.tool] = per_tool
                else:
                    merged = dict(body["defaults"].get(tier, {}))
                    merged.update(entry)
                    body["defaults"][tier] = merged
                try:
                    r = requests.put(
                        f"{base}/admin/workspacePolicy",
                        headers=self._setup_headers(target_slug), json=body,
                        timeout=10,
                    )
                    self._require_policy_write(r, "workspace policy_change PUT")
                    if not r.ok:
                        self._errors.append(
                            f"workspace policy_change PUT {r.status_code}: "
                            f"{r.text[:200]}")
                except requests.RequestException as e:
                    self._errors.append(
                        f"workspace policy_change PUT failed: {e!r}")
                time.sleep(1.5)  # replica lag before the next governance call
                return
            from firebase_admin import firestore as fb_firestore

            tid = self._tenant_ids_by_slug.get(target_slug, self._tenant_id)
            ref = self._db.document(f"tenants/{tid}/policies/governance")
            doc = ref.get().to_dict() or {}
            if pc.tool:
                tools = dict(doc.get("tools", {}))
                per_tool = dict(tools.get(pc.tool, {}))
                merged = dict(per_tool.get(tier, {}))
                merged.update(entry)
                per_tool[tier] = merged
                tools[pc.tool] = per_tool
                doc["tools"] = tools
            else:
                defaults = dict(doc.get("defaults", {}))
                merged = dict(defaults.get(tier, {}))
                merged.update(entry)
                defaults[tier] = merged
                doc["defaults"] = defaults
            doc["updatedBy"] = "agentgovbench-runner"
            doc["updatedAt"] = fb_firestore.SERVER_TIMESTAMP
            ref.set(doc)
            time.sleep(1.5)  # replica lag before the next governance call
            return

        real_uid = UID_MAP.get(pc.user, pc.user)
        body = {"defaults": {tier: entry}}

        if self._fs_setup:
            from firebase_admin import firestore as fb_firestore

            # Route to the tenant the change names, not always the primary.
            _tid = self._tenant_ids_by_slug.get(
                self._slug_for(pc.tenant), self._tenant_id)
            ref = self._db.document(
                f"tenants/{_tid}/userPolicies/{real_uid}")
            doc = ref.get().to_dict() or {}
            defaults = dict(doc.get("defaults", {}))
            merged = dict(defaults.get(tier, {}))
            merged.update(entry)
            defaults[tier] = merged
            doc["defaults"] = defaults
            doc["updatedBy"] = "agentgovbench-runner"
            doc["updatedAt"] = fb_firestore.SERVER_TIMESTAMP
            ref.set(doc)
            time.sleep(1.5)  # replica lag before next governance call
            return

        try:
            r = requests.put(
                f"{self._acp_base_url}/{self._tenant_slug}/admin/userPolicies/{real_uid}",
                headers=self._setup_headers(),
                json=body,
                timeout=10,
            )
            self._require_policy_write(r, f"apply_policy_change {real_uid}")
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
        #
        # The credential follows the PRINCIPAL's home tenant, not the
        # target path. Picking the key by target (_current_slug) meant a
        # forgery scenario — alice-at-a naming tenant-b — was sent with
        # tenant B's own valid key, so the gateway saw a legitimate
        # tenant-B request and rightly allowed it. The forged-credential
        # premise never reached the product, and the resulting
        # "expected deny, got allow" on cross_tenant_isolation.03/.05
        # read as an isolation leak when it was this line deciding.
        # With the home-tenant key on the target tenant's path, the
        # gateway's own credential/tenant binding makes the call:
        # 401 → deny("unauthenticated"), which is the product refusing.
        home = getattr(self, "_home_slug_by_user", {}).get(uid, self._current_slug)
        return self._key_for(home)

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
        # Read every tenant this run can act in. With one key that is just
        # the primary; with a second key the cross-tenant scenarios need
        # both logs, and each entry must be attributed to the tenant it
        # actually came from — reading only the primary would report
        # tenant-b's activity as missing rather than as isolated.
        slugs = [self._tenant_slug] + ([self._tenant_slug_b] if self._multi else [])
        entries: list[AuditEntry] = list(self._local_audit_entries)
        collected: list[tuple[str, dict]] = []

        for slug in slugs:
            try:
                r = requests.get(
                    f"{self._acp_base_url}/{slug}/admin/audit",
                    params={"since": since_iso, "limit": 500},
                    headers=self._admin_headers(slug),
                    timeout=15,
                )
            except requests.RequestException as e:
                self._errors.append(f"audit GET failed ({slug}): {e!r}")
                continue
            if not r.ok:
                self._errors.append(
                    f"audit GET {slug} {r.status_code}: {r.text[:200]}")
                continue
            try:
                payload = r.json() or {}
            except ValueError:
                self._errors.append(f"audit GET {slug}: non-JSON response")
                continue
            for _d in (payload.get("entries", []) or []):
                collected.append((slug, _d))

        for _slug, data in collected:
            scenario_tenant = REVERSE_TENANT_SLUG_MAP.get(_slug, _slug)
            # Prefer the name the agent actually invoked. ACP's `tool` is
            # the classified name it matched policy on, which since the
            # harness aliases (#676) can differ from the call — `read_file`
            # is filed as `Read`. Scenarios assert on what they invoked, so
            # assert against that; fall back to the classified name on
            # deployments predating the toolRaw field.
            tool = data.get("toolRaw") or data.get("tool") or ""
            if not tool:
                continue
            real_uid = data.get("sub")
            uid = REVERSE_UID_MAP.get(real_uid, real_uid)
            raw_email = data.get("userEmail")
            # Translate real benchmark-user email back to the scenario's
            # generic @example.com form so email-based assertions match
            # (same translation the reference `acp` runner does).
            email = EMAIL_MAP_REAL_TO_SCENARIO.get(raw_email, raw_email)
            # Match reference acp.Runner's defensive parsing:
            # unknown decision values default to "deny" (safe), and
            # non-list agentChain falls back to empty list.
            decision_raw = data.get("decision", "allow")
            decision = decision_raw if decision_raw in ("allow", "deny", "flag", "redact") else "deny"
            chain_from_audit = data.get("agentChain")
            chain = chain_from_audit if isinstance(chain_from_audit, list) else []
            entries.append(AuditEntry(
                timestamp=str(data.get("ts", "")),
                tenant=scenario_tenant,
                actor_uid=uid,
                actor_email=email,
                tool=tool,
                decision=decision,
                reason=data.get("decisionReason"),
                trace_id=data.get("requestId") or data.get("sessionId"),
                agent_tier=data.get("agentTier"),
                delegation_chain=chain,
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

        # Which tenant each scenario user belongs to, per the fixture.
        # _id_token_for uses this to send the principal's own credential
        # even when the call names a different tenant — the forged-tenant
        # scenarios are meaningless if the runner swaps in the target
        # tenant's valid key.
        self._home_slug_by_user = {
            u.uid: self._slug_for(t.id)
            for t in scenario.setup.tenants
            for u in t.users
        }
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
        # A rate-heavy scenario poisons the bucket for EVERY later
        # scenario that reuses the same user+tier inside the 60s window,
        # not just the next rate-heavy one — observed as a benign
        # subagent read in scope_inheritance.06 denied with "63/60 per
        # minute" ~18s after rate_limit_cascade finished. Cool down
        # before ANY scenario that starts inside the window.
        last_heavy_end = getattr(_AcpRunner, "_last_rate_heavy_end_ts", 0.0)
        elapsed = time.time() - last_heavy_end
        if last_heavy_end and elapsed < 62:
            time.sleep(62 - elapsed)  # 60s sliding window + 2s guard band
        if scenario_is_rate_heavy:
            # Recorded at teardown-time semantics: the window matters from
            # the scenario's LAST call, which we approximate as now +
            # scenario runtime; setting at setup start is conservative
            # only if we also update after the run — done in
            # collect_outcome below via the same attribute.
            _AcpRunner._pending_rate_heavy = True
        else:
            _AcpRunner._pending_rate_heavy = False
        _AcpRunner._prev_scenario_was_rate_heavy = scenario_is_rate_heavy

        self._scenario_start_ts = time.time()

        # Only ONE scenario needs early-skip: cross_tenant_isolation.02
        # collapses two tenants onto the runner's single tenant and
        # registers false cross-tenant leaks. Other declined scenarios
        # still run — their assertions happen to pass on a single
        # tenant or just get counted as documented declinations in the
        # scorecard. Skipping them breaks positive assertions that
        # require outcomes.
        # Only skipped while single-tenant, where both scenario tenants
        # collapse onto one real one and every entry reads as a leak. With
        # a second key the scenario is genuinely measurable, so run it.
        self._skip_scenario = (
            not self._multi
            and scenario.id == "cross_tenant_isolation.02_audit_log_separation"
        )

        self.setup_policy_only(scenario)

    def setup_policy_only(self, scenario) -> None:
        """Install this scenario's policy fixtures and nothing else.

        Split out so other subjects can reuse the tested translation and
        write path instead of carrying a second copy. runners/pi_acp.py
        composes this runner purely for fixtures while driving the calls
        through a real pi session.
        """
        self._scenario = scenario
        self._reset_stale_policies()

        all_policies = self._scenario_policy_to_acp(scenario)
        for _slug, policy in all_policies.items():
            self._write_policy(_slug, policy)

        time.sleep(0.3)  # let writes settle

    def collect_outcome(self):  # type: ignore[override]
        outcome = super().collect_outcome()
        # Stamp the end of a rate-heavy scenario so the next setup() can
        # hold until the gateway's 60s sliding window has actually
        # drained relative to the LAST call, not the scenario's start.
        from runners.acp import Runner as _AcpRunner
        if getattr(_AcpRunner, "_pending_rate_heavy", False):
            _AcpRunner._last_rate_heavy_end_ts = time.time()
        return outcome

    def _reset_stale_policies(self) -> None:
        """DELETE workspace policy and per-user policy docs for every
        benchmark user so prior-scenario state can't leak. Mirrors the
        cleanup loop at the top of acp.Runner.setup().
        """
        if self._fs_setup:
            # Workspace doc needs no delete — the coming set() replaces it
            # wholesale. User docs must go: a leftover per-user deny turns
            # a clean allow into a mystery deny.
            # Every tenant in play, not just the primary — a stale per-user
            # deny left in tenant B turns a clean allow into a mystery deny
            # exactly as it would in tenant A.
            for _tid in (self._tenant_ids_by_slug.values()
                         if getattr(self, "_tenant_ids_by_slug", None)
                         else [self._tenant_id]):
              for uid in ("agb-alice", "agb-bob", "agb-carol", "agb-dan", "agb-eve"):
                try:
                    self._db.document(
                        f"tenants/{_tid}/userPolicies/{uid}").delete()
                except Exception:
                    pass
            return

        base = f"{self._acp_base_url}/{self._tenant_slug}"
        # Workspace — clear any tools/defaults the prior scenario wrote.
        # The first refusal surfaces here, before any scenario runs.
        try:
            r = requests.delete(
                f"{base}/admin/workspacePolicy",
                headers=self._admin_headers(),
                timeout=10,
            )
        except requests.RequestException:
            r = None
        if r is not None:
            self._require_policy_write(r, "workspacePolicy DELETE")

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
