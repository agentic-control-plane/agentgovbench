"""Load scenarios from YAML on disk. Scenarios live in ``scenarios/<category>/*.yaml``."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .types import (
    Assertion,
    DirectToolCall,
    Delegation,
    GatewayFailure,
    ParallelFanOut,
    Policy,
    PolicyChange,
    Scenario,
    Setup,
    TierPolicy,
    Tenant,
    Tool,
    User,
)


ACTION_KINDS = {
    "direct_tool_call": DirectToolCall,
    "delegation": Delegation,
    "parallel_fan_out": ParallelFanOut,
    "gateway_failure": GatewayFailure,
    "policy_change": PolicyChange,
}


def _load_fixture(fixture_name: str, fixtures_dir: Path) -> dict[str, Any]:
    fpath = fixtures_dir / f"{fixture_name}.yaml"
    if not fpath.exists():
        raise FileNotFoundError(f"fixture not found: {fpath}")
    with open(fpath) as fh:
        return yaml.safe_load(fh) or {}


def _build_setup(setup_dict: dict[str, Any], fixtures_dir: Path) -> Setup:
    if "use_fixture" in setup_dict:
        fixture = _load_fixture(setup_dict["use_fixture"], fixtures_dir)
        setup_dict = {**fixture, **{k: v for k, v in setup_dict.items() if k != "use_fixture"}}
    tenants = []
    for t in setup_dict.get("tenants", []):
        users = [User(**u) for u in t.get("users", [])]
        pol = t.get("policy", {})
        policy = Policy(
            defaults={k: TierPolicy(**v) for k, v in pol.get("defaults", {}).items()},
            tools={tool: {tier: TierPolicy(**tp) for tier, tp in tiers.items()}
                   for tool, tiers in pol.get("tools", {}).items()},
            users={uid: {tier: TierPolicy(**tp) for tier, tp in tiers.items()}
                   for uid, tiers in pol.get("users", {}).items()},
            fail_mode=pol.get("fail_mode", "fail_closed"),
        )
        tenants.append(Tenant(
            id=t["id"], name=t.get("name", t["id"]),
            users=users, policy=policy,
        ))
    tools = [Tool(**tl) for tl in setup_dict.get("tools", [])]
    return Setup(tenants=tenants, tools=tools)


def _build_action(entry: dict[str, Any]):
    if len(entry) != 1:
        raise ValueError(f"action entry must have exactly one key, got {list(entry)}")
    (kind, params), = entry.items()
    cls = ACTION_KINDS.get(kind)
    if cls is None:
        raise ValueError(f"unknown action kind: {kind}")
    params = dict(params or {})
    params["kind"] = kind
    return cls(**params)


def load_scenario(path: str | Path, fixtures_dir: str | Path | None = None) -> Scenario:
    path = Path(path)
    fixtures_dir = Path(fixtures_dir) if fixtures_dir else path.parent.parent / "fixtures"
    with open(path) as fh:
        doc = yaml.safe_load(fh)
    setup = _build_setup(doc.get("setup", {}), fixtures_dir)
    actions = [_build_action(e) for e in doc.get("actions", [])]
    expected = [Assertion(**a) for a in doc.get("expected", [])]
    return Scenario(
        id=doc["id"],
        category=doc["category"],
        version=doc.get("version", 1),
        spec_version=doc.get("spec_version", "0.2"),
        nist=doc.get("nist", []),
        summary=doc.get("summary", ""),
        description=doc.get("description", ""),
        llm_required=doc.get("llm_required", False),
        setup=setup,
        actions=actions,
        expected=expected,
    )


def load_all(scenarios_dir: str | Path, category: str | None = None) -> list[Scenario]:
    scenarios_dir = Path(scenarios_dir)
    fixtures_dir = scenarios_dir.parent / "fixtures"
    results: list[Scenario] = []
    subdirs = [scenarios_dir / category] if category else sorted(
        [p for p in scenarios_dir.iterdir() if p.is_dir()]
    )
    for sub in subdirs:
        for yml in sorted(sub.glob("*.yaml")):
            try:
                results.append(load_scenario(yml, fixtures_dir))
            except Exception as e:
                raise RuntimeError(f"failed to load {yml}: {e}") from e
    return results
