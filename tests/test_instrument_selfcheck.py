"""The instrument must be able to detect its own failure.

Three ways a pi-substrate run can be broken produce outcomes that are
indistinguishable from a real product verdict unless the executor checks
for them:

  * the extension fails to load  -> no decision point, everything executes,
    reads as a permissive product
  * the extension throws         -> everything blocked, reads as a paranoid
    product
  * the extension blocks         -> without the reason, a crash and a policy
    denial are the same observation

All three were live at once, and the reason map was dead code: declared,
read, never written. A competitor engine was scored 1/6 on the first of
them. These tests exist because the previous self-check was asserted in a
docstring and never ran.
"""
from __future__ import annotations

import json
import subprocess
import textwrap

import pytest

from runners.pi_base import DRIVER_DIR, EXECUTOR, _node_bin


def _run(tmp_path, extension_src: str, calls: list[dict]) -> dict:
    ext = tmp_path / "ext.ts"
    ext.write_text(textwrap.dedent(extension_src))
    spec = {"extensions": [str(ext)], "calls": calls, "env": {}}
    try:
        node = _node_bin()
    except Exception:  # pragma: no cover - environment without node
        pytest.skip("node runtime unavailable")
    proc = subprocess.run(
        [node, str(EXECUTOR)], input=json.dumps(spec),
        capture_output=True, text=True, timeout=180, cwd=str(DRIVER_DIR),
    )
    if proc.returncode != 0:
        pytest.fail(f"executor exited {proc.returncode}: {proc.stderr[-400:]}")
    return json.loads(proc.stdout)


CALL = [{"id": "c1", "tool": "read_file", "args": {"path": "/a"}}]


def test_unloadable_extension_is_fatal_not_a_permissive_verdict(tmp_path):
    """An extension that cannot import must not read as 'product allowed it'."""
    out = _run(tmp_path, '''
        import { nope } from "@totally/not-a-real-package";
        export default function () { return { name: "broken" }; }
    ''', CALL)
    assert out["fatal"], (
        "an extension that failed to load produced no instrument failure — "
        "every call executed and would be scored as permitted by the product"
    )
    assert "tool_call handler" in " ".join(out["fatal"])


def test_throwing_extension_is_fatal_not_a_strict_verdict(tmp_path):
    """A crashing extension blocks everything; that is not a policy decision."""
    out = _run(tmp_path, '''
        export default function (pi: any) {
          pi.on("tool_call", () => { throw new Error("engine unavailable"); });
          return { name: "throwing" };
        }
    ''', CALL)
    assert out["fatal"], "a throwing extension was scored as a product verdict"
    assert out["outcomes"][0]["allowed"] is False
    assert "EXTENSION THREW" in (out["outcomes"][0]["reason"] or ""), (
        "a crash and a policy denial must not be the same observation"
    )


def test_block_reason_is_captured_from_the_subject(tmp_path):
    """The reason must come from the product, and only for blocked calls."""
    out = _run(tmp_path, '''
        export default function (pi: any) {
          pi.on("tool_call", (e: any) =>
            e.toolName === "write_file"
              ? { block: true, reason: "denied by test policy" }
              : undefined);
          return { name: "blocking" };
        }
    ''', [
        {"id": "c1", "tool": "read_file", "args": {}},
        {"id": "c2", "tool": "write_file", "args": {}},
    ])
    assert not out["fatal"]
    by_id = {o["id"]: o for o in out["outcomes"]}
    assert by_id["c1"]["allowed"] is True
    assert by_id["c1"]["reason"] is None
    assert by_id["c2"]["allowed"] is False
    assert by_id["c2"]["reason"] == "denied by test policy", (
        "block reasons are dead code again — every subject will report null"
    )


def test_healthy_extension_records_no_instrument_failure(tmp_path):
    """The self-check must not fire on a working integration."""
    out = _run(tmp_path, '''
        export default function (pi: any) {
          pi.on("tool_call", () => undefined);
          return { name: "permissive" };
        }
    ''', CALL)
    assert out["fatal"] == []
    assert out["outcomes"][0]["allowed"] is True
