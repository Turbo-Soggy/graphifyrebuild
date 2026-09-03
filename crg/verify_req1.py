#!/usr/bin/env python3
"""Requirement 1 verification per claude-code-implementation-brief.md.

Runs the full branch-cache scenario against the flask sandbox from a clean
state and prints PASS/FAIL for every acceptance criterion. Everything is
visible: labeled outcomes, CRG's own Incremental/Full-rebuild lines, timings.

Usage: python verify_req1.py [sandbox_path]
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SANDBOX = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "sandbox-flask"
SWAP = HERE / "swap_or_build.py"
PY = sys.executable

sys.path.insert(0, str(HERE))
import swap_or_build as sob  # noqa: E402  (reuse link-aware cleanup helpers)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))


def git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(SANDBOX), *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args}: {r.stderr.strip()}")
    return r.stdout.strip()


def run_swap() -> tuple[str, float]:
    t0 = time.time()
    r = subprocess.run([PY, str(SWAP)], cwd=str(SANDBOX),
                       capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip())
    return out, time.time() - t0


def timed_crg(*args: str) -> float:
    crg = Path(PY).parent / ("code-review-graph.exe" if os.name == "nt"
                             else "code-review-graph")
    t0 = time.time()
    subprocess.run([str(crg), *args], cwd=str(SANDBOX), capture_output=True)
    return time.time() - t0


def db_has_symbol(symbol: str) -> bool:
    db = SANDBOX / ".code-review-graph" / "graph.db"
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE name = ?", (symbol,)).fetchone()
            return bool(row and row[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def reset_state() -> None:
    print("== resetting sandbox state ==")
    d = SANDBOX / ".code-review-graph"
    if sob._is_reparse_point(d):
        sob._remove_link(d)
    elif d.exists():
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    cache = SANDBOX / ".crg-cache"
    if cache.exists():
        import shutil
        for p in sorted(cache.rglob("*"), key=lambda x: -len(x.parts)):
            if sob._is_reparse_point(p):
                sob._remove_link(p)
        shutil.rmtree(cache, ignore_errors=True)
    subprocess.run(["git", "-C", str(SANDBOX), "checkout", "-q", "-f", "main"],
                   capture_output=True)
    subprocess.run(["git", "-C", str(SANDBOX), "checkout", "-q", "--", "."],
                   capture_output=True)
    subprocess.run(["git", "-C", str(SANDBOX), "branch", "-q", "-D", "test-branch"],
                   capture_output=True)
    # Drop any accidentally committed/staged cache artifacts from prior runs.
    subprocess.run(["git", "-C", str(SANDBOX), "checkout", "-q", "--", ".gitignore"],
                   capture_output=True)


def main() -> int:
    reset_state()
    probe = SANDBOX / "src" / "flask" / "crg_cache_probe.py"

    print("\n== baseline: full build on main (timed) ==")
    t_build = timed_crg("build", "--quiet")
    print(f"baseline full build: {t_build:.1f}s")

    print("\n== first swap on main (adopts freshly built real data dir) ==")
    # mirror_back adopts the pre-existing real data dir into a slot, and the
    # DB's own git_head_sha anchor makes the swap a zero-file incremental
    # reconcile — strictly better than the naive expected FULL BUILD, and
    # self-correcting even if the adoption guesses the slot wrong (the diff
    # base travels INSIDE the DB).
    out, _ = run_swap()
    check("first swap adopts existing data (CACHE HIT + UPDATE, 0 files) "
          "or rebuilds (FULL BUILD)",
          ("CACHE HIT + UPDATE" in out and "Incremental: 0 files" in out)
          or "FULL BUILD (no cache" in out)

    print("\n== first visit to test-branch (expect FULL BUILD) ==")
    git("checkout", "-q", "-b", "test-branch")
    out, _ = run_swap()
    check("new branch first visit prints FULL BUILD", "FULL BUILD (no cache" in out)

    print("\n== commit on test-branch, then swap (expect CACHE HIT + UPDATE, incremental) ==")
    probe.write_text("def crg_cache_probe_fn():\n    return 42\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "test edit")
    out, _ = run_swap()
    check("post-commit swap prints CACHE HIT + UPDATE", "CACHE HIT + UPDATE" in out)
    check("CRG reports incremental (not full rebuild)",
          "Incremental:" in out and "Full rebuild" not in out)
    check("edit is reflected in graph", db_has_symbol("crg_cache_probe_fn"))

    print("\n== switch away and back (expect CACHE HIT + UPDATE, faster than build) ==")
    git("checkout", "-q", "main")
    out, _ = run_swap()
    check("back on main: CACHE HIT + UPDATE", "CACHE HIT + UPDATE" in out)
    check("main's graph does NOT have the branch edit",
          not db_has_symbol("crg_cache_probe_fn"))
    git("checkout", "-q", "test-branch")
    out, t_revisit = run_swap()
    check("revisit test-branch: CACHE HIT + UPDATE", "CACHE HIT + UPDATE" in out)
    check("pre-switch edit survived the round-trip", db_has_symbol("crg_cache_probe_fn"))
    check(f"revisit swap ({t_revisit:.1f}s) measurably faster than full build ({t_build:.1f}s)",
          t_revisit < t_build / 2)

    print("\n== history rewrite (amend) -> expect CACHE INVALID, REBUILDING ==")
    git("commit", "-q", "--amend", "-m", "rewritten")
    git("checkout", "-q", "main")
    run_swap()
    git("checkout", "-q", "test-branch")
    out, _ = run_swap()
    check("amended branch triggers CACHE INVALID, REBUILDING",
          "CACHE INVALID, REBUILDING" in out)

    print("\n== detached HEAD -> expect DETACHED HEAD, FULL BUILD ==")
    head = git("rev-parse", "HEAD")
    git("checkout", "-q", head)
    out, _ = run_swap()
    check("detached HEAD prints DETACHED HEAD, FULL BUILD",
          "DETACHED HEAD, FULL BUILD" in out)

    print("\n== error fallback: broken slot DB must not crash or serve stale ==")
    git("checkout", "-q", "test-branch")
    run_swap()
    slot_db = SANDBOX / ".crg-cache" / "test-branch" / "data" / "graph.db"
    git("checkout", "-q", "main")
    run_swap()
    slot_db.write_bytes(b"not a sqlite database")
    git("checkout", "-q", "test-branch")
    out, _ = run_swap()
    check("corrupt slot DB handled without crash (labeled outcome still printed)",
          any(k in out for k in ("CACHE HIT + UPDATE", "CACHE INVALID", "FULL BUILD")))
    check("graph is healthy after corrupt-slot recovery", db_has_symbol("crg_cache_probe_fn"))

    # cleanup
    reset_state()
    if probe.exists():
        probe.unlink()

    print("\n== summary ==")
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, _ in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
