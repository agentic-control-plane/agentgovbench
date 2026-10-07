"""Compare a fresh AgentGovBench run against a committed baseline.

Exit 1 if any scenario that PASSED in the baseline fails now (a regression).
Scenarios that were already failing/declined in the baseline don't fail the
check; newly passing ones are reported as improvements.

    python scripts/regression_check.py results/acp_api-v0.1.0-live.json current.json
"""
from __future__ import annotations

import json
import os
import sys

SCENARIOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scenarios")


def catalogue_ids() -> set[str] | None:
    """Scenario ids that exist in this checkout (``<category>.<file stem>``),
    or None when the scenarios tree is not next to this script. A baseline id
    that is no longer in the catalogue was renamed or retired, not skipped:
    the daily run has been failing on exactly that since #10 renamed
    audit_completeness 05/06 while the committed baseline kept the old ids."""
    if not os.path.isdir(SCENARIOS_DIR):
        return None
    ids: set[str] = set()
    for cat in os.listdir(SCENARIOS_DIR):
        cat_dir = os.path.join(SCENARIOS_DIR, cat)
        if not os.path.isdir(cat_dir):
            continue
        for name in os.listdir(cat_dir):
            if name.endswith((".yaml", ".yml")):
                ids.add(f"{cat}.{name.rsplit('.', 1)[0]}")
    return ids


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
    catalogue = catalogue_ids()
    absent = sorted(s for s, ok in base.items() if ok and s not in cur)
    retired = [s for s in absent if catalogue is not None and s not in catalogue]
    missing = [s for s in absent if s not in retired]
    improved = sorted(s for s, ok in cur.items() if ok and base.get(s) is False)

    print(f"baseline {sum(base.values())}/{len(base)}  current {sum(cur.values())}/{len(cur)}")
    for s in improved:
        print(f"IMPROVED   {s}")
    for s in retired:
        print(f"RETIRED    {s} (passed in baseline; no longer in scenarios/ — refresh the baseline)")
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
