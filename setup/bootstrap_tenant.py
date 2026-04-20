"""One-time bootstrap: create a dedicated benchmark tenant in Firestore,
create synthetic Firebase users, attach them as tenant members.

Idempotent — rerunning reuses existing records.

Usage:
    python setup/bootstrap_tenant.py

Requires:
    GOOGLE_APPLICATION_CREDENTIALS env or hardcoded path to a Firebase
    service account JSON with admin rights on the gatewaystack-connect
    project.

Emits:
    setup/benchmark_env.yaml — tenant_id, user_uids, tenant_slug. The
    live ACP runner consumes this at startup.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Defaults — override via env if pointing at a different GCP project.
DEFAULT_CREDS = "/Users/dev/dev/gatewaystack-connect/secrets/gatewaystack-connect-891514f0c67f.json"
DEFAULT_PROJECT = "gatewaystack-connect"
TENANTS = [
    {
        "slug": "agentgovbench",
        "name": "AgentGovBench tenant A",
        "users": [
            {"uid": "agb-alice",  "email": "alice@agentgovbench.test",  "role": "member"},
            {"uid": "agb-bob",    "email": "bob@agentgovbench.test",    "role": "admin"},
            {"uid": "agb-carol",  "email": "carol@agentgovbench.test",  "role": "viewer"},
        ],
    },
    {
        "slug": "agentgovbench-b",
        "name": "AgentGovBench tenant B",
        "users": [
            # alice-at-a is a member of tenant A only; we keep agb-alice
            # as her single identity and attach her to both tenants only
            # when a scenario needs her in both. By default, tenant-b's
            # users are distinct real Firebase users.
            {"uid": "agb-dan",  "email": "dan@agentgovbench.test",  "role": "member"},
            {"uid": "agb-eve",  "email": "eve@agentgovbench.test",  "role": "admin"},
        ],
    },
]


def main() -> int:
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = DEFAULT_CREDS
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", DEFAULT_PROJECT)

    try:
        import firebase_admin
        from firebase_admin import auth as fb_auth, credentials, firestore
    except ImportError:
        print("firebase-admin not installed. `pip install firebase-admin`", file=sys.stderr)
        return 1

    cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    try:
        firebase_admin.get_app()
    except ValueError:
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    provisioned: list[dict] = []
    for tcfg in TENANTS:
        slug = tcfg["slug"]
        # 1. Users
        for u in tcfg["users"]:
            try:
                fb_auth.get_user(u["uid"])
            except fb_auth.UserNotFoundError:
                fb_auth.create_user(uid=u["uid"], email=u["email"],
                                    email_verified=True,
                                    display_name=u["email"].split("@")[0])
                print(f"created user: {u['uid']}")

        # 2. Tenant
        slug_ref = db.document(f"tenantSlugs/{slug}")
        slug_snap = slug_ref.get()
        if slug_snap.exists:
            tenant_id = slug_snap.to_dict()["tenantId"]
            print(f"tenant exists: slug={slug} id={tenant_id}")
        else:
            _, tenant_ref = db.collection("tenants").add({
                "name": tcfg["name"], "slug": slug,
                "createdAt": firestore.SERVER_TIMESTAMP,
                "plan": "benchmark", "isBenchmarkTenant": True,
            })
            tenant_id = tenant_ref.id
            slug_ref.set({"tenantId": tenant_id,
                          "createdAt": firestore.SERVER_TIMESTAMP})
            print(f"created tenant: slug={slug} id={tenant_id}")

        # 3. Memberships
        for u in tcfg["users"]:
            mref = db.document(f"users/{u['uid']}/memberships/{tenant_id}")
            if not mref.get().exists:
                mref.set({
                    "tenantId": tenant_id, "slug": slug, "role": u["role"],
                    "addedAt": firestore.SERVER_TIMESTAMP,
                })
                print(f"  attached {u['uid']} as {u['role']} in {slug}")
            pref = db.document(f"users/{u['uid']}")
            if not pref.get().exists:
                pref.set({"email": u["email"], "displayName": u["email"]})

        # 4. Baseline policy
        policy_ref = db.document(f"tenants/{tenant_id}/policies/governance")
        policy_ref.set({
            "mode": "enforce",
            "defaults": {
                "interactive": {"permission": "allow", "rateLimit": 60,
                                "transform": "audit", "postTransform": "audit"},
                "subagent":    {"permission": "allow", "rateLimit": 60,
                                "transform": "redact", "postTransform": "redact"},
                "api":         {"permission": "allow", "rateLimit": 30,
                                "transform": "redact", "postTransform": "redact"},
                "background":  {"permission": "deny", "rateLimit": 20,
                                "transform": "redact", "postTransform": "redact"},
            },
            "updatedBy": "agentgovbench-bootstrap",
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }, merge=True)

        provisioned.append({"slug": slug, "tenant_id": tenant_id, "users": tcfg["users"]})

    # 5. Emit env yaml
    out = Path(__file__).resolve().parent / "benchmark_env.yaml"
    lines = [
        "# Generated by setup/bootstrap_tenant.py. Consumed by runners/acp.py.",
        f"project: {project}",
        "# Primary tenant for single-tenant scenarios",
        f"tenant_id: {provisioned[0]['tenant_id']}",
        f"tenant_slug: {provisioned[0]['slug']}",
        "tenants:",
    ]
    for tp in provisioned:
        lines.append(f"  - slug: {tp['slug']}")
        lines.append(f"    tenant_id: {tp['tenant_id']}")
        lines.append(f"    users:")
        for u in tp["users"]:
            lines.append(f"      - uid: {u['uid']}")
            lines.append(f"        email: {u['email']}")
            lines.append(f"        role: {u['role']}")
    out.write_text("\n".join(lines) + "\n")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
