#!/usr/bin/env python3
"""Config/schema linkage verification (spec Req 2, case 6).

Proves against the flask sandbox with real CRG builds:
  * the spec's checklist precondition — CRG emits NO config/env edge of its
    own — is re-asserted here as a live assertion, not a one-off observation;
  * env-var reads link to the config files that DEFINE them, across multiple
    formats (.env, Dockerfile, compose, CI yaml, terraform);
  * a read of an UNDEFINED var produces no edge (no phantom links);
  * schema references link to CRG's OWN parsed .sql table nodes, including
    flask's pre-existing examples/tutorial/flaskr/schema.sql (not just probes);
  * the code side always resolves to a real graph node (join-verified);
  * scan is idempotent; rows survive `update`; reapply restores after rebuild;
  * triage surfaces the dependencies; data is branch-scoped.

Usage: python verify_config.py [sandbox_path]
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
import config_link  # noqa: E402

RESULTS: list[tuple[str, bool]] = []

PROBE = SANDBOX / "src" / "flask" / "crg_cfg_probe.py"
ENV_FILE = SANDBOX / ".env.example"
DOCKERFILE = SANDBOX / "Dockerfile"
TFFILE = SANDBOX / "infra.tf"

PROBE_SRC = '''\
"""Probe: env-var reads + SQL table references for config-linkage checks."""
import os


def load_settings():
    api_key = os.environ["API_KEY"]
    db_url = os.getenv("DATABASE_URL")
    region = os.environ.get("AWS_REGION")
    return api_key, db_url, region


def read_undefined():
    # Defined nowhere in the repo: must produce NO edge.
    return os.getenv("TOTALLY_UNDEFINED_VAR_XYZ")


def fetch_users(db):
    return db.execute("SELECT id, username FROM user WHERE id = ?")


def insert_post(db, title):
    return db.execute("INSERT INTO post (title) VALUES (?)", (title,))
'''

ENV_SRC = "API_KEY=changeme\nDATABASE_URL=sqlite:///app.db\nUNUSED_VAR=1\n"
DOCKERFILE_SRC = "FROM python:3.12\nENV AWS_REGION=eu-west-1\nARG BUILD_ID\n"
TF_SRC = 'variable "API_KEY" {\n  type = string\n}\n'


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
    print(f"    crg {' '.join(args)}: "
          + (out.strip().splitlines()[-1] if out.strip() else "(no output)"))
    return out


def swap() -> str:
    r = subprocess.run([PY, str(HERE / "swap_or_build.py")], cwd=str(SANDBOX),
                       capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    for ln in out.splitlines():
        if "crg-branch-cache" in ln:
            print(f"    {ln.strip()}")
    return out


def rows() -> list[dict]:
    return config_link.config_rows(SANDBOX)


def keys_of(kind: str) -> set[str]:
    return {r["config_key"] for r in rows() if r["kind"] == kind}


def node_exists(qualified: str) -> bool:
    db = SANDBOX / ".code-review-graph" / "graph.db"
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return bool(conn.execute(
            "SELECT 1 FROM nodes WHERE qualified_name = ?", (qualified,)).fetchone())
    finally:
        conn.close()


def crg_native_config_edges() -> int:
    """CRG's own edges linking code to a config/schema file (expect 0).

    Joins on the nodes table's real file_path rather than pattern-matching
    qualified_name: a naive ``target_qualified LIKE '%.env%'`` matches the
    SYMBOL ``werkzeug.test.EnvironBuilder`` and reports phantom config edges.
    """
    db = SANDBOX / ".code-review-graph" / "graph.db"
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute("""
            SELECT COUNT(*) FROM edges e
            JOIN nodes s ON s.qualified_name = e.source_qualified
            JOIN nodes t ON t.qualified_name = e.target_qualified
            WHERE s.file_path LIKE '%.py'
              AND (t.file_path LIKE '%.sql' OR t.file_path LIKE '%.tf'
                   OR t.file_path LIKE '%.env' OR t.file_path LIKE '%.env.%'
                   OR t.file_path LIKE '%Dockerfile%')
        """).fetchone()[0]
    finally:
        conn.close()


def triage() -> str:
    r = subprocess.run(
        [PY, str(HERE / "test_triage.py"), "--repo", str(SANDBOX),
         "--file", "src/flask/crg_cfg_probe.py", "--symbol", "load_settings"],
        capture_output=True, text=True)
    return (r.stdout or "") + (r.stderr or "")


def cleanup(original_head: str | None) -> None:
    subprocess.run(["git", "-C", str(SANDBOX), "checkout", "-q", "-f", "main"],
                   capture_output=True)
    subprocess.run(["git", "-C", str(SANDBOX), "branch", "-q", "-D", "cfg-free"],
                   capture_output=True)
    if original_head:
        subprocess.run(["git", "-C", str(SANDBOX), "reset", "-q", "--hard",
                        original_head], capture_output=True)
    for p in (PROBE, ENV_FILE, DOCKERFILE, TFFILE):
        if p.exists():
            p.unlink()
    subprocess.run(["git", "-C", str(SANDBOX), "reset", "-q"], capture_output=True)
    try:
        conn = config_link.connect(SANDBOX)
        try:
            conn.execute("DROP TABLE IF EXISTS config_edges")
            conn.commit()
        finally:
            conn.close()
    except SystemExit:
        pass
    f = SANDBOX / ".code-review-graph" / config_link.FINDINGS_NAME
    if f.exists():
        f.unlink()
    crg("update", "--quiet")


def main() -> int:
    git("checkout", "-q", "-f", "main")
    original_head = subprocess.run(
        ["git", "-C", str(SANDBOX), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()

    print("== setup: slot-managed main, add probe + config files, sync ==")
    swap()
    PROBE.write_text(PROBE_SRC, encoding="utf-8")
    ENV_FILE.write_text(ENV_SRC, encoding="utf-8")
    DOCKERFILE.write_text(DOCKERFILE_SRC, encoding="utf-8")
    TFFILE.write_text(TF_SRC, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "crg config probe")
    crg("update", "--quiet")

    try:
        print("\n== precondition: CRG emits no code->config edges of its own ==")
        check("CRG native code->config edge count is 0", crg_native_config_edges() == 0)

        print("\n== scan ==")
        r = subprocess.run(
            [PY, str(HERE / "config_link.py"), "--repo", str(SANDBOX), "scan"],
            capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        print(out.strip())
        check("scan applied edges", "applied" in out and " 0 config edge(s)" not in out)

        env_keys = keys_of("READS_CONFIG")
        check("API_KEY linked (.env.example + terraform definitions)",
              "API_KEY" in env_keys)
        check("DATABASE_URL linked (.env.example)", "DATABASE_URL" in env_keys)
        check("AWS_REGION linked (Dockerfile ENV)", "AWS_REGION" in env_keys)
        check("undefined var produces NO edge",
              "TOTALLY_UNDEFINED_VAR_XYZ" not in env_keys)
        check("env var defined but never read is NOT linked",
              "UNUSED_VAR" not in env_keys)

        api_rows = [r for r in rows() if r["config_key"] == "API_KEY"]
        check("API_KEY links to BOTH .env.example and infra.tf",
              {Path(r["config_file"]).name for r in api_rows}
              >= {".env.example", "infra.tf"})

        schema_keys = keys_of("USES_SCHEMA")
        check("SQL table 'user' linked from code to flask's own schema.sql",
              "user" in schema_keys)
        check("SQL table 'post' linked (INSERT INTO)", "post" in schema_keys)
        schema_rows = [r for r in rows() if r["kind"] == "USES_SCHEMA"]
        check("schema links point at flask's pre-existing schema.sql",
              any("examples/tutorial/flaskr/schema.sql" in (r["config_file"] or "")
                  for r in schema_rows))
        check("schema links carry a REAL CRG target node",
              bool(schema_rows) and all(
                  r["target_qualified"] and node_exists(r["target_qualified"])
                  for r in schema_rows))

        check("code side always resolves to a real graph node",
              bool(rows()) and all(node_exists(r["source_qualified"]) for r in rows()))
        check("code side resolves to the enclosing function, not just the file",
              any(r["source_qualified"].endswith("::load_settings") for r in rows()))
        check("env-var edges have no target node (.env/Dockerfile aren't parsed)",
              all(r["target_qualified"] is None
                  for r in rows() if r["kind"] == "READS_CONFIG"))

        print("\n== idempotency + survival ==")
        before = len(rows())
        config_link.scan(SANDBOX)
        check("re-scan is idempotent (same row count)", len(rows()) == before)
        crg("update", "--quiet")
        check("config_edges survive an incremental update", len(rows()) == before)

        print("\n== reapply after full rebuild ==")
        crg("build", "--quiet")
        conn = config_link.connect(SANDBOX)
        conn.execute("DELETE FROM config_edges")
        conn.commit()
        conn.close()
        check("config_edges empty before reapply", len(rows()) == 0)
        rep = config_link.reapply(SANDBOX)
        check("reapply restores edges against the rebuilt graph",
              rep is not None and rep["applied"] == before)

        print("\n== triage surfaces config dependencies ==")
        t = triage()
        check("config/schema section present in triage output",
              "config/schema dependencies of blast radius" in t)
        check("triage reports env-var and schema link counts",
              "env-var link(s)" in t and "schema link(s)" in t)
        check("triage names a linked config key",
              "API_KEY" in t or "DATABASE_URL" in t)

        print("\n== branch scoping ==")
        git("checkout", "-q", "-b", "cfg-free")
        git("commit", "-q", "--allow-empty", "-m", "diverge")
        swap()
        check("fresh branch slot has no config rows", len(rows()) == 0)
        check("probe files ARE present on the other branch "
              "(so zero rows is scoping, not absence)",
              PROBE.exists() and ENV_FILE.exists())
        git("checkout", "-q", "main")
        swap()
        check("main's config rows return after swap back", len(rows()) == before)
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
