"""Shared CRG SQLite access + node resolution.

Extracted from taint_inject.py so the taint and config-linkage passes resolve
findings to graph nodes through ONE implementation — two copies of span
containment would drift, and both passes feed the same triage output.

CRG identity facts these helpers encode (source-verified, CRG v2.3.8):
  * DB lives at ``<repo>/.code-review-graph/graph.db`` (incremental.get_db_path).
  * ``qualified_name`` = ``<absolute/posix/file/path>::<Symbol>``; File nodes
    are the bare path (graph.py schema + parser.normalize_file_path, #774).
  * ``nodes`` carries line_start/line_end, so a tool finding at ``file:line``
    resolves by span containment to the enclosing definition.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def norm_path(p: "str | Path") -> str:
    """Forward-slash spelling — matches parser.normalize_file_path."""
    return str(p).replace("\\", "/")


def data_dir(repo: Path) -> Path:
    return repo / ".code-review-graph"


def db_path(repo: Path) -> Path:
    return data_dir(repo) / "graph.db"


def connect(repo: Path, *, readonly: bool = False) -> sqlite3.Connection:
    db = db_path(repo)
    if not db.exists():
        raise SystemExit(
            f"error: no graph DB at {db} — run 'code-review-graph build' first")
    if readonly:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
    else:
        conn = sqlite3.connect(str(db), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def file_nodes(conn: sqlite3.Connection, repo: Path, file: str) -> list[sqlite3.Row]:
    """All nodes for ``file``, accepting a repo-relative or absolute spelling.

    Stored file_path is the ABSOLUTE forward-slash form; the trailing-suffix
    match is the fallback for drive-letter/case drift on Windows.
    """
    rel = norm_path(file)
    probe = rel if Path(rel).is_absolute() else norm_path((repo / rel).resolve())
    rows = conn.execute(
        "SELECT qualified_name, kind, line_start, line_end FROM nodes "
        "WHERE file_path = ?", (probe,)).fetchall()
    if rows:
        return list(rows)
    return list(conn.execute(
        "SELECT qualified_name, kind, line_start, line_end FROM nodes "
        "WHERE file_path LIKE ?", ("%/" + rel.lstrip("/"),)).fetchall())


def resolve_location(conn: sqlite3.Connection, repo: Path,
                     file: str, line: "int | None") -> "str | None":
    """``file(:line)`` -> qualified_name of the smallest enclosing node.

    Falls back to the File node when no definition encloses the line (or no
    line was given). Returns None when the file isn't in the graph at all.
    """
    rows = file_nodes(conn, repo, file)
    if not rows:
        return None
    files = [r for r in rows if r["kind"] == "File"]
    if line is None:
        return (files or rows)[0]["qualified_name"]

    # Reject a line past the end of the file. CRG records line_end on the File
    # node, so an out-of-range location is detectable — and must be, or a bogus
    # finding silently becomes a plausible-looking file-level edge instead of
    # being reported as unresolvable.
    for f in files:
        end = f["line_end"] or 0
        if end and line > end:
            return None

    enclosing = [
        r for r in rows
        if r["kind"] != "File"
        and (r["line_start"] or 0) <= line <= (r["line_end"] or 10 ** 9)
    ]
    if enclosing:
        enclosing.sort(key=lambda r: (r["line_end"] or 10 ** 9) - (r["line_start"] or 0))
        return enclosing[0]["qualified_name"]
    return files[0]["qualified_name"] if files else None


def resolve_symbol(conn: sqlite3.Connection, query: str) -> list[str]:
    """Symbol / qualified-name / path query -> matching qualified_names."""
    q = norm_path(query)
    exact = conn.execute(
        "SELECT qualified_name FROM nodes WHERE qualified_name = ?", (q,)).fetchall()
    if exact:
        return [r[0] for r in exact]
    by_name = conn.execute(
        "SELECT qualified_name FROM nodes WHERE name = ?", (query,)).fetchall()
    if by_name:
        return [r[0] for r in by_name]
    return [r[0] for r in conn.execute(
        "SELECT qualified_name FROM nodes WHERE qualified_name LIKE ?",
        ("%" + q,)).fetchall()]


def resolve_ref(conn: sqlite3.Connection, repo: Path, ref: dict) -> "str | None":
    """Resolve a findings ref: ``{"node": ...}`` or ``{"file": ..., "line": ...}``."""
    if "node" in ref:
        matches = resolve_symbol(conn, str(ref["node"]))
        return matches[0] if len(matches) == 1 else None
    if "file" in ref:
        line = ref.get("line")
        return resolve_location(conn, repo, str(ref["file"]),
                                int(line) if line is not None else None)
    return None
