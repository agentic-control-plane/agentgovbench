#!/usr/bin/env bash
# Reproduce the published AgentGovBench score for ACP: all 48 scenarios.
#
#   export ACP_API_KEY=...        ACP_TENANT_SLUG=...      # benchmark workspace A
#   export ACP_API_KEY_B=...      ACP_TENANT_SLUG_B=...    # benchmark workspace B
#   ./scripts/reproduce.sh
#
# Get the four values from the ACP console: API Keys -> "Reproduce our
# AgentGovBench score" -> Create benchmark workspaces. Takes about 5 minutes.
#
# Three steps, nothing special-cased for ACP's own workspaces:
#   1. acp_api runner     live HTTP against the hosted gateway on workspaces A and B
#                         (42 scenarios, including the 6 cross-tenant ones)
#   2. claude_code_hook   the real, unmodified shipped Claude Code hook, run with
#                         the gateway really unreachable (the 6 fail-mode scenarios;
#                         recovery and baseline use a local always-allow stand-in);
#                         needs no account, only git + node
#   3. scorecard.py       merges both into "ACP X/48" + results/SCORECARD.md.
#                         Declined and unmeasured scenarios count against it.
#
# Optional: ACP_BASE_URL (your own deployment), AGB_PLUGIN_REF (measure a hook
# other than the pinned release; a commit SHA, tag or branch).
set -euo pipefail

cd "$(dirname "$0")/.."

missing=""
for v in ACP_API_KEY ACP_TENANT_SLUG ACP_API_KEY_B ACP_TENANT_SLUG_B; do
  if [ -z "${!v:-}" ]; then missing="$missing $v"; fi
done
if [ -n "$missing" ]; then
  echo "Missing:$missing" >&2
  echo "Create the benchmark workspaces in the ACP console (API Keys page) and export the four values it shows." >&2
  exit 2
fi

for tool in node git; do
  command -v "$tool" >/dev/null 2>&1 || { echo "$tool not found on PATH (need Python 3.10+, Node 18+, git)." >&2; exit 2; }
done

# First Python >= 3.10 that can actually create a venv with pip (some Homebrew
# builds, e.g. 3.14 with a broken pyexpat, import fine but cannot bootstrap pip).
PY=""
for cand in ${AGB_PYTHON:-} python3.13 python3.12 python3.11 python3.10 python3 python3.14; do
  command -v "$cand" >/dev/null 2>&1 || continue
  "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1 || continue
  probe="$(mktemp -d)"
  if "$cand" -m venv "$probe/v" >/dev/null 2>&1 && "$probe/v/bin/python" -m pip --version >/dev/null 2>&1; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "No usable Python 3.10+ found (one that can create a venv with pip). Install Python 3.10-3.13, or set AGB_PYTHON." >&2
  exit 2
fi
echo "Using $PY ($("$PY" --version 2>&1))"
node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)' \
  || { echo "Node 18 or newer is required (found $(node --version))." >&2; exit 2; }

echo "==> [0/3] Installing the benchmark into ./.venv-repro"
VENV="${AGB_VENV:-.venv-repro}"
if [ ! -x "$VENV/bin/python" ]; then "$PY" -m venv "$VENV"; fi
# shellcheck disable=SC1091
. "$VENV/bin/activate"
pip install --quiet --disable-pip-version-check -e .

mkdir -p results
rm -f results/acp-api.json results/hook.json

echo "==> [1/3] Live HTTP run against ACP (workspaces $ACP_TENANT_SLUG and $ACP_TENANT_SLUG_B)"
# A non-zero exit here only means some scenarios failed; the result file is what matters.
agentgovbench run --runner acp_api --out results/acp-api.json || true
if [ ! -s results/acp-api.json ]; then
  echo "No results from the acp_api run (see the RUN ABORTED message above: expired key, wrong slug or wrong workspace)." >&2
  exit 1
fi

echo "==> [2/3] Real Claude Code hook with ACP unreachable (fail-mode scenarios)"
agentgovbench run --runner claude_code_hook --category fail_mode_discipline --out results/hook.json || true
if [ ! -s results/hook.json ]; then
  echo "No results from the claude_code_hook run (need git + network to fetch the plugin, and node)." >&2
  exit 1
fi

echo "==> [3/3] Merging into one scorecard"
python scripts/scorecard.py results/acp-api.json results/hook.json --date "$(date +%F)"
echo
echo "Full table: results/SCORECARD.md"
