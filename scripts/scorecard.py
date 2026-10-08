"""Merge the per-runner result files into one scorecard out of ALL scenarios.

    python scripts/scorecard.py http.json hook.json --date 2026-10-08 \
        --run-url https://github.com/.../actions/runs/123

Rules, fixed in code so the published number cannot be flattered:
  * Every scenario in the catalogue is counted. The denominator is the
    number of scenario files, not the number a runner happened to run.
  * fail_mode_discipline is scored from the hook runner (the only one that
    can really take the gateway away); every other category from the HTTP
    runner (acp_api, live against production).
  * A declined scenario is a FAIL and is named with its reason.
  * A scenario no runner measured (N/A, or missing) is NOT a pass. It is
    counted against the score and listed as "not measured".
Writes results/scorecard.json and results/SCORECARD.md, prints the headline.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCEN = os.path.join(HERE, "..", "scenarios")
HOOK_CATEGORIES = {"fail_mode_discipline"}


def catalogue() -> list[str]:
    ids = []
    for cat in sorted(os.listdir(SCEN)):
        d = os.path.join(SCEN, cat)
        if os.path.isdir(d):
            ids += [f"{cat}.{n.rsplit('.', 1)[0]}" for n in sorted(os.listdir(d)) if n.endswith((".yaml", ".yml"))]
    return ids


def load(path: str) -> dict:
    with open(path) as f:
        data = json.load(f)
    return {
        "runner": (data.get("runner") or {}).get("name", "?"),
        "declined": (data.get("runner") or {}).get("declined_categories") or {},
        "by_id": {r["scenario_id"]: r for r in data.get("results", [])},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("http")
    ap.add_argument("hook")
    ap.add_argument("--date", required=True)
    ap.add_argument("--run-url", default="")
    ap.add_argument("--out-dir", default=os.path.join(HERE, "..", "results"))
    a = ap.parse_args()
    http, hook = load(a.http), load(a.hook)
    rows = []
    for sid in catalogue():
        src = hook if sid.split(".")[0] in HOOK_CATEGORIES else http
        r = src["by_id"].get(sid)
        reason = ""
        if r is None:
            status, reason = "not measured", "no result from the runner assigned to this category"
        elif sid in src["declined"]:
            status, reason = "fail (declined)", src["declined"][sid]
        elif r.get("status") == "na":
            status, reason = "not measured", r.get("na_reason") or "runner could not exercise it"
        elif r.get("passed"):
            status = "pass"
        else:
            status = "fail"
            reason = "; ".join(
                f'{x.get("kind")}: {x.get("note", "")}' for x in r.get("assertions", []) if not x.get("passed")
            )
        rows.append({"scenario": sid, "runner": src["runner"], "status": status, "note": reason})
    total = len(rows)
    passed = sum(r["status"] == "pass" for r in rows)
    summary = {"date": a.date, "passed": passed, "total": total, "run_url": a.run_url, "rows": rows}
    os.makedirs(a.out_dir, exist_ok=True)
    with open(os.path.join(a.out_dir, "scorecard.json"), "w") as f:
        json.dump(summary, f, indent=2)
    lines = [
        f"# AgentGovBench: ACP scores {passed}/{total} ({a.date})", "",
        "Counted out of every scenario. Declined scenarios count as failures and are named below; "
        "a scenario no runner measured counts against the score.", "",
        f"Run: {a.run_url or 'local'}", "",
        "| Scenario | Measured by | Result | Note |", "|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f'| {r["scenario"]} | {r["runner"]} | {r["status"]} | {r["note"].replace("|", "/")[:300]} |')
    with open(os.path.join(a.out_dir, "SCORECARD.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"ACP {passed}/{total} on {a.date}")
    for r in rows:
        if r["status"] != "pass":
            print(f'  {r["status"]:16} {r["scenario"]} ({r["runner"]}) {r["note"][:140]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
