"""Core data types for the benchmark.

A Scenario is a fully-specified, deterministic adversarial setup. It has:
  - setup: tenants, users, tools, policies
  - actions: what the "agent" attempts to do, in order
  - expected: what the governance layer must have done about it

A Runner receives a Scenario, replays the actions through the governance
layer under test, and exposes the observable outcomes (decisions, audit
log entries). The Scorer compares observed vs expected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


# ── Setup types ────────────────────────────────────────────────────────


@dataclass
class User:
    uid: str
    email: Optional[str] = None
    role: Literal["owner", "admin", "member", "viewer"] = "member"
    scopes: list[str] = field(default_factory=list)


@dataclass
class Tool:
    name: str
    required_scopes: list[str] = field(default_factory=list)
    sensitivity: Literal["public", "user_data", "user_action", "admin"] = "user_data"
    description: Optional[str] = None


@dataclass
class TierPolicy:
    permission: Literal["allow", "flag", "deny"] = "allow"
    rate_limit_per_minute: Optional[int] = None
    post_transform: Literal["off", "audit", "redact", "block"] = "audit"


@dataclass
class Policy:
    """A workspace governance policy doc. Simplified shape across runners."""
    defaults: dict[str, TierPolicy] = field(default_factory=dict)  # tier → TierPolicy
    tools: dict[str, dict[str, TierPolicy]] = field(default_factory=dict)
    # user-scoped tier-level overrides. uid → { tier → TierPolicy }
    users: dict[str, dict[str, TierPolicy]] = field(default_factory=dict)
    # user-scoped tool-specific overrides. uid → { tool → tier → TierPolicy }.
    # Wins over workspace.tools (most-specific-wins).
    user_tools: dict[str, dict[str, dict[str, TierPolicy]]] = field(default_factory=dict)
    # declared fail mode when gateway unreachable
    fail_mode: Literal["fail_open", "fail_closed"] = "fail_closed"


@dataclass
class Tenant:
    id: str
    name: str
    users: list[User] = field(default_factory=list)
    policy: Policy = field(default_factory=Policy)


@dataclass
class Setup:
    tenants: list[Tenant] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)


# ── Action types ───────────────────────────────────────────────────────

# Actions describe what the adversarial "agent" attempts. A runner must
# route each action through its governance layer and expose what happened.


@dataclass
class DirectToolCall:
    """A single tool call, attributed to a single user, no delegation."""
    kind: Literal["direct_tool_call"] = "direct_tool_call"
    tool: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    as_user: str = ""            # uid
    as_tenant: Optional[str] = None  # tenant id, default first tenant
    agent_tier: Literal["interactive", "subagent", "background", "api"] = "interactive"
    # Optional — used by provenance tests to name the caller in the audit.
    agent_name: Optional[str] = None


@dataclass
class Delegation:
    """Orchestrator agent spawns a subagent to carry out a sub-task.
    Declared scopes are what the delegator INTENDS to hand off. The test
    assertion is usually whether this intent is enforced downstream."""
    kind: Literal["delegation"] = "delegation"
    from_agent: str = "orchestrator"
    to_agent: str = "worker"
    task: str = ""
    delegated_scopes: list[str] = field(default_factory=list)
    as_user: str = ""
    as_tenant: Optional[str] = None


@dataclass
class ParallelFanOut:
    """Spawn K subagents concurrently, each making N calls. Used to test
    aggregation: rate limit / budget / identity must sum across fan-out."""
    kind: Literal["parallel_fan_out"] = "parallel_fan_out"
    worker_count: int = 1
    calls_per_worker: int = 1
    tool: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    as_user: str = ""
    as_tenant: Optional[str] = None
    window_seconds: int = 60


@dataclass
class GatewayFailure:
    """Simulate governance layer unreachability for the next actions.
    Used to test fail-mode discipline. Modes:
      - unreachable: network-partitioned, timeouts
      - error_5xx: gateway returns 5xx
    """
    kind: Literal["gateway_failure"] = "gateway_failure"
    mode: Literal["unreachable", "error_5xx"] = "unreachable"
    duration_seconds: int = 30


@dataclass
class PolicyChange:
    """Admin updates a policy mid-scenario. Used for revocation / precedence tests."""
    kind: Literal["policy_change"] = "policy_change"
    tenant: Optional[str] = None
    user: Optional[str] = None           # target of the change, if user-scoped
    tool: Optional[str] = None           # target of the change, if tool-scoped
    tier: Optional[str] = None
    set_permission: Optional[str] = None  # allow/deny/flag
    set_rate_limit: Optional[int] = None


Action = DirectToolCall | Delegation | ParallelFanOut | GatewayFailure | PolicyChange


# ── Observed outcomes ──────────────────────────────────────────────────


@dataclass
class ToolOutcome:
    """What the runner reports happened when a tool was attempted."""
    tool: str
    input: dict[str, Any]
    as_user: str
    as_tenant: Optional[str]
    allowed: bool
    reason: Optional[str] = None
    agent_tier: Optional[str] = None
    agent_name: Optional[str] = None
    latency_ms: Optional[float] = None


@dataclass
class AuditEntry:
    """A single audit log record. Vendors may include more; we assert on
    this minimum structural set."""
    timestamp: str
    tenant: Optional[str]
    actor_uid: Optional[str]
    actor_email: Optional[str]
    tool: str
    decision: Literal["allow", "deny", "flag", "redact"]
    reason: Optional[str] = None
    trace_id: Optional[str] = None
    # Provenance: the chain of agents through which this call flowed.
    delegation_chain: list[str] = field(default_factory=list)
    # Other fields the runner emitted — not asserted on, but available.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunOutcome:
    """Complete record of what happened during a scenario run."""
    tool_outcomes: list[ToolOutcome] = field(default_factory=list)
    audit_entries: list[AuditEntry] = field(default_factory=list)
    # Did the governance layer become unreachable during the run?
    gateway_reachable: bool = True
    # Any runner-side errors.
    runner_errors: list[str] = field(default_factory=list)


# ── Assertions ─────────────────────────────────────────────────────────


@dataclass
class Assertion:
    """A single expected-outcome assertion. One of the kinds below.

    We use a generic kind+params shape so scenarios can express arbitrary
    checks without the scorer exploding in complexity. The Scorer has a
    handler per kind.
    """
    kind: str  # see Scorer.CHECKS for enum
    params: dict[str, Any] = field(default_factory=dict)
    description: Optional[str] = None


# ── Scenario ───────────────────────────────────────────────────────────


@dataclass
class Scenario:
    id: str
    category: str
    version: int = 1
    spec_version: str = "0.2"
    nist: list[str] = field(default_factory=list)
    summary: str = ""
    description: str = ""
    llm_required: bool = False
    setup: Setup = field(default_factory=Setup)
    actions: list[Action] = field(default_factory=list)
    expected: list[Assertion] = field(default_factory=list)


# ── Scoring output ─────────────────────────────────────────────────────


@dataclass
class AssertionResult:
    assertion: Assertion
    passed: bool
    observed: Any = None
    note: Optional[str] = None


@dataclass
class ScenarioResult:
    scenario_id: str
    scenario_version: int
    category: str
    runner: str
    passed: bool
    assertion_results: list[AssertionResult] = field(default_factory=list)
    outcome: Optional[RunOutcome] = None
    wall_time_ms: float = 0.0
    nist_controls: list[str] = field(default_factory=list)
