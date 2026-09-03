#!/usr/bin/env python3
"""Config/schema linkage pass for code-review-graph (spec Req 2, case 6).

Answers the question a coding agent must ask before changing validation or
data-access logic: *what configuration and schema assumptions does this code
depend on?* Changing an env-var check can break a `.env`/compose/Terraform
contract; changing a query can break a DB schema that lives in a `.sql` file.

**Verified before building** (the spec's checklist item — "verify against
source whether such edges exist before building"): they do NOT. CRG v2.3.8
emits exactly six edge kinds — CALLS, TESTED_BY, CONTAINS, IMPORTS_FROM,
REFERENCES, INHERITS — and the parser has no env-var read detection at all.
SQL tables ARE parsed into real nodes (`schema.sql::users`), so the schema half
attaches to genuine anchors, exactly as the spec anticipated.

Two link kinds, both stored in a ``config_edges`` table (same architecture as
``taint_inject.py``: a table CRG doesn't know about survives build *and*
update, which reconcile only their own per-file rows):

  READS_CONFIG  code node --> env var, defined in a config file
  USES_SCHEMA   code node --> SQL table node (a real CRG node)

Asymmetric on purpose: the CODE side always resolves to a real graph node (so
triage can answer "which functions are affected"), while the CONFIG side
carries a real ``target_qualified`` only when CRG actually parses that format
(.sql/.tf/.yaml/.yml/.properties get nodes; .env and Dockerfile do not), and
always carries honest ``config_file``/``config_line``/``config_key`` columns.

Usage:
  python config_link.py scan   [--repo PATH] [--dry-run]
  python config_link.py query  (--symbol X | --file F) [--repo PATH] [--json]
  python config_link.py status | reapply | clear   [--repo PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crg_graphdb import (  # noqa: E402
    connect, data_dir, norm_path, resolve_location, resolve_symbol,
)

FINDINGS_NAME = "config-findings.json"
VALID_KINDS = ("READS_CONFIG", "USES_SCHEMA")

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS config_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- READS_CONFIG, USES_SCHEMA
    source_qualified TEXT NOT NULL,  -- always a real CRG node (the code side)
    target_qualified TEXT,           -- real CRG node when the format is parsed
    config_key TEXT NOT NULL,        -- env var name / table name
    code_file TEXT,
    code_line INTEGER,
    config_file TEXT,
    config_line INTEGER,
    detail TEXT DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_config_source ON config_edges(source_qualified);
CREATE INDEX IF NOT EXISTS idx_config_key ON config_edges(config_key);
"""

# --------------------------------------------------------------- scan patterns

# Env-var READS, per language family. Every pattern exposes the var via a
# group named var* (several alternations need distinct group names).
_ENV_READ_PATTERNS = (
    # Python: os.environ["X"] / os.environ.get("X") / os.getenv("X") / environ["X"]
    re.compile(r"""(?:os\.)?environ(?:\.get)?\s*[\[(]\s*['"](?P<var1>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
    re.compile(r"""os\.getenv\s*\(\s*['"](?P<var2>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
    # JS/TS: process.env.X / process.env["X"]
    re.compile(r"""process\.env\.(?P<var3>[A-Za-z_][A-Za-z0-9_]*)"""),
    re.compile(r"""process\.env\[\s*['"](?P<var4>[A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""),
    # Go: os.Getenv("X") / os.LookupEnv("X")
    re.compile(r"""os\.(?:Getenv|LookupEnv)\s*\(\s*"(?P<var5>[A-Za-z_][A-Za-z0-9_]*)"""),
    # Java / C#
    re.compile(r"""System\.getenv\s*\(\s*"(?P<var6>[A-Za-z_][A-Za-z0-9_]*)"""),
    re.compile(r"""Environment\.GetEnvironmentVariable\s*\(\s*"(?P<var7>[A-Za-z_][A-Za-z0-9_]*)"""),
    # Ruby: ENV["X"] / ENV.fetch("X")
    re.compile(r"""ENV(?:\.fetch)?\s*[\[(]\s*['"](?P<var8>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
    # PHP: getenv("X") / $_ENV['X']
    re.compile(r"""getenv\s*\(\s*['"](?P<var9>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
    re.compile(r"""\$_ENV\[\s*['"](?P<var10>[A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""),
)

_CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
                  ".rb", ".go", ".java", ".kt", ".cs", ".php", ".scala"}

# Env-var DEFINITION sites. (glob, line-regex with a `var` group).
_ENV_DEF_SOURCES = (
    # .env / .env.example / .env.local ...
    ("**/.env", r"^\s*(?:export\s+)?(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*="),
    ("**/.env.*", r"^\s*(?:export\s+)?(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*="),
    ("**/*.env", r"^\s*(?:export\s+)?(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*="),
    # Dockerfile: ENV X=v / ENV X v / ARG X
    ("**/Dockerfile", r"^\s*(?:ENV|ARG)\s+(?P<var>[A-Za-z_][A-Za-z0-9_]*)"),
    ("**/Dockerfile.*", r"^\s*(?:ENV|ARG)\s+(?P<var>[A-Za-z_][A-Za-z0-9_]*)"),
    # compose / k8s / CI yaml: `- X=v`, `X: v` under an env: block, `- name: X`
    ("**/docker-compose*.yml", r"^\s*-?\s*(?P<var>[A-Z][A-Z0-9_]{2,})\s*[:=]"),
    ("**/docker-compose*.yaml", r"^\s*-?\s*(?P<var>[A-Z][A-Z0-9_]{2,})\s*[:=]"),
    ("**/.github/workflows/*.yml", r"^\s*(?P<var>[A-Z][A-Z0-9_]{2,})\s*:"),
    ("**/.github/workflows/*.yaml", r"^\s*(?P<var>[A-Z][A-Z0-9_]{2,})\s*:"),
    ("**/*.yaml", r"^\s*-?\s*name:\s*(?P<var>[A-Z][A-Z0-9_]{2,})\s*$"),
    ("**/*.yml", r"^\s*-?\s*name:\s*(?P<var>[A-Z][A-Z0-9_]{2,})\s*$"),
    # Terraform: variable "x" {} — TF_VAR_x is the env-var convention
    ("**/*.tf", r'^\s*variable\s+"(?P<var>[A-Za-z_][A-Za-z0-9_]*)"'),
    ("**/*.tfvars", r"^\s*(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*="),
    # Java-style properties
    ("**/*.properties", r"^\s*(?P<var>[A-Za-z_][A-Za-z0-9_.]*)\s*="),
)

# SQL table references inside code (string literals or ORM table hints).
_TABLE_REF_PATTERNS = (
    re.compile(r"""\bFROM\s+["'`\[]?(?P<tbl1>[A-Za-z_][A-Za-z0-9_]*)""", re.I),
    re.compile(r"""\bJOIN\s+["'`\[]?(?P<tbl2>[A-Za-z_][A-Za-z0-9_]*)""", re.I),
    re.compile(r"""\bINSERT\s+INTO\s+["'`\[]?(?P<tbl3>[A-Za-z_][A-Za-z0-9_]*)""", re.I),
    re.compile(r"""\bUPDATE\s+["'`\[]?(?P<tbl4>[A-Za-z_][A-Za-z0-9_]*)\s+SET\b""", re.I),
    re.compile(r"""\bDELETE\s+FROM\s+["'`\[]?(?P<tbl5>[A-Za-z_][A-Za-z0-9_]*)""", re.I),
    re.compile(r"""__tablename__\s*=\s*['"](?P<tbl6>[A-Za-z_][A-Za-z0-9_]*)['"]"""),
)

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox",
              "dist", "build", ".code-review-graph", ".crg-cache", ".mypy_cache"}


def _skip(p: Path) -> bool:
    return any(part in _SKIP_DIRS for part in p.parts)


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _match_var(m: re.Match) -> "str | None":
    for name, val in m.groupdict().items():
        if val and (name.startswith("var") or name.startswith("tbl")):
            return val
    return None


def scan_env_reads(repo: Path) -> list[tuple[str, int, str]]:
    """[(rel_file, line, VAR)] for every env-var read in source files."""
    out: list[tuple[str, int, str]] = []
    for p in repo.rglob("*"):
        if not p.is_file() or p.suffix not in _CODE_SUFFIXES or _skip(p):
            continue
        rel = norm_path(p.relative_to(repo))
        for i, line in enumerate(_read(p).splitlines(), 1):
            for pat in _ENV_READ_PATTERNS:
                for m in pat.finditer(line):
                    var = _match_var(m)
                    if var:
                        out.append((rel, i, var))
    return out


def scan_env_definitions(repo: Path) -> dict[str, list[tuple[str, int]]]:
    """{VAR: [(rel_config_file, line)]} across .env/Docker/compose/CI/tf/properties."""
    defs: dict[str, list[tuple[str, int]]] = {}
    for glob, pattern in _ENV_DEF_SOURCES:
        rx = re.compile(pattern)
        for p in repo.glob(glob):
            if not p.is_file() or _skip(p):
                continue
            rel = norm_path(p.relative_to(repo))
            for i, line in enumerate(_read(p).splitlines(), 1):
                m = rx.match(line)
                if m and m.group("var"):
                    defs.setdefault(m.group("var"), []).append((rel, i))
    return defs


def sql_tables(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str, int]]]:
    """{table_name_lower: [(qualified_name, file_path, line_start)]}.

    Uses CRG's OWN parsed SQL nodes as the anchor set — the spec's "you have
    real anchors to attach to". Only non-File nodes from .sql files count.
    """
    tables: dict[str, list[tuple[str, str, int]]] = {}
    for r in conn.execute(
            "SELECT qualified_name, name, file_path, line_start FROM nodes "
            "WHERE kind != 'File' AND file_path LIKE '%.sql'"):
        tables.setdefault(str(r["name"]).lower(), []).append(
            (r["qualified_name"], r["file_path"], r["line_start"] or 1))
    return tables


def scan_table_refs(repo: Path, known: set[str]) -> list[tuple[str, int, str]]:
    """[(rel_file, line, table)] for code referencing a KNOWN schema table.

    Restricted to tables the graph actually knows about: matching every
    SQL-shaped word would flood the graph with edges to nothing.
    """
    out: list[tuple[str, int, str]] = []
    if not known:
        return out
    for p in repo.rglob("*"):
        if not p.is_file() or p.suffix not in _CODE_SUFFIXES or _skip(p):
            continue
        rel = norm_path(p.relative_to(repo))
        for i, line in enumerate(_read(p).splitlines(), 1):
            for pat in _TABLE_REF_PATTERNS:
                for m in pat.finditer(line):
                    tbl = _match_var(m)
                    if tbl and tbl.lower() in known:
                        out.append((rel, i, tbl.lower()))
    return out


# ------------------------------------------------------------------ findings

def build_findings(repo: Path, conn: sqlite3.Connection) -> dict:
    """Scan the repo and emit neutral findings (no DB writes)."""
    edges: list[dict] = []

    defs = scan_env_definitions(repo)
    for file, line, var in scan_env_reads(repo):
        for cfg_file, cfg_line in defs.get(var, []):
            edges.append({
                "kind": "READS_CONFIG",
                "code": {"file": file, "line": line},
                "config": {"file": cfg_file, "line": cfg_line},
                "key": var,
                "detail": f"env:{var}",
            })

    tables = sql_tables(conn)
    for file, line, tbl in scan_table_refs(repo, set(tables)):
        for qualified, cfg_file, cfg_line in tables[tbl]:
            edges.append({
                "kind": "USES_SCHEMA",
                "code": {"file": file, "line": line},
                "config": {"file": cfg_file, "line": cfg_line, "node": qualified},
                "key": tbl,
                "detail": f"table:{tbl}",
            })
    return {"edges": edges}


def apply_findings(repo: Path, findings: dict, *, store: bool = True) -> dict:
    """Resolve + inject findings. Owns config_edges (idempotent replace)."""
    conn = connect(repo)
    try:
        conn.executescript(_TABLE_SQL)
        conn.execute("DELETE FROM config_edges")
        applied = 0
        unresolved: list[dict] = []
        seen: set[tuple] = set()
        for f in findings.get("edges", []):
            kind = str(f.get("kind", ""))
            if kind not in VALID_KINDS:
                unresolved.append({**f, "reason": f"unknown kind {kind!r}"})
                continue
            code = f.get("code", {})
            src = resolve_location(conn, repo, str(code.get("file", "")),
                                   code.get("line"))
            if src is None:
                unresolved.append({**f, "reason": "code location not in graph"})
                continue
            cfg = f.get("config", {})
            tgt = cfg.get("node")  # real node only where CRG parses the format
            # The config FILE is part of identity: one var legitimately has
            # several definition sites (.env AND terraform AND compose), and
            # each is a separate contract the code depends on. Keying without
            # it silently dropped every site after the first — and for
            # READS_CONFIG target_qualified is always NULL, so the collapse
            # was total.
            key = (kind, src, tgt, str(f.get("key")),
                   cfg.get("file"), cfg.get("line"))
            if key in seen:
                continue
            seen.add(key)
            conn.execute(
                "INSERT INTO config_edges (kind, source_qualified, target_qualified, "
                "config_key, code_file, code_line, config_file, config_line, detail, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (kind, src, tgt, str(f.get("key", "")),
                 code.get("file"), code.get("line"),
                 cfg.get("file"), cfg.get("line"),
                 str(f.get("detail", "")), time.time()))
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


def scan(repo: Path, *, store: bool = True) -> dict:
    conn = connect(repo, readonly=True)
    try:
        findings = build_findings(repo, conn)
    finally:
        conn.close()
    report = apply_findings(repo, findings, store=store)
    report["scanned"] = len(findings["edges"])
    return report


def reapply(repo: Path) -> "dict | None":
    """Re-resolve stored findings after a rebuild (same contract as taint)."""
    path = data_dir(repo) / FINDINGS_NAME
    if not path.exists():
        return None
    findings = json.loads(path.read_text(encoding="utf-8-sig"))
    return apply_findings(repo, findings, store=False)


def config_rows(repo: Path) -> list[dict]:
    conn = connect(repo, readonly=True)
    try:
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM config_edges")]
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()


def dependencies_for(repo: Path, qualified_names: list[str]) -> list[dict]:
    """Config/schema dependencies of the given code nodes (triage helper)."""
    if not qualified_names:
        return []
    conn = connect(repo, readonly=True)
    try:
        ph = ",".join("?" * len(qualified_names))
        try:
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM config_edges WHERE source_qualified IN ({ph})",
                qualified_names)]
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()


# ----------------------------------------------------------------------- CLI

def _report(report: dict) -> int:
    scanned = report.get("scanned")
    if scanned is not None:
        print(f"scanned {scanned} candidate link(s)")
    print(f"applied {report['applied']} config edge(s)")
    if report["unresolved"]:
        print(f"{len(report['unresolved'])} finding(s) DID NOT resolve to graph nodes:")
        for u in report["unresolved"][:10]:
            print(f"  - {u.get('kind')} {u.get('key')}: {u.get('reason')} "
                  f"({u.get('code')})")
        if len(report["unresolved"]) > 10:
            print(f"  ... and {len(report['unresolved']) - 10} more")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="config_link")
    ap.add_argument("--repo", default=".")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scan")
    sc.add_argument("--dry-run", action="store_true",
                    help="print findings JSON without writing to the DB")

    sub.add_parser("reapply")
    sub.add_parser("status")
    sub.add_parser("clear")

    qp = sub.add_parser("query")
    qp.add_argument("--symbol")
    qp.add_argument("--file")
    qp.add_argument("--json", action="store_true", dest="as_json")

    args = ap.parse_args()
    repo = Path(args.repo).resolve()

    if args.cmd == "scan":
        if args.dry_run:
            conn = connect(repo, readonly=True)
            try:
                findings = build_findings(repo, conn)
            finally:
                conn.close()
            print(json.dumps(findings, indent=2))
            return 0
        return _report(scan(repo))

    if args.cmd == "reapply":
        report = reapply(repo)
        if report is None:
            print(f"nothing stored at {data_dir(repo) / FINDINGS_NAME}")
            return 0
        return _report(report)

    if args.cmd == "status":
        rows = config_rows(repo)
        by_kind: dict[str, int] = {}
        for r in rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        print(f"config_edges rows: {len(rows)}"
              + (f" ({', '.join(f'{k}={v}' for k, v in by_kind.items())})" if rows else ""))
        print(f"stored findings file: {(data_dir(repo) / FINDINGS_NAME).exists()}")
        return 0

    if args.cmd == "clear":
        conn = connect(repo)
        try:
            try:
                n = conn.execute("DELETE FROM config_edges").rowcount
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
                    (norm_path((repo / rel).resolve()), "%/" + rel.lstrip("/")))]
        finally:
            conn.close()
        if not names:
            print("no matching nodes")
            return 1
        rows = dependencies_for(repo, names)
        if args.as_json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print("no config/schema dependencies for the matched node(s)")
        else:
            for r in rows:
                where = f"{r['config_file']}:{r['config_line']}"
                print(f"  [{r['kind']}] {r['config_key']} <- {where}"
                      + (f"  (node {r['target_qualified']})"
                         if r["target_qualified"] else ""))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
