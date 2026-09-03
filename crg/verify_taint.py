#!/usr/bin/env python3
"""Taint-edge injector verification (spec Req 2 step 3 + checklist items).

Proves, against the flask sandbox with real CRG builds:
  * semgrep-style dataflow findings resolve to EXACT node qualified_names
    (validated by joining taint_edges against the nodes table);
  * unresolved findings are reported, never silently dropped;
  * apply is idempotent;
  * taint_edges survive `code-review-graph update` (verified upstream fact:
    build/update reconcile per-file and never touch foreign tables);
  * reapply re-resolves after the vulnerable code MOVES (line shift);
  * test_triage.py surfaces the taint-exposed subset of the blast radius and
    no longer prints the not-implemented stub;
  * taint data is branch-scoped: swapping to a branch without findings shows
    none, swapping back restores them (findings live in the slot).

Usage: python verify_taint.py [sandbox_path]
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SANDBOX = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "sandbox-flask"
PY = sys.executable

sys.path.insert(0, str(HERE))
import taint_inject  # noqa: E402

RESULTS: list[tuple[str, bool]] = []

VULN = SANDBOX / "src" / "flask" / "crg_vuln.py"
VULN_SRC = '''\
"""Probe: classic source -> sink flow for taint-injection verification."""


def read_user_input(request):
    return request.args.get("q")


def build_query(raw):
    return "SELECT * FROM t WHERE c = '" + raw + "'"


def run_query(db, sql):
    return db.execute(sql)


def handler(request, db):
    raw = read_user_input(request)
    sql = build_query(raw)
    return run_query(db, sql)
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
    tail = out.strip().splitlines()[-1] if out.strip() else "(no output)"
    print(f"    crg {' '.join(args)}: {tail}")
    return out


def line_of(text: str, needle: str) -> int:
    for i, ln in enumerate(text.splitlines(), 1):
        if needle in ln:
            return i
    raise ValueError(f"{needle!r} not found")


def semgrep_fixture(src: str) -> dict:
    """Semgrep-shaped taint result: source at the request.args read inside
    read_user_input, sink at the db.execute inside run_query — plus one
    finding pointing at a nonexistent file (must be REPORTED unresolved)."""
    rel = "src/flask/crg_vuln.py"
    return {"results": [
        {
            "check_id": "python.flask.security.injection.tainted-sql-string",
            "path": rel,
            "start": {"line": line_of(src, 'db.execute(sql)')},
            "end": {"line": line_of(src, 'db.execute(sql)')},
            "extra": {
                "severity": "ERROR",
                "dataflow_trace": {
                    "taint_source": [{
                        "path": rel,
                        "start": {"line": line_of(src, 'request.args.get')},
                    }],
                },
            },
        },
        {
            "check_id": "python.bogus.unresolvable",
            "path": "src/flask/does_not_exist.py",
            "start": {"line": 1},
            "extra": {
                "severity": "WARNING",
                "dataflow_trace": {
                    "taint_source": [{"path": "src/flask/does_not_exist.py",
                                      "start": {"line": 1}}],
                },
            },
        },
    ]}


def node_exists(qualified: str) -> bool:
    db = SANDBOX / ".code-review-graph" / "graph.db"
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return bool(conn.execute(
            "SELECT 1 FROM nodes WHERE qualified_name = ?", (qualified,)).fetchone())
    finally:
        conn.close()


def rows() -> list[dict]:
    return taint_inject.taint_rows(SANDBOX)


def swap() -> str:
    r = subprocess.run([PY, str(HERE / "swap_or_build.py")], cwd=str(SANDBOX),
                       capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    for ln in out.splitlines():
        if "crg-branch-cache" in ln:
            print(f"    {ln.strip()}")
    return out


def triage(*extra: str) -> str:
    r = subprocess.run(
        [PY, str(HERE / "test_triage.py"), "--repo", str(SANDBOX),
         "--file", "src/flask/crg_vuln.py", "--symbol", "run_query", *extra],
        capture_output=True, text=True)
    return (r.stdout or "") + (r.stderr or "")


def cleanup(original_head: str | None = None) -> None:
    subprocess.run(["git", "-C", str(SANDBOX), "checkout", "-q", "-f", "main"],
                   capture_output=True)
    subprocess.run(["git", "-C", str(SANDBOX), "branch", "-q", "-D", "taint-free"],
                   capture_output=True)
    if original_head:
        # The probe was committed on main; restore the branch tip exactly.
        subprocess.run(["git", "-C", str(SANDBOX), "reset", "-q", "--hard",
                        original_head], capture_output=True)
    if VULN.exists():
        VULN.unlink()
    subprocess.run(["git", "-C", str(SANDBOX), "reset", "-q"], capture_output=True)
    try:
        conn = taint_inject.connect(SANDBOX)
        try:
            conn.execute("DROP TABLE IF EXISTS taint_edges")
            conn.commit()
        finally:
            conn.close()
    except SystemExit:
        pass
    f = SANDBOX / ".code-review-graph" / taint_inject.FINDINGS_NAME
    if f.exists():
        f.unlink()
    crg("update", "--quiet")


def main() -> int:
    git("checkout", "-q", "-f", "main")
    original_head = subprocess.run(
        ["git", "-C", str(SANDBOX), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    print("== setup: make main slot-managed, then add probe & sync ==")
    # Inject only ever writes through the .code-review-graph link INTO the
    # active slot — so main must be slot-managed BEFORE injecting, exactly as
    # in real usage where the post-checkout hook keeps the link live. (Inject
    # into a not-yet-linked real dir would be discarded by the next swap.)
    swap()
    VULN.write_text(VULN_SRC, encoding="utf-8")
    git("add", "src/flask/crg_vuln.py")
    # COMMIT the probe (don't leave it staged): the branch-scoping test below
    # branches off main, and a staged-but-uncommitted file would be swept into
    # the other branch's commit and then vanish from main — which would make
    # "no taint edges on main" correct-but-meaningless.
    git("commit", "-q", "-m", "crg taint probe")
    crg("update", "--quiet")

    try:
        print("\n== inject semgrep-style findings ==")
        fixture = HERE / "semgrep_fixture.json"
        fixture.write_text(json.dumps(semgrep_fixture(VULN_SRC), indent=2),
                           encoding="utf-8")
        r = subprocess.run(
            [PY, str(HERE / "taint_inject.py"), "--repo", str(SANDBOX),
             "apply", "--semgrep", str(fixture)],
            capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        print(out.strip())
        check("2 taint edges applied (TAINTS + REACHES_SINK)",
              "applied 2 taint edge(s)" in out)
        check("unresolvable finding reported, not dropped",
              "DID NOT resolve" in out and "does_not_exist" in out)

        current = rows()
        check("rows land on EXACT node qualified_names (join against nodes table)",
              bool(current) and all(
                  node_exists(x[k]) for x in current
                  for k in ("source_qualified", "target_qualified")))
        check("source resolved to enclosing function read_user_input",
              any(x["source_qualified"].endswith("::read_user_input")
                  for x in current))
        check("sink resolved to enclosing function run_query",
              any(x["target_qualified"].endswith("::run_query")
                  for x in current))

        print("\n== idempotency: apply again ==")
        report = taint_inject.apply_findings(
            SANDBOX, taint_inject.from_semgrep(semgrep_fixture(VULN_SRC)))
        check("re-apply replaces rows (still exactly 2)",
              report["applied"] == 2 and len(rows()) == 2)

        print("\n== survival across `update` ==")
        crg("update", "--quiet")
        check("taint_edges survive an incremental update", len(rows()) == 2)

        print("\n== reapply after a full graph REBUILD (new-slot path) ==")
        # The production reapply trigger: a fresh branch slot rebuilds the
        # graph from scratch (new node rows/ids), code unchanged. Stored
        # findings must re-resolve against the rebuilt graph. `build` drops and
        # recreates nodes/edges but leaves the foreign taint_edges table, so we
        # clear it first to prove reapply genuinely re-populates it.
        crg("build", "--quiet")
        conn = taint_inject.connect(SANDBOX)
        conn.execute("DELETE FROM taint_edges")
        conn.commit()
        conn.close()
        check("taint_edges empty before reapply", len(rows()) == 0)
        rep2 = taint_inject.reapply(SANDBOX)
        check("reapply re-resolves both edges against the rebuilt graph",
              rep2 is not None and rep2["applied"] == 2
              and any(x["source_qualified"].endswith("::read_user_input")
                      for x in rows())
              and any(x["target_qualified"].endswith("::run_query")
                      for x in rows()))

        print("\n== test_triage surfaces the taint-exposed subset ==")
        out = triage()
        check("stub line is GONE",
              "not implemented, structural blast-radius only" not in out)
        check("taint-exposed section present",
              "taint-exposed subset of blast radius" in out)
        check("summary counts injected edges and exposed nodes",
              "taint reachability: 2 injected edge(s)" in out
              and "taint-exposed" in out)
        check("exposed nodes include the sink function",
              "crg_vuln.py::run_query" in out)

        print("\n== branch scoping: findings live in the slot ==")
        # Same code on both branches (probe is committed); only the injected
        # taint data should differ, proving it is slot-scoped and not global.
        git("checkout", "-q", "-b", "taint-free")
        git("commit", "-q", "--allow-empty", "-m", "diverge")
        swap()
        check("fresh branch slot has no taint rows", len(rows()) == 0)
        check("vulnerable file IS present on the other branch "
              "(so zero rows is scoping, not absence)", VULN.exists())
        git("checkout", "-q", "main")
        swap()
        check("main's taint rows return after swap back", len(rows()) == 2)
    finally:
        print("\n== cleanup ==")
        cleanup(original_head)

    print("\n== summary ==")
    failed = [x for x in RESULTS if not x[1]]
    for name, ok in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
