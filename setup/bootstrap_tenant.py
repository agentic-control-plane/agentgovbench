"""One-time bootstrap: create two dedicated benchmark tenants in Firestore,
create synthetic Firebase users, attach them as tenant members.

Idempotent — rerunning reuses existing records.

TO USE AGAINST YOUR OWN ACP DEPLOYMENT:

    # 1. Download a Firebase service account JSON with admin rights on
    #    the ACP project you want to benchmark.
    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/firebase-service-account.json

    # 2. (optional) Point at a different GCP project + email domain:
    export AGB_PROJECT=your-firebase-project-id
    export AGB_EMAIL_DOMAIN=agentgovbench.yourdomain.com

    # 3. Bootstrap:
    python setup/bootstrap_tenant.py

    # 4. Run the benchmark:
    export AGB_TENANT_ID=<printed by step 3>
    export AGB_TENANT_SLUG=<printed by step 3>
    export FIREBASE_WEB_API_KEY=<your project's web API key>
    python -m benchmark.cli run --runner acp

Emits:
    setup/benchmark_env.yaml — tenant_ids, user_uids, tenant_slugs.
    The live ACP runner consumes this on startup.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Defaults — override via env when pointing at a different project.
DEFAULT_CREDS = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
DEFAULT_PROJECT = os.environ.get("AGB_PROJECT", "gatewaystack-connect")
EMAIL_DOMAIN = os.environ.get("AGB_EMAIL_DOMAIN", "agentgovbench.test")

TENANTS = [
    {
        "slug": "agentgovbench",
        "name": "AgentGovBench tenant A",
        "users": [
            {"uid": "agb-alice",  "email": f"alice@{EMAIL_DOMAIN}",  "role": "member"},
            {"uid": "agb-bob",    "email": f"bob@{EMAIL_DOMAIN}",    "role": "admin"},
            {"uid": "agb-carol",  "email": f"carol@{EMAIL_DOMAIN}",  "role": "viewer"},
        ],
    },
    {
        "slug": "agentgovbench-b",
        "name": "AgentGovBench tenant B",
        "users": [
            {"uid": "agb-dan",  "email": f"dan@{EMAIL_DOMAIN}",  "role": "member"},
            {"uid": "agb-eve",  "email": f"eve@{EMAIL_DOMAIN}",  "role": "admin"},
        ],
    },
]


def main() -> int:
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        if not DEFAULT_CREDS:
            print(
                "GOOGLE_APPLICATION_CREDENTIALS not set. Point it at a Firebase "
                "service account JSON with admin rights on the ACP project "
                "you want to benchmark, then rerun. Header of this file has "
                "full setup docs.",
                file=sys.stderr,
            )
            return 1
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
