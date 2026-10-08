"""The REAL Claude Code hook, run for real, with the gateway really down.

This runner exists for one category: fail_mode_discipline. What happens when
ACP is unreachable is decided by the shipped plugin hook on the client
(bin/govern.mjs of claude-code-acp-plugin), not by the hosted gateway, so the
HTTP runner (acp_api) cannot exercise it and reports those scenarios N/A.
Here nothing about the outage is simulated:

  * the hook is the unmodified shipped file, executed with node on the
    same stdin JSON Claude Code sends;
  * HOME is a fresh temp dir per scenario, so the hook's lapse log and
    offline ledger are real files it wrote itself;
  * "unreachable" is a closed localhost port; "error_5xx" is a local
    server that really answers HTTP 500; "recovered" is a local server that
    answers the hook's wire format with an allow.

What is a stand-in, stated plainly: the gateway during recovery and in the
no-failure baseline is a local stub that always allows. These scenarios
measure the CLIENT's failure posture, not ACP's policy engine (the acp_api
runner measures that, live, against production).

How the declared fail_mode is exercised. The plugin has no per-tenant
fail_mode switch; its posture is chosen by the session's tier: attended
(interactive) sessions fail OPEN loudly, unattended sessions (headless
entrypoint, CI, bypassPermissions) fail CLOSED. The runner therefore runs a
`fail_open` scenario as an attended session and a `fail_closed` scenario as
an unattended one. That mapping is the runner's, it is disclosed in the
scorecard, and it means this category measures "does the plugin behave
correctly in the posture the tier selects", not "does a tenant setting
change it" (there is no such setting).

Audit evidence is whatever the hook itself wrote to its offline ledger. The
ledger rows carry no user identity (the client cannot know who the server
would have attributed the call to), and the runner does not add one, so an
assertion that needs the actor on an outage row fails honestly.

Environment:
  AGB_CC_PLUGIN   path to claude-code-acp-plugin/bin/govern.mjs
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from benchmark.runner import RunnerMetadata, StatefulRunner
from benchmark.types import (
    AuditEntry, DirectToolCall, GatewayFailure, ToolOutcome,
)

PLUGIN_REPO = "https://github.com/agentic-control-plane/claude-code-acp-plugin.git"
# The released hook this build of the benchmark measures by default: plugin
# 0.28.0 ("Release 0.28.0"). Pinned so a reproduction run measures a fixed,
# named artifact and not whatever main is today; override with AGB_PLUGIN_REF
# (any commit SHA, or a branch/tag name such as `main`) to measure another.
# The ref and the exact commit SHA that was executed are recorded in the
# result file and printed in the SCORECARD header.
PINNED_PLUGIN_REF = "cb043a8e81f0d8382dba9e45a4e9ed7ec4c8606c"


def _git(*args: str, cwd: Optional[Path] = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=180,
    ).stdout.strip()


def resolve_hook() -> tuple[Path, str, str]:
    """Return (path to govern.mjs, ref, commit sha) of the hook to execute.

    AGB_CC_PLUGIN (an explicit path to a govern.mjs) wins and is labelled as
    such. Otherwise the plugin is fetched, unmodified, at AGB_PLUGIN_REF
    (default: the pinned release) into a per-user cache and the commit that
    was actually checked out is reported.
    """
    explicit = os.environ.get("AGB_CC_PLUGIN", "").strip()
    if explicit:
        hook = Path(explicit)
        try:
            sha = _git("rev-parse", "HEAD", cwd=hook.parent)
        except Exception:  # noqa: BLE001 - not a git checkout
            sha = "unknown"
        return hook, "AGB_CC_PLUGIN", sha
    ref = os.environ.get("AGB_PLUGIN_REF", "").strip() or PINNED_PLUGIN_REF
    cache = Path(os.environ.get("AGB_CACHE_DIR", Path.home() / ".cache" / "agentgovbench"))
    dest = cache / "claude-code-acp-plugin"
    try:
        if not (dest / ".git").exists():
            dest.mkdir(parents=True, exist_ok=True)
            _git("init", "-q", cwd=dest)
        _git("fetch", "-q", "--depth", "1", PLUGIN_REPO, ref, cwd=dest)
        _git("checkout", "-q", "--force", "FETCH_HEAD", cwd=dest)
        sha = _git("rev-parse", "HEAD", cwd=dest)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"could not fetch the Claude Code hook at {ref} from {PLUGIN_REPO}: {e}. "
            "Check your network, or clone the plugin yourself and set "
            "AGB_CC_PLUGIN=<clone>/bin/govern.mjs."
        ) from e
    return dest / "bin" / "govern.mjs", ref, sha

# Unattended entrypoint: the plugin reads CLAUDE_CODE_ENTRYPOINT (`claude -p`).
UNATTENDED_ENTRYPOINT = "sdk-cli"
ATTENDED_ENTRYPOINT = "cli"


class _Stub(BaseHTTPRequestHandler):
    status = 200

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        body = b'{"decision":"allow"}' if self.server.mode == "ok" else b'{"error":"boom"}'
        self.send_response(200 if self.server.mode == "ok" else 500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST

    def log_message(self, *a):  # silence
        pass


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Runner(StatefulRunner):
    def __init__(self) -> None:
        super().__init__()
        self._hook: Path = Path("")
        self._hook_ref = ""
        self._hook_sha = ""
        self._server: Optional[ThreadingHTTPServer] = None
        self._home: Optional[str] = None
        self._down_until = 0.0
        self._down_mode = "unreachable"
        self._unattended = True
        self._saw_outage = False
        self._dead = f"http://127.0.0.1:{_closed_port()}"

    @property
    def metadata(self) -> RunnerMetadata:
        return RunnerMetadata(
            name="claude_code_hook",
            version="0.1.0",
            governance_source="product",
            product="Claude Code + ACP (real govern.mjs hook, real outage)",
            vendor="agenticcontrolplane.com",
            notes=(
                "Executes the unmodified shipped govern.mjs hook with node, "
                "HOME in a temp dir, and the gateway really down (closed port "
                "or a local server answering 500). Measures the client's fail "
                "posture only; recovery and baseline use a local always-allow "
                "stub as the gateway. fail_mode is exercised through the tier "
                "the plugin keys on: fail_open as an attended session, "
                "fail_closed as an unattended one. "
                f"[hook ref={self._hook_ref or 'unresolved'} sha={self._hook_sha or 'unresolved'}]"
            ),
            capabilities={"simulate_outage": True, "multi_tenant": False},
        )

    def preflight(self) -> None:
        if shutil.which("node") is None:
            raise RuntimeError("node not found on PATH; the hook is a Node script.")
        self._hook, self._hook_ref, self._hook_sha = resolve_hook()
        if not self._hook.exists():
            raise RuntimeError(f"hook not found at {self._hook} (AGB_CC_PLUGIN must point at bin/govern.mjs).")

    def setup(self, scenario) -> None:
        super().setup(scenario)
        self.teardown()
        self._home = tempfile.mkdtemp(prefix="agb-hook-home-")
        acp = Path(self._home, ".acp")
        acp.mkdir()
        # A dummy credential so the hook takes its governed path. It is never
        # sent anywhere real: the only endpoints are localhost.
        (acp / "credentials").write_text("gsk_agb_standin_not_a_real_key\n")
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        self._server.mode = "ok"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        modes = [t.policy.fail_mode for t in scenario.setup.tenants if getattr(t, "policy", None)]
        self._unattended = (modes[0] == "fail_closed") if modes else False
        self._down_until = 0.0
        self._saw_outage = False

    def teardown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._home:
            shutil.rmtree(self._home, ignore_errors=True)
            self._home = None

    def _base(self) -> str:
        if time.time() < self._down_until:
            if self._down_mode == "error_5xx":
                self._server.mode = "500"
                return f"http://127.0.0.1:{self._server.server_port}"
            return self._dead
        self._server.mode = "ok"
        return f"http://127.0.0.1:{self._server.server_port}"

    def execute_action(self, action) -> Optional[ToolOutcome]:
        if isinstance(action, GatewayFailure):
            self._down_until = time.time() + max(1, action.duration_seconds)
            self._down_mode = action.mode
            return None
        if not isinstance(action, DirectToolCall):
            self._errors.append(f"{type(action).__name__} not handled by claude_code_hook")
            return None
        down = time.time() < self._down_until
        base = self._base()
        # Plugin 0.28+ only accepts ACP hosts from ACP_*_BASE; its supported
        # test override is ~/.acp/dev_base_override. HOME is this scenario's
        # throwaway temp dir, so this points only this hook run at the local
        # stub / closed port. Older releases ignore the file and use the env.
        Path(self._home, ".acp", "dev_base_override").write_text(base + "\n")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": self._home,
            "ACP_GOVERN_BASE": base,
            "ACP_API_BASE": base,
            "ACP_SHADOW": "off",
            "CI": "",  # a CI runner would otherwise make every session unattended
            "CLAUDE_CODE_ENTRYPOINT": UNATTENDED_ENTRYPOINT if self._unattended else ATTENDED_ENTRYPOINT,
        }
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": f"agb-{id(self)}",
            "tool_name": action.tool,
            "tool_input": action.input,
            "permission_mode": "default",
            "cwd": self._home,
        }
        t0 = time.time()
        p = subprocess.run(
            ["node", str(self._hook)], input=json.dumps(payload), text=True,
            capture_output=True, env=env, timeout=30,
        )
        ms = (time.time() - t0) * 1000
        out = {}
        try:
            out = json.loads(p.stdout) if p.stdout.strip() else {}
        except json.JSONDecodeError:
            self._errors.append(f"hook stdout not JSON: {p.stdout[:120]!r}")
        hso = out.get("hookSpecificOutput") or {}
        denied = hso.get("permissionDecision") == "deny"
        reason = hso.get("permissionDecisionReason") or out.get("systemMessage")
        # The outage is OBSERVED from what the hook itself said and wrote,
        # never from the scenario's own declaration.
        said = json.dumps(out).lower()
        if "unreachable" in said:
            self._saw_outage = True
        # A crashed or silent hook is not an allow. Record a runner error and
        # emit no outcome, so an assertion expecting an allow cannot pass on it.
        if p.returncode != 0:  # a clean allow legitimately prints nothing
            self._errors.append(
                f"hook gave no decision (exit {p.returncode}) for {action.tool}: {p.stderr[:120]!r}"
            )
            return None
        outcome = ToolOutcome(
            tool=action.tool, input=action.input, as_user=action.as_user,
            as_tenant=action.as_tenant, allowed=not denied, reason=reason,
            agent_tier="background" if self._unattended else "interactive",
            agent_name=action.agent_name, latency_ms=ms, source="product",
        )
        self._tool_outcomes.append(outcome)
        return outcome

    def audit_log(self) -> list[AuditEntry]:
        """Rows the hook wrote to its own offline ledger. Not enriched."""
        rows: list[AuditEntry] = []
        if not self._home:
            return rows
        ledger = Path(self._home, ".acp", "ledger.jsonl")
        if not ledger.exists():
            return rows
        for line in ledger.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            ungoverned = r.get("source") == "ungoverned"
            rows.append(AuditEntry(
                timestamp=r.get("ts", ""), tenant=None, actor_uid=None,
                actor_email=None, tool=r.get("tool") or "",
                # An allow that ran without a policy decision is recorded as
                # a flag, not a plain allow: that is what the hook's own
                # `source: ungoverned` field says.
                decision="flag" if ungoverned else r.get("decision", "allow"),
                reason=r.get("reason"), extra={"ledger_source": r.get("source")},
                source="product",
            ))
        return rows

    def collect_outcome(self):
        out = super().collect_outcome()
        out.gateway_reachable = not self._saw_outage
        return out
