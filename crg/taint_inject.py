#!/usr/bin/env python3
"""Taint-edge injector for code-review-graph (spec Requirement 2, step 3).

Maps source→sink findings from a taint analyzer (Semgrep JSON, or a neutral
findings format for CodeQL/SISA pipelines) onto CRG's node identifiers and
stores them in a dedicated ``taint_edges`` table inside CRG's own SQLite DB.

Why this shape (all source-verified against CRG v2.3.8):

* **Separate table, not rows in CRG's ``edges``**: build/update reconcile the
  ``edges`` table per file (``DELETE FROM edges WHERE file_path = ?``), so
  injected rows there would be silently wiped or double-counted. A table CRG
  doesn't know about survives ``build`` AND ``update`` untouched — full_build
  reconciles per-file rather than dropping the database.
* **Keyed on qualified_name strings** (``file/path::Symbol``, POSIX slashes,
  File nodes = bare path) — CRG's edges reference the same strings, so joins
  line up with no FK gymnastics.
* **Findings stored at ``<data-dir>/taint-findings.json``** — the data dir IS
  the branch slot under the per-branch cache, so taint data swaps with the
  branch for free, and ``reapply`` can re-resolve after code moves.
* **Node mapping is span containment**: nodes carry line_start/line_end, so a
  finding at file:line resolves to the smallest enclosing non-File node
  (fallback: the File node). Unresolved findings are REPORTED, never dropped
  silently.

Usage:
  python taint_inject.py apply   --semgrep semgrep.json   [--repo PATH]
  python taint_inject.py apply   --findings findings.json [--repo PATH]
  python taint_inject.py reapply [--repo PATH]
  python taint_inject.py query   (--symbol X | --file F)  [--repo PATH] [--json]
  python taint_inject.py status  [--repo PATH]
  python taint_inject.py clear   [--repo PATH]

Neutral findings format:
  {"edges": [{"kind": "TAINTS" | "REACHES_SINK",
              "source": {"file": "rel/or/abs", "line": N} | {"node": "qualified-or-name"},
              "sink":   {...same...},
              "detail": "provenance", "confidence": 0.0-1.0}]}
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crg_graphdb import (  # noqa: E402  — shared with config_link.py
    connect, data_dir, db_path, norm_path, resolve_location, resolve_symbol,
    resolve_ref as _resolve_ref,
)

FINDINGS_NAME = "taint-findings.json"
VALID_KINDS = ("TAINTS", "REACHES_SINK")

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS taint_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,             -- TAINTS, REACHES_SINK
    source_qualified TEXT NOT NULL,
    target_qualified TEXT NOT NULL,
    source_file TEXT,
    source_line INTEGER,
    sink_file TEXT,
    sink_line INTEGER,
    detail TEXT DEFAULT '',
    confidence REAL DEFAULT 1.0,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_taint_source ON taint_edges(source_qualified);
CREATE INDEX IF NOT EXISTS idx_taint_target ON taint_edges(target_qualified);
"""


# ------------------------------------------------------------------- adapters

def from_semgrep(semgrep_json: dict) -> dict:
    """Semgrep taint results (dataflow_trace) -> neutral findings.

    Plain pattern matches without a trace yield nothing — those are
    duplicate-search material (spec case 7), deliberately out-of-band.
    """
    edges: list[dict] = []
    for res in semgrep_json.get("results", []):
        trace = (res.get("extra") or {}).get("dataflow_trace")
        if not trace:
            continue
        src = _semgrep_loc(trace.get("taint_source"))
        sink_file, sink_line = res.get("path"), (res.get("start") or {}).get("line")
        if not (src and sink_file and sink_line):
            continue
        check = str(res.get("check_id", "semgrep"))
        sev = str(((res.get("extra") or {}).get("severity")) or "")
        conf = {"ERROR": 1.0, "WARNING": 0.8, "INFO": 0.5}.get(sev, 0.9)
        common = {"detail": f"semgrep:{check}", "confidence": conf}
        edges.append({"kind": "TAINTS",
                      "source": {"file": src[0], "line": src[1]},
                      "sink": {"file": sink_file, "line": sink_line}, **common})
        edges.append({"kind": "REACHES_SINK",
                      "source": {"file": src[0], "line": src[1]},
                      "sink": {"file": sink_file, "line": sink_line}, **common})
    return {"edges": edges}


def _semgrep_loc(obj) -> tuple[str, int] | None:
    """Handle semgrep's CliLoc variants: {path,start} dict, [location, content]
    pair, or a nested {"location": {...}} wrapper."""
    if obj is None:
        return None
    if isinstance(obj, list) and obj:
        obj = obj[0]
    if isinstance(obj, dict):
        path, start = obj.get("path"), obj.get("start") or {}
        line = start.get("line") if isinstance(start, dict) else None
        if path and line:
            return str(path), int(line)
        loc = obj.get("location")
        if isinstance(loc, dict):
            return _semgrep_loc(loc)
    return None


# ------------------------------------------------------------------- injection

def apply_findings(repo: Path, findings: dict, *, store: bool = True) -> dict:
    """Resolve + inject findings. Owns the whole taint_edges table (idempotent:
    previous rows are replaced). Returns {"applied", "unresolved": [...]}."""
    conn = connect(repo)
    try:
        conn.executescript(_TABLE_SQL)
        conn.execute("DELETE FROM taint_edges")
        applied = 0
        unresolved: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        for f in findings.get("edges", []):
            kind = str(f.get("kind", ""))
            if kind not in VALID_KINDS:
                unresolved.append({**f, "reason": f"unknown kind {kind!r}"})
                continue
            src_q = _resolve_ref(conn, repo, f.get("source", {}))
            sink_q = _resolve_ref(conn, repo, f.get("sink", {}))
            if src_q is None or sink_q is None:
                unresolved.append({
                    **f,
                    "reason": ("source unresolved" if src_q is None else "sink unresolved"),
                })
                continue
            key = (kind, src_q, sink_q)
            if key in seen:
                continue
            seen.add(key)
            conn.execute(
                "INSERT INTO taint_edges (kind, source_qualified, target_qualified, "
                "source_file, source_line, sink_file, sink_line, detail, confidence, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (kind, src_q, sink_q,
                 f.get("source", {}).get("file"), f.get("source", {}).get("line"),
                 f.get("sink", {}).get("file"), f.get("sink", {}).get("line"),
                 str(f.get("detail", "")), float(f.get("confidence", 1.0)),
                 time.time()))
            applied += 1
        conn.commit()
    finally:
        conn.close()
    if store:
        path = data_dir(repo) / FINDINGS_NAME
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(findings, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    return {"applied": applied, "unresolved": unresolved}


def reapply(repo: Path) -> dict | None:
    """Re-resolve stored findings (after code moved / a fresh slot build).
    Returns the apply report, or None when nothing is stored."""
    path = data_dir(repo) / FINDINGS_NAME
    if not path.exists():
        return None
    findings = json.loads(path.read_text(encoding="utf-8-sig"))
    return apply_findings(repo, findings, store=False)


def taint_rows(repo: Path) -> list[dict]:
    """All injected rows, for consumers (test_triage's exposed-subset filter)."""
    conn = connect(repo, readonly=True)
    try:
        try:
            rows = conn.execute("SELECT * FROM taint_edges").fetchall()
        except sqlite3.OperationalError:
            return []  # table doesn't exist: nothing injected yet
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ------------------------------------------------------------------------ CLI

def _report(report: dict) -> int:
    print(f"applied {report['applied']} taint edge(s)")
    if report["unresolved"]:
        print(f"{len(report['unresolved'])} finding(s) DID NOT resolve to graph nodes:")
        for u in report["unresolved"][:10]:
            print(f"  - {u.get('kind')}: {u.get('reason')} "
                  f"source={u.get('source')} sink={u.get('sink')}")
        if len(report["unresolved"]) > 10:
            print(f"  ... and {len(report['unresolved']) - 10} more")
    return 0 if report["applied"] or not report["unresolved"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="taint_inject")
    ap.add_argument("--repo", default=".", help="repo root (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    app = sub.add_parser("apply")
    app.add_argument("--semgrep", help="semgrep JSON output (taint mode)")
    app.add_argument("--findings", help="neutral findings JSON")

    sub.add_parser("reapply")
    sub.add_parser("status")
    sub.add_parser("clear")

    qp = sub.add_parser("query")
    qp.add_argument("--symbol")
    qp.add_argument("--file")
    qp.add_argument("--json", action="store_true", dest="as_json")

    args = ap.parse_args()
    repo = Path(args.repo).resolve()

    if args.cmd == "apply":
        if bool(args.semgrep) == bool(args.findings):
            sys.exit("error: pass exactly one of --semgrep or --findings")
        raw = json.loads(Path(args.semgrep or args.findings)
                         .read_text(encoding="utf-8-sig"))
        findings = from_semgrep(raw) if args.semgrep else raw
        return _report(apply_findings(repo, findings))

    if args.cmd == "reapply":
        report = reapply(repo)
        if report is None:
            print(f"nothing stored at {data_dir(repo) / FINDINGS_NAME}")
            return 0
        return _report(report)

    if args.cmd == "status":
        rows = taint_rows(repo)
        stored = (data_dir(repo) / FINDINGS_NAME).exists()
        print(f"taint_edges rows: {len(rows)}; stored findings file: {stored}")
        return 0

    if args.cmd == "clear":
        conn = connect(repo)
        try:
            try:
                n = conn.execute("DELETE FROM taint_edges").rowcount
                conn.commit()
            except sqlite3.OperationalError:
                n = 0
        finally:
            conn.close()
        f = data_dir(repo) / FINDINGS_NAME
        if f.exists():
            f.unlink()
        print(f"cleared {n} row(s) and stored findings")
        return 0

    if args.cmd == "query":
        if bool(args.symbol) == bool(args.file):
            sys.exit("error: pass exactly one of --symbol or --file")
        conn = connect(repo, readonly=True)
        try:
            if args.symbol:
                names = resolve_symbol(conn, args.symbol)
            else:
                rel = norm_path(args.file)
                names = [r[0] for r in conn.execute(
                    "SELECT qualified_name FROM nodes WHERE file_path = ? "
                    "OR file_path LIKE ?",
                    (norm_path((repo / rel).resolve()), "%/" + rel.lstrip("/"))
                ).fetchall()]
            if not names:
                print("no matching nodes")
                return 1
            placeholders = ",".join("?" * len(names))
            try:
                rows = [dict(r) for r in conn.execute(
                    f"SELECT * FROM taint_edges WHERE source_qualified IN ({placeholders}) "
                    f"OR target_qualified IN ({placeholders})",
                    (*names, *names)).fetchall()]
            except sqlite3.OperationalError:
                rows = []
        finally:
            conn.close()
        if args.as_json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print("no taint edges touch the matched node(s)")
        else:
            for r in rows:
                print(f"  [{r['kind']}] {r['source_qualified']} -> "
                      f"{r['target_qualified']} ({r['detail']}, conf {r['confidence']})")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
