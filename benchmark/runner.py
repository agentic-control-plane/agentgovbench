"""The BaseRunner interface every governance product implements.

A runner receives a Scenario, sets up its tenant/policy state via the
vendor's own APIs, executes the action sequence, and reports observed
outcomes (tool decisions + audit log). The benchmark harness scores
those outcomes against the scenario's assertions.

Vendors contributing a runner must:
  1. Subclass BaseRunner
  2. Implement setup / execute_action / audit_log / teardown
  3. Document any non-default configuration in the runner module's
     docstring (runners are public code; deviations are visible)
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional

from .types import Action, AuditEntry, RunOutcome, Scenario, ToolOutcome


@dataclass
class RunnerMetadata:
    """Self-reported identity of a runner. Displayed in results."""
    name: str
    version: str
    product: str
    vendor: Optional[str] = None
    notes: str = ""
    # Categories the runner declines to participate in (N/A in scorecard).
    # Each entry must include a short human-readable justification.
    declined_categories: dict[str, str] = field(default_factory=dict)


class BaseRunner(abc.ABC):
    """Abstract runner. One instance per scenario run.

    Lifecycle:
        runner = MyRunner()
        runner.setup(scenario)
        for action in scenario.actions:
            runner.execute_action(action)
        outcome = runner.collect_outcome()
        runner.teardown()
    """

    @property
    @abc.abstractmethod
    def metadata(self) -> RunnerMetadata:
        """Self-report name / version / vendor. Surfaces in results."""

    @abc.abstractmethod
    def setup(self, scenario: Scenario) -> None:
        """Install tenants, users, tools, policies in the vendor's backend
        so that the scenario can run. MUST be idempotent-ish: successive
        scenarios may reuse the same backing tenant with different policy."""

    @abc.abstractmethod
    def execute_action(self, action: Action) -> Optional[ToolOutcome]:
        """Push an action through the governance layer. Returns the tool-
        execution outcome for tool-call-like actions; returns None for
        actions that don't produce a direct tool outcome (e.g. delegation
        setup, policy change, gateway failure simulation).

        Runners are responsible for internally queuing audit entries
        that ``audit_log()`` will return."""

    @abc.abstractmethod
    def audit_log(self) -> list[AuditEntry]:
        """Return the audit log entries emitted during this scenario run.
        Order preserved if possible. Vendor-specific fields go in
        ``AuditEntry.extra``; required fields must be populated."""

    @abc.abstractmethod
    def teardown(self) -> None:
        """Clean up any state installed during setup. Runners may choose
        to no-op here if state is shared across runs (document this)."""

    # ── Optional hooks — default implementations provided ─────────────

    def collect_outcome(self) -> RunOutcome:
        """Assemble the RunOutcome from accumulated tool outcomes + audit.
        Default assembles from ``_tool_outcomes`` list the runner appends
        to during ``execute_action``."""
        return RunOutcome(
            tool_outcomes=getattr(self, "_tool_outcomes", []),
            audit_entries=self.audit_log(),
            gateway_reachable=getattr(self, "_gateway_reachable", True),
            runner_errors=getattr(self, "_errors", []),
        )


# ── Utility base class with common state management ─────────────────


class StatefulRunner(BaseRunner):
    """Convenience base that tracks state between actions. Subclasses
    focus on the vendor-specific integration; this class handles the
    boring housekeeping."""

    def __init__(self) -> None:
        self._scenario: Optional[Scenario] = None
        self._tool_outcomes: list[ToolOutcome] = []
        self._audit: list[AuditEntry] = []
        self._gateway_reachable: bool = True
        self._errors: list[str] = []

    def setup(self, scenario: Scenario) -> None:
        self._scenario = scenario
        self._tool_outcomes = []
        self._audit = []
        self._gateway_reachable = True
        self._errors = []

    def audit_log(self) -> list[AuditEntry]:
        return list(self._audit)

    def teardown(self) -> None:
        pass
