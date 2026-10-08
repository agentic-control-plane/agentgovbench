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


def not_applicable(path: str) -> dict[str, str]:
    """Scenario ids the run reported N/A for its runner, with the reason.

    N/A means the adapter could not exercise the scenario (no outage
    simulation, one tenant credential). It is neither a pass nor a fail
    and must not fail the check — but it is printed, every time, so a
    scenario quietly leaving the denominator is never invisible. Files
    written before the status field exist carry no N/A."""
    with open(path) as f:
        data = json.load(f)
    return {
        r["scenario_id"]: r.get("na_reason") or ""
        for r in data.get("results", [])
        if r.get("status") == "na"
    }


def declined(path: str) -> dict[str, str]:
    with open(path) as f:
        data = json.load(f)
    return dict((data.get("runner") or {}).get("declined_categories") or {})


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
    cur_na = not_applicable(current_path)
    cur_declined = declined(current_path)
    # N/A and declined scenarios are outside the score on both sides: a
    # baseline pass that is now N/A is not a regression (the adapter did
    # not try), and a declined scenario was never the product's claim.
    excluded = set(cur_na) | set(cur_declined)
    regressions = sorted(
        s for s, ok in base.items()
        if ok and cur.get(s) is False and s not in excluded
    )
    catalogue = catalogue_ids()
    absent = sorted(s for s, ok in base.items() if ok and s not in cur)
    retired = [s for s in absent if catalogue is not None and s not in catalogue]
    missing = [s for s in absent if s not in retired]
    improved = sorted(s for s, ok in cur.items() if ok and base.get(s) is False)

    def scored(passed: dict[str, bool], excl: set[str]) -> tuple[int, int]:
        ids = [s for s in passed if s not in excl]
        return sum(passed[s] for s in ids), len(ids)

    bp, bt = scored(base, set(declined(baseline_path)) | set(not_applicable(baseline_path)))
    cp, ct = scored(cur, excluded)
    print(f"baseline {bp}/{bt}  current {cp}/{ct}"
          + (f"  ({len(cur_na)} N/A for this runner" if cur_na else "")
          + (f"{', ' if cur_na else '  ('}{len(cur_declined)} declined)" if cur_declined
             else (")" if cur_na else "")))
    for s in sorted(cur_na):
        was = " (passed in baseline)" if base.get(s) else ""
        print(f"N/A        {s}{was} — {cur_na[s]}")
    for s in sorted(cur_declined):
        print(f"DECLINED   {s} — {cur_declined[s]}")
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
