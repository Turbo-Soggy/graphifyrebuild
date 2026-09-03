#!/usr/bin/env python3
"""Requirement 2 verification per claude-code-implementation-brief.md.

Drives test_triage.py (live MCP serve, stdio) through the brief's scenario
against the flask sandbox and prints PASS/FAIL per acceptance criterion:

* real JSON from all four triage tools;
* get_knowledge_gaps_tool flags an untested high-degree symbol, and the flag
  FLIPS OFF after a test covering it is added (the brief's flip check —
  upstream's hotspot bar is degree >= 5 with no TESTED_BY edge, so the probe
  module gives the symbol five same-file callers);
* --post-fix produces a visibly different, risk-scored detect_changes delta
  after a real edit;
* taint-reachability is explicitly stubbed in output;
* total round-trip stays within an interactive budget.

Usage: python verify_req2.py [sandbox_path]
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SANDBOX = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "sandbox-flask"
PY = sys.executable

RESULTS: list[tuple[str, bool]] = []

PROBE = SANDBOX / "src" / "flask" / "crg_probe.py"
PROBE_TEST = SANDBOX / "tests" / "test_crg_probe.py"

# Upstream's hotspot list is hard-capped at the TOP 20 BY DEGREE
# (analysis.find_knowledge_gaps -> untested_hotspots[:20]), and flask already
# has 20 untested hotspots of its own — so the probe needs enough callers to
# outrank them, not just clear the degree>=5 bar.
_N_CALLERS = 30
PROBE_SRC = (
    '"""Probe module for CRG verification: validate_token must become an\n'
    'untested hotspot (high degree, no TESTED_BY edge)."""\n\n\n'
    'def validate_token(token="x"):\n'
    '    return bool(token)\n\n\n'
    + "\n\n".join(
        f"def caller_{i:02d}():\n    return validate_token(\"t{i}\")"
        for i in range(_N_CALLERS)
    )
    + "\n"
)

PROBE_TEST_SRC = '''\
from flask.crg_probe import validate_token


def test_validate_token():
    assert validate_token("tok") is True
'''


def check(name: str, ok: bool) -> None:
    RESULTS.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}: {name}")


def git(*args: str) -> None:
    r = subprocess.run(["git", "-C", str(SANDBOX), *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args}: {r.stderr.strip()}")


def crg(*args: str) -> str:
    import os
    exe = Path(PY).parent / ("code-review-graph.exe" if os.name == "nt"
                             else "code-review-graph")
    r = subprocess.run([str(exe), *args], cwd=str(SANDBOX),
                       capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.strip().splitlines()[-1] if out.strip() else "(no output)")
    return out


def triage(*extra: str) -> str:
    r = subprocess.run(
        [PY, str(HERE / "test_triage.py"), "--repo", str(SANDBOX),
         "--file", "src/flask/crg_probe.py", "--symbol", "validate_token",
         *extra],
        capture_output=True, text=True)
    return (r.stdout or "") + (r.stderr or "")


def cleanup() -> None:
    for p in (PROBE, PROBE_TEST):
        if p.exists():
            p.unlink()
    subprocess.run(["git", "-C", str(SANDBOX), "reset", "-q"], capture_output=True)
    # Purge probe nodes from the graph so the sandbox ends clean.
    crg("update", "--quiet")


def main() -> int:
    git("checkout", "-q", "-f", "main")
    print("== sync graph to main ==")
    crg("update", "--quiet")

    try:
        print("\n== add untested hotspot probe (validate_token, 5 callers) ==")
        PROBE.write_text(PROBE_SRC, encoding="utf-8")
        git("add", "src/flask/crg_probe.py")
        crg("update")

        print("\n== triage run 1: validate_token should be flagged untested ==")
        t0 = time.time()
        out1 = triage()
        t1 = time.time() - t0
        check("all four tools returned sections",
              all(m in out1 for m in ("get_impact_radius_tool (blast radius)",
                                      "query_graph_tool (callers_of",
                                      "get_knowledge_gaps_tool",
                                      "get_review_context_tool")))
        check("blast radius contains real probe nodes",
              "crg_probe" in out1 and '"status": "ok"' in out1)
        check("callers_of returned the probe callers",
              all(c in out1 for c in ("caller_00", f"caller_{_N_CALLERS - 1:02d}")))
        check("validate_token flagged as untested hotspot",
              '"symbol_flagged_untested": true' in out1)
        # The injector now exists (taint_inject.py), so triage reports the
        # real taint state instead of the old not-implemented stub. With no
        # findings injected for this probe, it must SAY so rather than omit
        # the line — the brief's "never silently missing" requirement.
        check("taint state reported explicitly (no findings injected here)",
              "taint reachability: no findings injected" in out1)
        m = re.search(r"total round-trip .*: ([\d.]+)s", out1)
        rt = float(m.group(1)) if m else 999.0
        check(f"round-trip within interactive budget ({rt:.1f}s < 30s)", rt < 30)

        print("\n== add covering test, update, re-run: flag must flip off ==")
        PROBE_TEST.write_text(PROBE_TEST_SRC, encoding="utf-8")
        git("add", "tests/test_crg_probe.py")
        crg("update")
        out2 = triage()
        check("validate_token NO LONGER flagged after covering test added",
              '"symbol_flagged_untested": false' in out2)

        print("\n== post-fix: edit validate_token, detect_changes delta ==")
        PROBE.write_text(PROBE_SRC.replace(
            'return bool(token)',
            'if token is None:\n        raise ValueError("token required")\n'
            '    return bool(token)'), encoding="utf-8")
        out3 = triage("--post-fix")
        check("post-fix run used detect_changes_tool",
              "detect_changes_tool (post-fix risk-scored delta)" in out3)
        check("delta mentions the edited probe file", "crg_probe" in out3)
        check("delta is risk-scored",
              any(k in out3 for k in ('"risk', '"priority', 'risk_score')))
        check("post-fix output visibly differs from pre-fix triage output",
              out3 != out1 and "get_impact_radius_tool (blast radius)" not in out3)
    finally:
        print("\n== cleanup ==")
        cleanup()

    print("\n== summary ==")
    failed = [r for r in RESULTS if not r[1]]
    for name, ok in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    print(f"(triage run 1 wall time incl. server spawn: {t1:.1f}s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
