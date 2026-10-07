"""Command-line entry point for AgentGovBench.

Example:
    python -m agentgovbench run --runner vanilla
    python -m agentgovbench run --runner acp --category identity_propagation --json
    python -m agentgovbench run --runner acp --out results/acp.json
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import click

from . import SCENARIO_LIBRARY_VERSION, SPEC_VERSION
from .loader import load_all
from .runner import BaseRunner, RunAborted
from .scorer import aggregate, na_result, not_applicable_reason, score_scenario
from .types import ScenarioResult


DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"
DEFAULT_RUNNERS_PACKAGE = "runners"


_GOVERNANCE_SOURCE_BANNERS = {
    "seam": (
        "DECISIONS CAME FROM THE RUNNER, NOT THE SUBJECT.\n"
        "  This subject provides an interception point but ships no policy\n"
        "  engine, so the runner supplied the decision logic. These scores\n"
        "  measure whether the seam is expressive enough to carry a policy.\n"
        "  They do NOT mean the subject enforces one. Do not compare these\n"
        "  numbers head-to-head against a product-sourced column."
    ),
    "none": (
        "NO INTERCEPTION POINT. Baseline subject — scores describe what\n"
        "  happens when nothing governs the calls."
    ),
}


def _echo_governance_source(source: str) -> None:
    """Print who actually made the decisions, when it is not the product.

    A scorecard where one column's verdicts come from a vendor engine and
    another's come from the benchmark author is not a comparison, and the
    difference is invisible in the numbers. It gets a banner, not a
    footnote.
    """
    banner = _GOVERNANCE_SOURCE_BANNERS.get(source)
    if banner:
        click.echo(f"Governance source: {source.upper()} — {banner}")


def _load_runner(name: str) -> BaseRunner:
    """Load a runner by its module name under the runners/ package.

    ``--runner acp`` → ``runners.acp.Runner``
    ``--runner my_vendor`` → ``runners.my_vendor.Runner``

    A runner module must expose a class named ``Runner``.
    """
    module = importlib.import_module(f"{DEFAULT_RUNNERS_PACKAGE}.{name}")
    cls = getattr(module, "Runner", None)
    if cls is None:
        raise click.ClickException(f"runner module {name!r} has no `Runner` class")
    return cls()


def _result_to_dict(r: ScenarioResult, include_outcome: bool = False) -> dict:
    d = {
        "scenario_id": r.scenario_id,
        "scenario_version": r.scenario_version,
        "category": r.category,
        "runner": r.runner,
        "passed": r.passed,
        # "pass" | "fail" | "na". N/A = this runner could not exercise the
        # scenario; it was not run and is outside the score either way.
        "status": r.status,
        "na_reason": r.na_reason,
        "nist_controls": r.nist_controls,
        "wall_time_ms": r.wall_time_ms,
        "assertions": [
            {
                "kind": a.assertion.kind,
                "params": a.assertion.params,
                "passed": a.passed,
                "note": a.note,
                "observed": a.observed,
            }
            for a in r.assertion_results
        ],
    }
    if include_outcome and r.outcome is not None:
        # The trajectory — every tool the runner actually attempted, and
        # whether it went through. Off by default because it carries the
        # scenario's tool inputs and roughly triples the file size.
        #
        # Without it a result says only pass/fail per assertion, which cannot
        # answer "what did the agent actually DO on the way there". Severity
        # grading (L0-L6, arXiv:2607.07474) needs the trajectory: a scenario
        # can pass every assertion while the run still completed a cross-scope
        # or privilege-expanding action through a tool no assertion watched.
        d["outcome"] = {
            "gateway_reachable": r.outcome.gateway_reachable,
            "runner_errors": list(r.outcome.runner_errors),
            "tool_outcomes": [
                {
                    "tool": t.tool,
                    "input": t.input,
                    "as_user": t.as_user,
                    "as_tenant": t.as_tenant,
                    "allowed": t.allowed,
                    "reason": t.reason,
                    "agent_tier": t.agent_tier,
                    "agent_name": t.agent_name,
                }
                for t in r.outcome.tool_outcomes
            ],
            "audit_entries": [
                {
                    "timestamp": a.timestamp,
                    "tenant": a.tenant,
                    "actor_uid": a.actor_uid,
                    "tool": a.tool,
                    "decision": a.decision,
                    "reason": a.reason,
                    "delegation_chain": list(a.delegation_chain),
                }
                for a in r.outcome.audit_entries
            ],
        }
    return d


@click.group()
def cli() -> None:
    """AgentGovBench CLI."""


@cli.command()
@click.option("--runner", required=True, help="Runner module name (e.g. 'vanilla', 'acp')")
@click.option("--category", default=None, help="Limit to one category")
@click.option("--scenarios-dir", default=str(DEFAULT_SCENARIOS_DIR),
              help="Path to scenarios/ directory")
@click.option("--out", default=None, help="Write full results JSON to this path")
@click.option("--json", "as_json", is_flag=True, help="Print full results JSON to stdout")
@click.option("--limit", type=int, default=None, help="Cap number of scenarios run")
@click.option("--verbose", "-v", is_flag=True, help="Print per-scenario outcome")
@click.option("--include-outcomes", is_flag=True,
              help="Persist each scenario's trajectory (tool calls attempted + audit "
                   "entries) into the results JSON. Needed for post-hoc trajectory "
                   "analysis such as severity grading; off by default because it "
                   "carries tool inputs and enlarges the file.")
def run(runner: str, category: Optional[str], scenarios_dir: str, out: Optional[str],
        as_json: bool, limit: Optional[int], verbose: bool, include_outcomes: bool) -> None:
    """Run scenarios against a runner."""
    runner_inst = _load_runner(runner)
    scenarios = load_all(scenarios_dir, category=category)
    if limit:
        scenarios = scenarios[:limit]
    if not scenarios:
        click.echo("no scenarios found", err=True)
        sys.exit(1)

    from .types import Wait

    try:
        runner_inst.preflight()
    except Exception as e:
        click.echo(
            f"PREFLIGHT FAILED for runner '{runner_inst.metadata.name}': {e}\n"
            "\nRefusing to run. A scorecard produced when the runner cannot "
            "install policy or read decisions is not a measurement of the "
            "product — it looks like one, which is worse.",
            err=True,
        )
        sys.exit(2)

    results: list[ScenarioResult] = []
    capabilities = getattr(runner_inst.metadata, "capabilities", None) or {}
    for i, scn in enumerate(scenarios, 1):
        t0 = time.time()
        # A scenario this adapter cannot exercise is N/A for this adapter:
        # not run, not scored, not a pass or a fail. Running it anyway would
        # either fail the product for the adapter's limitation or let the
        # adapter fake the missing step — the runner must not change the
        # score either way.
        na_reason = not_applicable_reason(scn, capabilities)
        if na_reason:
            results.append(na_result(scn, runner_inst.metadata.name, na_reason))
            if verbose:
                click.echo(f"[{i}/{len(scenarios)}] – {scn.id}  N/A for runner "
                           f"{runner_inst.metadata.name}: {na_reason}")
            continue
        try:
            runner_inst.setup(scn)
            for action in scn.actions:
                # Waiting is the harness's job, not the product's. Handling
                # it here keeps every adapter identical on this axis.
                if isinstance(action, Wait):
                    # Let a batching runner send what it has BEFORE time
                    # advances, or the pre-wait calls land after it.
                    runner_inst.flush()
                    time.sleep(action.seconds)
                    continue
                runner_inst.execute_action(action)
            outcome = runner_inst.collect_outcome()
        except RunAborted as e:
            click.echo(f"\n[{i}/{len(scenarios)}] {scn.id}: RUN ABORTED — no scorecard produced.\n\n{e}", err=True)
            sys.exit(2)
        except Exception as e:
            # A scenario the runner could not drive scores FAIL. It must not
            # leave the denominator: dropping it shrinks the total and
            # rewards a fragile adapter with a better-looking pass rate,
            # which biases hardest against adapters written by someone who
            # does not own the product under test.
            from .types import Assertion, AssertionResult
            click.echo(f"[{i}/{len(scenarios)}] ✗ {scn.id}: RUNNER ERROR {e!r}", err=True)
            results.append(ScenarioResult(
                scenario_id=scn.id,
                scenario_version=scn.version,
                category=scn.category,
                runner=runner_inst.metadata.name,
                passed=False,
                assertion_results=[AssertionResult(
                    assertion=Assertion(kind="_runner_error", params={}),
                    passed=False,
                    observed=repr(e),
                    note=f"runner raised: {e!r}",
                )],
                outcome=None,
                wall_time_ms=(time.time() - t0) * 1000,
                nist_controls=list(scn.nist),
                status="fail",
            ))
            continue
        finally:
            try:
                runner_inst.teardown()
            except Exception:
                pass
        wall = (time.time() - t0) * 1000
        res = score_scenario(scn, outcome, runner_inst.metadata.name, wall)
        res.status = "pass" if res.passed else "fail"
        results.append(res)
        status = "✓" if res.passed else "✗"
        if verbose or not res.passed:
            click.echo(f"[{i}/{len(scenarios)}] {status} {scn.id}  ({wall:.0f}ms)")
            if not res.passed:
                for a in res.assertion_results:
                    if not a.passed:
                        click.echo(f"    ✗ {a.assertion.kind} — {a.note}")

    agg = aggregate(results, runner_inst.metadata.declined_categories)

    _print_scorecard(runner_inst, agg, results)

    if out or as_json:
        blob = {
            "spec_version": SPEC_VERSION,
            "scenario_library_version": SCENARIO_LIBRARY_VERSION,
            "runner": {
                "name": runner_inst.metadata.name,
                "version": runner_inst.metadata.version,
                "product": runner_inst.metadata.product,
                "vendor": runner_inst.metadata.vendor,
                "notes": runner_inst.metadata.notes,
                "declined_categories": runner_inst.metadata.declined_categories,
                "capabilities": capabilities,
            },
            "aggregate": agg,
            # Runner-side failures, deduplicated with counts. Persisted at
            # the top level so a results file can never present a score
            # without the evidence that the run was compromised — reading
            # the JSON must not be a way to miss what the console shouted.
            "runner_errors": _runner_error_tally(results),
            "results": [_result_to_dict(r, include_outcomes) for r in results],
        }
        if out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(json.dumps(blob, indent=2, default=str))
            click.echo(f"\nwrote {out}")
        if as_json:
            click.echo(json.dumps(blob, indent=2, default=str))


def _print_scorecard(runner_inst: BaseRunner, agg: dict, results: list[ScenarioResult]) -> None:
    meta = runner_inst.metadata
    click.echo()
    click.echo("=" * 70)
    click.echo(f"AgentGovBench  spec v{SPEC_VERSION}  library {SCENARIO_LIBRARY_VERSION}")
    click.echo(f"Runner: {meta.name} ({meta.product} {meta.version})"
               + (f" — {meta.vendor}" if meta.vendor else ""))
    _echo_governance_source(getattr(meta, "governance_source", "product"))
    click.echo("=" * 70)
    click.echo()
    click.echo(f"{'Category':<36} {'Pass':>6} {'Rate':>8}  {'N/A':>4} {'Decl':>4}")
    click.echo("-" * 64)
    for row in agg["by_category"]:
        rate = f"{row['pass_rate'] * 100:.0f}%"
        na = row.get("na", 0) or ""
        decl = row.get("declined", 0) or ""
        click.echo(f"{row['category']:<36} {row['passed']:>3}/{row['total']:<2} {rate:>8}  "
                   f"{na!s:>4} {decl!s:>4}")
    click.echo("-" * 64)
    click.echo(f"{'total':<36} {agg['total_passed']:>3}/{agg['total_scenarios']:<2}"
               + format_score_suffix(agg, meta.name))
    na_by_id = {r.scenario_id: r.na_reason for r in results if r.status == "na"}
    for sid in agg.get("na_ids", []):
        click.echo(f"  ({sid}: N/A for runner {meta.name} — {na_by_id.get(sid)})")
    for cat, reason in (meta.declined_categories or {}).items():
        click.echo(f"  ({cat}: declined — {reason})")

    _print_runner_errors(results)


def format_score_suffix(agg: dict, runner_name: str) -> str:
    """' (Z N/A for runner R, W declined)' — empty when neither applies.

    Shown next to every X/Y so a score can never be read without the size
    of the library the runner did not face."""
    parts = []
    if agg.get("na_scenarios"):
        parts.append(f"{agg['na_scenarios']} N/A for runner {runner_name}")
    if agg.get("declined_scenarios"):
        parts.append(f"{agg['declined_scenarios']} declined")
    return f"  ({', '.join(parts)})" if parts else ""


def _runner_error_tally(results: list[ScenarioResult]) -> dict:
    """{error: count} plus the scenarios affected. Empty dict when clean."""
    tally: dict[str, int] = {}
    affected: set[str] = set()
    for r in results:
        for err in (r.outcome.runner_errors if r.outcome else []) or []:
            tally[err] = tally.get(err, 0) + 1
            affected.add(r.scenario_id)
    if not tally:
        return {}
    return {
        "total": sum(tally.values()),
        "scenarios_affected": sorted(affected),
        "by_error": tally,
    }


def _print_runner_errors(results: list[ScenarioResult]) -> None:
    """Surface anything the runner swallowed, loudly.

    Runners accumulate failures into an internal list and carry on. That
    is the right call mid-run — one flaky read shouldn't abort 48
    scenarios — but it means a run whose SETUP never worked still
    completes and still prints a scorecard that looks like a measurement.

    This has now happened twice for real: a revoked API key produced a
    clean-looking 13/48, and a policy-write the gateway refused produced
    a 16/48. Both were meaningless, both were silent, and both cost a
    twenty-minute run to discover. Errors are deduplicated because a
    setup failure repeats once per scenario and the count is the
    interesting part, not forty-eight copies of the same line.
    """
    tally: dict[str, int] = {}
    affected: set[str] = set()
    for r in results:
        for err in (r.outcome.runner_errors if r.outcome else []) or []:
            tally[err] = tally.get(err, 0) + 1
            affected.add(r.scenario_id)

    if not tally:
        return

    click.echo()
    click.echo("!" * 70)
    click.echo(f"RUNNER ERRORS — {sum(tally.values())} across {len(affected)} scenario(s)")
    click.echo("!" * 70)
    for err, count in sorted(tally.items(), key=lambda kv: -kv[1]):
        suffix = f"  (x{count})" if count > 1 else ""
        click.echo(f"  • {err}{suffix}")
    click.echo()
    click.echo("These are failures the RUNNER hit, not verdicts the product")
    click.echo("returned. If they touch setup or audit reads, the scores above")
    click.echo("describe the runner's problems rather than the product's, and")
    click.echo("should not be reported as a result.")


@cli.command()
def list_scenarios() -> None:
    """List all scenarios in the default library."""
    scenarios = load_all(DEFAULT_SCENARIOS_DIR)
    click.echo(f"{len(scenarios)} scenarios loaded:")
    by_cat: dict[str, list[str]] = {}
    for s in scenarios:
        by_cat.setdefault(s.category, []).append(s.id)
    for cat in sorted(by_cat):
        click.echo(f"\n{cat}  ({len(by_cat[cat])})")
        for sid in sorted(by_cat[cat]):
            click.echo(f"  {sid}")


def main():
    cli()


if __name__ == "__main__":
    main()
