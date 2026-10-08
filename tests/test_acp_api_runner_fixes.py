"""acp_api runner fixes: audit filtering, cooldown arming, fan-out pacing,
and the scorecard naming the declined scope_inheritance.04."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import runners.acp_api as acp_api
from runners.acp import Runner as AcpRunner


def _runner(monkeypatch):
    monkeypatch.setenv("ACP_API_KEY", "gsk_x_abc")
    return acp_api.Runner()


class _Resp:
    ok = True
    status_code = 200
    text = ""

    def __init__(self, entries):
        self._entries = entries

    def json(self):
        return {"entries": self._entries}


def test_audit_log_counts_only_governed_tool_rows(monkeypatch):
    r = _runner(monkeypatch)
    r._scenario_start_ts = time.time()
    monkeypatch.setattr(acp_api.time, "sleep", lambda s: None)
    rows = [
        {"tool": "Read", "hookEvent": "PreToolUse", "decision": "allow"},
        {"tool": "Read", "hookEvent": "PostToolUse", "decision": "allow"},
        {"tool": "llm.chat", "hookEvent": "LlmCall", "decision": "allow"},
        {"tool": "workspacePolicy", "hookEvent": "PolicyChange"},
        {"tool": "setup", "hookEvent": "Setup"},
        {"tool": "keys", "hookEvent": "AdminAction"},
        {"tool": "Read", "decision": "allow"},  # older deployment, no hookEvent
    ]
    monkeypatch.setattr(
        acp_api.requests, "get", lambda *a, **k: _Resp(rows))
    tools = [e.tool for e in r.audit_log()]
    assert tools == ["Read", "Read", "llm.chat", "Read"]


@pytest.mark.parametrize("category", ["rate_limit_cascade", "cross_tenant_isolation"])
def test_cooldown_arms_for_any_heavy_category(monkeypatch, category):
    r = _runner(monkeypatch)
    monkeypatch.setattr(r, "setup_policy_only", lambda s: None)
    from benchmark.runner import StatefulRunner
    monkeypatch.setattr(StatefulRunner, "setup", lambda self, s: None)
    monkeypatch.setattr(acp_api.time, "sleep", lambda s: None)
    scenario = SimpleNamespace(
        category=category,
        actions=[SimpleNamespace(calls_per_worker=15, worker_count=2)],
        setup=SimpleNamespace(tenants=[]),
    )
    r.setup(scenario)
    assert AcpRunner._pending_rate_heavy is True

    light = SimpleNamespace(
        category=category,
        actions=[SimpleNamespace(calls_per_worker=2, worker_count=2)],
        setup=SimpleNamespace(tenants=[]),
    )
    r.setup(light)
    assert AcpRunner._pending_rate_heavy is False


def test_fan_out_finishes_inside_window_with_slow_calls(monkeypatch):
    r = _runner(monkeypatch)

    def slow(call):
        time.sleep(0.05)
        return call.agent_name

    monkeypatch.setattr(r, "_do_direct", slow)
    fan = SimpleNamespace(
        worker_count=1, calls_per_worker=40, tool="read_file", input={},
        as_user="u", as_tenant=None, agent_tier="subagent", window_seconds=2,
    )
    t0 = time.monotonic()
    r._do_fan_out(fan)
    assert time.monotonic() - t0 < 2.0


def test_scorecard_names_scope_inheritance_04():
    # scripts/scorecard.py renders every declined_categories key as
    # "fail (declined)" with its reason; the founder decision is that 04
    # stays a counted fail, named.
    import inspect
    src = inspect.getsource(acp_api.Runner)
    assert '"scope_inheritance.04_task_narrowing"' in src
