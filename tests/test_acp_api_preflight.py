"""acp_api preflight: the policy-write probe must really try the write.

Since gatewaystack-connect #1365/#1379 an API key with `bench.impersonate`
on a dedicated benchmark workspace MAY write policy, so the runner uses the
key for setup by default. The probe PUTs the workspace policy back to
itself with that credential and lets the gateway decide. Two outcomes are
pinned here:

  - gateway accepts the write  -> preflight returns, the run proceeds;
  - gateway says 403 human-auth-required -> preflight refuses with the
    way out (Create benchmark workspace / ACP_ADMIN_TOKEN) and no
    scorecard is ever produced.
"""
from __future__ import annotations

import json

import pytest

import runners.acp_api as acp_api


class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> dict:
        return self._body


class _FakeRequests:
    """Stands in for the `requests` module inside runners.acp_api."""

    def __init__(self, put_resp: _Resp):
        self.put_resp = put_resp
        self.puts: list[tuple[str, dict, dict]] = []

    def get(self, url: str, **kw) -> _Resp:
        if url.endswith("/admin/audit?limit=1"):
            return _Resp(200, {"entries": []})
        return _Resp(200, {"mode": "enforce", "defaults": {"x": 1}, "tools": {}})

    def put(self, url: str, headers=None, json=None, **kw) -> _Resp:
        self.puts.append((url, headers or {}, json or {}))
        return self.put_resp


@pytest.fixture
def key_only_env(monkeypatch):
    monkeypatch.setenv("ACP_API_KEY", "gsk_test_key")
    monkeypatch.setenv("ACP_TENANT_SLUG", "agentgovbench")
    monkeypatch.setenv("ACP_BASE_URL", "https://acp.test")
    for var in ("ACP_ADMIN_TOKEN", "AGB_POLICY_SETUP", "ACP_API_KEY_B",
                "AGB_ALLOW_TENANT"):
        monkeypatch.delenv(var, raising=False)


def test_key_write_allowed_preflight_proceeds(key_only_env, monkeypatch):
    fake = _FakeRequests(_Resp(200, {"ok": True}))
    monkeypatch.setattr(acp_api, "requests", fake)

    acp_api.Runner().preflight()  # must not raise

    # The probe is a REAL write with the API key, echoing the document read.
    assert len(fake.puts) == 1
    url, headers, body = fake.puts[0]
    assert url == "https://acp.test/agentgovbench/admin/workspacePolicy"
    assert headers["Authorization"] == "Bearer gsk_test_key"
    assert body == {"mode": "enforce", "defaults": {"x": 1}, "tools": {}}


def test_human_auth_required_refuses_with_the_way_out(key_only_env, monkeypatch):
    fake = _FakeRequests(_Resp(403, {"error": "human-auth-required"}))
    monkeypatch.setattr(acp_api, "requests", fake)

    with pytest.raises(RuntimeError) as excinfo:
        acp_api.Runner().preflight()

    msg = str(excinfo.value)
    assert "human-auth-required" in msg
    assert "not a benchmark workspace" in msg
    assert "Create benchmark workspace" in msg
    assert "ACP_ADMIN_TOKEN" in msg
    assert len(fake.puts) == 1, "the write must actually have been attempted"


def test_other_write_refusal_is_still_fatal(key_only_env, monkeypatch):
    fake = _FakeRequests(_Resp(403, {"error": "api key lacks bench.impersonate"}))
    monkeypatch.setattr(acp_api, "requests", fake)

    with pytest.raises(RuntimeError) as excinfo:
        acp_api.Runner().preflight()
    assert "policy WRITE with ACP_API_KEY -> HTTP 403" in str(excinfo.value)


def test_admin_token_takes_over_policy_setup(key_only_env, monkeypatch):
    monkeypatch.setenv("ACP_ADMIN_TOKEN", "firebase-id-token")
    fake = _FakeRequests(_Resp(200, {"ok": True}))
    monkeypatch.setattr(acp_api, "requests", fake)

    runner = acp_api.Runner()
    runner.preflight()

    _, headers, _ = fake.puts[0]
    assert headers["Authorization"] == "Bearer firebase-id-token"
    # The exercise credential stays the API key — the two are never swapped.
    assert runner._admin_headers()["Authorization"] == "Bearer gsk_test_key"
