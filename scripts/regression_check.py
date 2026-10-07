"""Compare a fresh AgentGovBench run against a committed baseline.

Exit 1 if any scenario that PASSED in the baseline fails now (a regression).
Scenarios that were already failing/declined in the baseline don't fail the
check; newly passing ones are reported as improvements.

    python scripts/regression_check.py results/acp_api-v0.1.0-live.json current.json
"""
from __future__ import annotations

import json
import sys


def passed_by_id(path: str) -> dict[str, bool]:
    with open(path) as f:
        data = json.load(f)
    return {r["scenario_id"]: bool(r["passed"]) for r in data.get("results", [])}


def failing_assertions(path: str, scenario_id: str) -> list[str]:
    with open(path) as f:
        data = json.load(f)
    for r in data.get("results", []):
        if r["scenario_id"] == scenario_id:
            return [
                f'{a.get("kind")}: {a.get("note", "")}'
                for a in r.get("assertions", [])
                if not a.get("passed")
            ]
    return []


def main(baseline_path: str, current_path: str) -> int:
    base = passed_by_id(baseline_path)
    cur = passed_by_id(current_path)
    regressions = sorted(s for s, ok in base.items() if ok and cur.get(s) is False)
    missing = sorted(s for s, ok in base.items() if ok and s not in cur)
    improved = sorted(s for s, ok in cur.items() if ok and base.get(s) is False)

    print(f"baseline {sum(base.values())}/{len(base)}  current {sum(cur.values())}/{len(cur)}")
    for s in improved:
        print(f"IMPROVED   {s}")
    for s in missing:
        print(f"MISSING    {s} (passed in baseline, not run now)")
    for s in regressions:
        print(f"REGRESSED  {s}")
        for line in failing_assertions(current_path, s):
            print(f"             {line}")
    return 1 if (regressions or missing) else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
