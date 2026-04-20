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
from .runner import BaseRunner
from .scorer import aggregate, score_scenario
from .types import ScenarioResult


DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"
DEFAULT_RUNNERS_PACKAGE = "runners"


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


def _result_to_dict(r: ScenarioResult) -> dict:
    return {
        "scenario_id": r.scenario_id,
        "scenario_version": r.scenario_version,
        "category": r.category,
        "runner": r.runner,
        "passed": r.passed,
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
def run(runner: str, category: Optional[str], scenarios_dir: str, out: Optional[str],
        as_json: bool, limit: Optional[int], verbose: bool) -> None:
    """Run scenarios against a runner."""
    runner_inst = _load_runner(runner)
    scenarios = load_all(scenarios_dir, category=category)
    if limit:
        scenarios = scenarios[:limit]
    if not scenarios:
        click.echo("no scenarios found", err=True)
        sys.exit(1)

    results: list[ScenarioResult] = []
    for i, scn in enumerate(scenarios, 1):
        t0 = time.time()
        try:
            runner_inst.setup(scn)
            for action in scn.actions:
                runner_inst.execute_action(action)
            outcome = runner_inst.collect_outcome()
        except Exception as e:
            click.echo(f"[{i}/{len(scenarios)}] {scn.id}: RUNNER ERROR {e!r}", err=True)
            continue
        finally:
            try:
                runner_inst.teardown()
            except Exception:
                pass
        wall = (time.time() - t0) * 1000
        res = score_scenario(scn, outcome, runner_inst.metadata.name, wall)
        results.append(res)
        status = "✓" if res.passed else "✗"
        if verbose or not res.passed:
            click.echo(f"[{i}/{len(scenarios)}] {status} {scn.id}  ({wall:.0f}ms)")
            if not res.passed:
                for a in res.assertion_results:
                    if not a.passed:
                        click.echo(f"    ✗ {a.assertion.kind} — {a.note}")

    agg = aggregate(results)

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
            },
            "aggregate": agg,
            "results": [_result_to_dict(r) for r in results],
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
    click.echo("=" * 70)
    click.echo()
    click.echo(f"{'Category':<36} {'Pass':>6} {'Rate':>8}")
    click.echo("-" * 56)
    for row in agg["by_category"]:
        rate = f"{row['pass_rate'] * 100:.0f}%"
        click.echo(f"{row['category']:<36} {row['passed']:>3}/{row['total']:<2} {rate:>8}")
    click.echo("-" * 56)
    click.echo(f"{'total':<36} {agg['total_passed']:>3}/{agg['total_scenarios']:<2}")
    for cat, reason in (meta.declined_categories or {}).items():
        click.echo(f"  ({cat}: N/A — {reason})")


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
