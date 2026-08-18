"""Base runner for subjects driven through a real pi agent session.

Replaces the hand-written Python runners. Those simulated an agent, and a
simulation can substitute its own behaviour for what a scenario declared —
which it did four separate times: a hardcoded agent_tier, a dropped policy
change, a synthesized audit entry, and an enforcement shim for a feature the
product does not have. Each produced a failure that looked like a product gap.

Here the decision is observed from EXECUTION. A tool whose execute() fired was
allowed; one that never fired was blocked. The driver cannot manufacture that,
which is the whole point of the replacement.

Shape: this class maps a Scenario onto the executor's JSON spec and shells out
to drivers/pi/executor.mjs (Node 22 — pi does not run on Node 20). Subclasses
supply the governance extension under test and any setup it needs.

SUBSTRATE LIMITS — declared, not scored. These are properties of pi, and a
subject is not penalised for them:

  * No agent-tier concept. pi exposes only `mode` and `hasUI`. ACP's plugin
    synthesises a tier from an env var, which a competing layer attached to pi
    would not see. Tier is therefore PASSED THROUGH but disclosed, not treated
    as a capability under test — scoring it would structurally favour ACP.
  * No native subagents. The shipped example spawns child processes with no
    parent-session link, so delegation provenance is not observable. The
    delegation_provenance and scope_inheritance categories are declined here
    and belong to framework-native subjects that have real delegation.
  * In-message parallelism serialises at the hook (tool calls are prepared
    sequentially; only execution overlaps). Fan-out uses N sessions.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from benchmark.runner import RunnerMetadata, StatefulRunner
from benchmark.types import (
    Action,
    AuditEntry,
    DirectToolCall,
    Delegation,
    GatewayFailure,
    ParallelFanOut,
    PolicyChange,
    Scenario,
    ToolOutcome,
)

DRIVER_DIR = Path(__file__).resolve().parent.parent / "drivers" / "pi"
EXECUTOR = DRIVER_DIR / "executor.mjs"

# pi requires Node >= 22.19; the common default is 20, where it fails to
# import with `webidl.util.markAsUncloneable is not a function`.
MIN_NODE_MAJOR = 22

# Categories pi cannot express. Declined rather than failed — see the
# module docstring.
SUBSTRATE_DECLINED = {
    "delegation_provenance": (
        "pi has no native subagents. Its shipped delegation example spawns "
        "child processes with no parent-session link, so a delegation chain "
        "is not observable. Injecting a correlation ID would mean scoring a "
        "mechanism the benchmark authored rather than one the harness "
        "provides. Scored against framework-native subjects instead."
    ),
    "scope_inheritance": (
        "Depends on a parent agent delegating narrowed scope to a child; pi "
        "has no native subagent to narrow. Same reasoning as "
        "delegation_provenance."
    ),
}


def _node_bin() -> str:
    """Locate a Node >= 22. Explicit AGB_NODE wins; otherwise try fnm."""
    explicit = os.environ.get("AGB_NODE")
    if explicit:
        return explicit
    for candidate in ("node",):
        path = shutil.which(candidate)
        if not path:
            continue
        try:
            out = subprocess.run([path, "--version"], capture_output=True,
                                 text=True, timeout=10).stdout.strip()
            if int(out.lstrip("v").split(".")[0]) >= MIN_NODE_MAJOR:
                return path
        except Exception:
            pass
    # fnm keeps versions outside PATH; ask it for one.
    fnm = shutil.which("fnm") or "/opt/homebrew/bin/fnm"
    if Path(fnm).exists():
        try:
            root = subprocess.run(
                [fnm, "exec", "--using", str(MIN_NODE_MAJOR), "node", "-e",
                 "process.stdout.write(process.execPath)"],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            if root:
                return root
        except Exception:
            pass
    raise RuntimeError(
        f"no Node >= {MIN_NODE_MAJOR} found. pi will not import on Node 20. "
        "Install one (fnm install 22) or set AGB_NODE to its path."
    )


class PiRunner(StatefulRunner):
    """Drives scenarios through a real pi session. Subclass per subject."""

    #: Absolute paths to governance extensions loaded into the session.
    extensions: list[str] = []
    #: Env passed to the executor process (credentials, tier hints).
    driver_env: dict[str, str] = {}

    @property
    def metadata(self) -> RunnerMetadata:  # pragma: no cover - overridden
        raise NotImplementedError

    def preflight(self) -> None:
        if not EXECUTOR.exists():
            raise RuntimeError(f"executor not found at {EXECUTOR}")
        node = _node_bin()
        out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
        if int(out.lstrip("v").split(".")[0]) < MIN_NODE_MAJOR:
            raise RuntimeError(f"{node} is {out}; pi needs >= {MIN_NODE_MAJOR}")
        if not (DRIVER_DIR / "node_modules").exists():
            raise RuntimeError(
                f"driver deps missing. Run: cd {DRIVER_DIR} && npm install --ignore-scripts"
            )

    # ── Scenario → spec ────────────────────────────────────────────────

    def setup(self, scenario: Scenario) -> None:
        super().setup(scenario)
        self._calls: list[dict[str, Any]] = []
        self._declined = scenario.category in SUBSTRATE_DECLINED

    def execute_action(self, action: Action) -> Optional[ToolOutcome]:
        """Collect calls; nothing is dispatched until collect_outcome().

        pi runs a whole scripted sequence in one session, so actions are
        accumulated and executed together rather than one at a time.
        """
        if self._declined:
            return None
        if isinstance(action, DirectToolCall):
            self._calls.append({
                "id": f"c{len(self._calls)}",
                "tool": action.tool,
                "args": action.input,
                # Passed through for the extension to read; NOT scored.
                "tier": action.agent_tier,
                "as_user": action.as_user,
                "agent_name": action.agent_name,
            })
        elif isinstance(action, ParallelFanOut):
            for i in range(action.worker_count * action.calls_per_worker):
                self._calls.append({
                    "id": f"c{len(self._calls)}",
                    "tool": action.tool,
                    "args": action.input,
                    "tier": action.agent_tier,
                    "as_user": action.as_user,
                    "agent_name": f"worker-{i // action.calls_per_worker}",
                })
        elif isinstance(action, (Delegation, GatewayFailure, PolicyChange)):
            # Delegation: no native subagents (category declined).
            # GatewayFailure: induced by the subject's own transport, not here.
            # PolicyChange: subclass responsibility — it is product config.
            self._apply_non_call_action(action)
        return None

    def _apply_non_call_action(self, action: Action) -> None:
        """Override for policy changes / induced outages. Default: record."""
        self._errors.append(
            f"{type(action).__name__} not handled by {self.metadata.name}"
        )

    def collect_outcome(self):
        if not self._declined and self._calls:
            self._dispatch()
        return super().collect_outcome()

    #: Env var a subject uses to declare the tier for a process, if any.
    #: When set, calls are grouped by tier and one executor process runs per
    #: group — pi has no per-call tier, so a mixed-tier scenario cannot be
    #: expressed in a single session.
    tier_env_var: Optional[str] = None

    def _dispatch(self) -> None:
        # One executor process per (user, tier). Neither is expressible
        # per-call: pi has no tier concept, and a harness plugin
        # authenticates with one credential that IS its identity. A scenario
        # mixing users or tiers therefore needs a session each, which is
        # also how it works in production — a key belongs to a person.
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for c in self._calls:
            groups.setdefault(
                (c.get("as_user") or "", c.get("tier") or "interactive"), []
            ).append(c)
        for (user, tier), calls in groups.items():
            env = self._env_for_group(user, tier)
            if env is None:
                self._errors.append(
                    f"no credential for user {user!r} — its calls were not "
                    "dispatched, so this scenario is unmeasured rather than failed"
                )
                continue
            self._dispatch_group(calls, env)

    def _env_for_group(self, user: str, tier: str) -> Optional[dict[str, str]]:
        """Env for one (user, tier) session. None means "cannot run this group".

        Subclasses that authenticate per user override this to select that
        user's credential.
        """
        return {self.tier_env_var: tier} if self.tier_env_var else {}

    def _dispatch_group(self, calls: list[dict[str, Any]], extra_env: dict[str, str]) -> None:
        spec = {
            "extensions": list(self.extensions),
            "calls": [
                {"id": c["id"], "tool": c["tool"], "args": c["args"]}
                for c in calls
            ],
            "env": {**self.driver_env, **extra_env},
        }
        try:
            proc = subprocess.run(
                [_node_bin(), str(EXECUTOR)],
                input=json.dumps(spec),
                capture_output=True, text=True, timeout=180,
                cwd=str(DRIVER_DIR),
            )
        except subprocess.TimeoutExpired:
            self._errors.append("pi executor timed out after 180s")
            return
        if proc.returncode != 0:
            self._errors.append(f"pi executor exit {proc.returncode}: {proc.stderr[:300]}")
            return
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self._errors.append(f"pi executor produced non-JSON: {proc.stdout[:200]}")
            return

        for err in result.get("errors", []):
            self._errors.append(f"pi: {err}")

        by_id = {c["id"]: c for c in calls}
        for o in result.get("outcomes", []):
            src = by_id.get(o["id"], {})
            self._tool_outcomes.append(ToolOutcome(
                tool=o["tool"],
                input=o.get("args", {}),
                as_user=src.get("as_user", ""),
                as_tenant=None,
                allowed=bool(o.get("allowed")),
                reason=o.get("reason"),
                agent_tier=src.get("tier"),
                agent_name=src.get("agent_name"),
            ))

    def audit_log(self) -> list[AuditEntry]:
        """Subjects that expose an audit trail override this."""
        return list(self._audit)
