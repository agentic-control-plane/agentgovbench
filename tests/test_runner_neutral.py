"""The runner must not change the score.

A scenario an adapter physically cannot exercise is N/A for that adapter:
never executed, never passed, never failed, outside both the numerator and
the denominator. These tests pin that contract at every layer — the
capability derivation, the aggregate, the CLI gate, and the regression
check — so no layer can quietly turn "the adapter could not try" into a
pass or a fail.

Run: pytest tests/test_runner_neutral.py -v
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from benchmark.loader import load_all
from benchmark.scorer import (
    aggregate,
    na_result,
    not_applicable_reason,
    required_capabilities,
)
from benchmark.types import ScenarioResult

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = ROOT / "scenarios"
REGRESSION_CHECK = ROOT / "scripts" / "regression_check.py"


def _by_id():
    return {s.id: s for s in load_all(SCENARIOS_DIR)}


def test_outage_scenarios_require_simulate_outage():
    needs = {sid: required_capabilities(s) for sid, s in _by_id().items()}
    outage = {sid for sid, n in needs.items() if "simulate_outage" in n}
    assert outage == {
        "fail_mode_discipline.01_fail_closed_honored",
        "fail_mode_discipline.02_fail_open_honored",
        "fail_mode_discipline.03_5xx_not_silent_allow",
        "fail_mode_discipline.04_resume_after_recovery",
        "fail_mode_discipline.05_no_audit_without_governance",
    }


def test_two_tenant_scenarios_require_multi_tenant():
    needs = {sid: required_capabilities(s) for sid, s in _by_id().items()}
    multi = {sid for sid, n in needs.items() if "multi_tenant" in n}
    assert multi == {sid for sid in needs if sid.startswith("cross_tenant_isolation.")}
    assert len(multi) == 6


def test_undeclared_capability_is_assumed_available():
    s = _by_id()["fail_mode_discipline.01_fail_closed_honored"]
    assert not_applicable_reason(s, {}) is None
    assert not_applicable_reason(s, None) is None
    assert not_applicable_reason(s, {"simulate_outage": True}) is None
    assert "outage" in (not_applicable_reason(s, {"simulate_outage": False}) or "")


def test_na_leaves_numerator_and_denominator():
    by_id = _by_id()
    s_na = by_id["fail_mode_discipline.01_fail_closed_honored"]
    s_ok = by_id["fail_mode_discipline.06_clean_state_baseline"]
    results = [
        na_result(s_na, "r", "cannot induce outage"),
        ScenarioResult(scenario_id=s_ok.id, scenario_version=1, category=s_ok.category,
                       runner="r", passed=True, status="pass"),
    ]
    agg = aggregate(results)
    assert agg["total_scenarios"] == 1
    assert agg["total_passed"] == 1
    assert agg["na_scenarios"] == 1
    assert agg["na_ids"] == [s_na.id]
    row = agg["by_category"][0]
    assert (row["passed"], row["total"], row["na"]) == (1, 1, 1)


def test_na_result_is_not_a_pass():
    s = _by_id()["fail_mode_discipline.01_fail_closed_honored"]
    r = na_result(s, "r", "x")
    assert r.status == "na" and r.passed is False and r.assertion_results == []


def test_acp_runners_declare_no_outage_simulation():
    """The hosted gateway cannot be taken offline by either ACP adapter.
    Declaring otherwise would re-admit the client-side simulation that
    scored the adapter's own answer."""
    for name in ("acp.py", "acp_api.py"):
        src = (ROOT / "runners" / name).read_text()
        assert '"simulate_outage": False' in src, name


def test_cli_never_executes_an_na_scenario(monkeypatch, tmp_path):
    """The gate sits in front of setup(): an N/A scenario must not touch
    the runner at all, and the scorecard must say so."""
    from click.testing import CliRunner

    from benchmark import cli as cli_mod
    from benchmark.runner import RunnerMetadata, StatefulRunner
    from benchmark.types import ToolOutcome

    touched: list[str] = []

    class Stub(StatefulRunner):
        @property
        def metadata(self):
            return RunnerMetadata(name="stub", version="0", product="stub",
                                  capabilities={"simulate_outage": False})

        def setup(self, scenario):
            super().setup(scenario)
            touched.append(scenario.id)

        def execute_action(self, action):
            if hasattr(action, "tool"):
                o = ToolOutcome(tool=action.tool, input=action.input, as_user=action.as_user,
                                as_tenant=action.as_tenant, allowed=True)
                self._tool_outcomes.append(o)
                return o
            return None

    monkeypatch.setattr(cli_mod, "_load_runner", lambda name: Stub())
    out = tmp_path / "out.json"
    r = CliRunner().invoke(cli_mod.cli, [
        "run", "--runner", "stub", "--category", "fail_mode_discipline",
        "--out", str(out),
    ])
    assert r.exit_code == 0, r.output
    assert touched == ["fail_mode_discipline.06_clean_state_baseline"]
    blob = json.loads(out.read_text())
    by_id = {row["scenario_id"]: row for row in blob["results"]}
    assert sum(1 for row in by_id.values() if row["status"] == "na") == 5
    assert by_id["fail_mode_discipline.01_fail_closed_honored"]["na_reason"]
    assert blob["aggregate"]["total_scenarios"] == 1
    assert blob["aggregate"]["na_scenarios"] == 5
    assert blob["runner"]["capabilities"] == {"simulate_outage": False}
    assert "(5 N/A for runner stub)" in r.output


def _write_results(path: Path, rows: list[dict], declined: dict | None = None) -> None:
    path.write_text(json.dumps({
        "runner": {"name": "t", "declined_categories": declined or {}},
        "results": rows,
    }))


def _run_check(baseline: Path, current: Path) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, str(REGRESSION_CHECK), str(baseline), str(current)],
        capture_output=True, text=True,
    )
    return p.returncode, p.stdout


def test_regression_check_treats_na_as_visible_but_not_failing(tmp_path):
    sid = "fail_mode_discipline.01_fail_closed_honored"
    other = "fail_mode_discipline.06_clean_state_baseline"
    base = tmp_path / "base.json"
    cur = tmp_path / "cur.json"
    _write_results(base, [
        {"scenario_id": sid, "passed": True},
        {"scenario_id": other, "passed": True},
    ])
    _write_results(cur, [
        {"scenario_id": sid, "passed": False, "status": "na", "na_reason": "no outage"},
        {"scenario_id": other, "passed": True, "status": "pass"},
    ])
    code, out = _run_check(base, cur)
    assert code == 0, out
    assert "N/A        " + sid in out
    assert "(passed in baseline)" in out
    assert "REGRESSED" not in out
    assert "current 1/1" in out


def test_regression_check_still_fails_a_real_regression(tmp_path):
    sid = "fail_mode_discipline.06_clean_state_baseline"
    base = tmp_path / "base.json"
    cur = tmp_path / "cur.json"
    _write_results(base, [{"scenario_id": sid, "passed": True}])
    _write_results(cur, [{"scenario_id": sid, "passed": False, "status": "fail"}])
    code, out = _run_check(base, cur)
    assert code == 1
    assert "REGRESSED  " + sid in out


def test_regression_check_excludes_declined(tmp_path):
    sid = "scope_inheritance.04_task_narrowing"
    base = tmp_path / "base.json"
    cur = tmp_path / "cur.json"
    _write_results(base, [{"scenario_id": sid, "passed": True}])
    _write_results(cur, [{"scenario_id": sid, "passed": False, "status": "fail"}],
                   declined={sid: "product roadmap item"})
    code, out = _run_check(base, cur)
    assert code == 0, out
    assert "DECLINED   " + sid in out
