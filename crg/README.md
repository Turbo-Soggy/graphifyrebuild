# CRG Customization — Per-Branch Caching + AppSec Triage Context

Implements `C:\projects\code-review-graph-customization-spec.md` against
[tirth8205/code-review-graph](https://github.com/tirth8205/code-review-graph)
(CRG v2.3.8, cloned read-only in `crg-upstream/`), verified locally per
`C:\projects\claude-code-implementation-brief.md` against a real sandbox
(`sandbox-flask/`, 94 files, 1710 nodes, 8359 edges).

**This supersedes the graphify-based implementation** (`../graphify_ext/`),
kept as the record of what was originally scoped.

## Files

| file | role |
|---|---|
| `swap_or_build.py` | Requirement 1: per-branch cache swap + `install-hook` (plain git post-checkout / optional post-commit) |
| `crg_graphdb.py` | shared CRG DB access + node resolution (span containment), used by both injectors |
| `taint_inject.py` | Requirement 2 step 3: taint-edge injector (Semgrep/neutral findings → `taint_edges` table) |
| `config_link.py` | Requirement 2 case 6: config/schema linkage pass (env vars + SQL tables → `config_edges` table) |
| `test_triage.py` | Requirement 2: live-MCP triage smoke test (4 tools, raw JSON, taint-exposed subset, config deps, `--post-fix`) |
| `verify_req1.py` | brief's Req-1 scenario, PASS/FAIL per acceptance criterion — **14/14** |
| `verify_req2.py` | brief's Req-2 scenario, PASS/FAIL per acceptance criterion — **11/11** |
| `verify_taint.py` | taint-injector scenario, PASS/FAIL per claim — **16/16** |
| `verify_config.py` | config-linkage scenario, PASS/FAIL per claim — **25/25** |

## Requirement 1 — per-branch caching

```
.crg-cache/<branch-slot>/data/     # the whole .code-review-graph data dir per branch
.crg-cache/<branch-slot>/manifest.json
.code-review-graph                 # link (symlink/junction; copy-mode fallback) to active slot's data/
```

```bash
python swap_or_build.py               # swap/build for current branch (4 labeled outcomes)
python swap_or_build.py install-hook  # .git/hooks/post-checkout -> backgrounded swap, logs to .crg-cache/hook.log
```

Measured on the flask sandbox: full `build` ≈ 10.6–13.5s; branch-revisit swap
(CACHE HIT + UPDATE) ≈ 2–3s; no-op `update` ≈ 1.4s (matches the spec's
startup-cost claim).

### Source-verified facts (the spec's "confirm before implementing" items)

1. **DB path**: `<repo>/.code-review-graph/graph.db` (`incremental.get_db_path`;
   resolution order registry `--data-dir` → `CRG_DATA_DIR` → default). The
   script manages the default location and **refuses loudly** when
   `CRG_DATA_DIR` is set rather than swapping a directory CRG isn't reading.
2. **WAL mode** ⇒ the link targets the whole **data directory**, not the bare
   `graph.db` the spec sketched — SQLite's `-wal`/`-shm` side-files then land
   in the slot with no special-casing, and locking is normal single-file
   locking (all handles resolve through the link to the same real file).
   *Operational caveat:* a process holding an open handle across a swap
   (daemon, `serve`) keeps the old slot's file until it reopens — stop or
   restart long-lived readers around a swap.
3. **CRG has no git hooks.** Its `hooks/` directory is Claude Code *agent*
   hooks (SessionStart/PostToolUse). `install-hook` writes a plain
   `.git/hooks/post-checkout` (skips file checkouts via `$3 != 1` and no-op
   `checkout -b` via `$1 == $2`; `CRG_CACHE_SKIP=1` opt-out; backgrounded so
   checkout never blocks).
4. **The spec's "CRG's DB has no git-history anchor" is wrong** — and the
   truth simplifies everything: the DB `metadata` table stores `git_head_sha`
   at every build/update, and CRG's `resolve_incremental_base` diffs
   stored-SHA → working tree, explicitly designed for "multi-commit pull,
   rebase, or branch switch", falling back to a full rebuild itself when the
   anchor is unusable. Consequences used here:
   - `cache_is_trustworthy` reads the slot DB's own `git_head_sha` first
     (sidecar `manifest.json` is fallback + carries the CRG-version stamp);
   - slot contents are **self-correcting**: even a slot seeded with another
     branch's data converges on the next `update`, because the diff base
     travels *inside* the DB;
   - adoption of a pre-existing real data dir becomes a 0-file incremental
     reconcile instead of a full rebuild (verified: "first swap" check).
5. **Node-ID scheme** (for the future taint mapper): `qualified_name` =
   `<file_path>::<symbol>` with POSIX slashes; File nodes are the bare path;
   edges reference `source_qualified`/`target_qualified` strings (not FK ids)
   with `confidence`/`confidence_tier` columns.

### Deviations from the spec (each for a verified reason)

- Whole-dir link instead of single-file symlink (WAL side-files, fact 2).
- Ignore entries go to `.git/info/exclude`, not `.gitignore` — editing the
  *tracked* `.gitignore` from a hook dirties the worktree and git then blocks
  the very branch switch the hook serves (hit in verification). The bare
  `.code-review-graph` (no trailing slash) entry matters: slash patterns
  don't match symlinks.
- Update-failure fallback clears the slot data before the full rebuild, so a
  corrupt slot DB recovers instead of failing twice (verified with a
  deliberately corrupted slot).
- Link creation is verified *functionally* (mkdir through it) with copy-mode
  fallback — on this machine a filter driver breaks junction traversal under
  the user profile while junctions under `C:\projects` work.

### Verification (run `python verify_req1.py`) — 14/14

- Four labeled outcomes print exactly as the brief specifies: `FULL BUILD`,
  `CACHE HIT + UPDATE`, `CACHE INVALID, REBUILDING`, `DETACHED HEAD, FULL BUILD`
- Branch revisit reconciles via `update` (CRG prints `Incremental: N files`),
  reflects the pre-switch edit, 2.9s vs 10.6s full build
- Amend/force-push → `CACHE INVALID, REBUILDING`; detached HEAD → full build
- No silent failures: corrupt slot DB → logged fallback, healthy graph after
- Live `.git/hooks/post-checkout` fires on a real branch switch (hook.log)

## Requirement 2 — appsec triage context

```bash
python test_triage.py --file src/flask/app.py --symbol Flask
python test_triage.py --file src/flask/app.py --symbol Flask --post-fix
```

Spawns a live `code-review-graph serve` restricted via
`--tools query_graph_tool,get_impact_radius_tool,get_review_context_tool,get_knowledge_gaps_tool,detect_changes_tool`
(stdio transport, MCP SDK client) and prints each tool's **raw JSON**. Output
always states: `taint reachability: not implemented, structural blast-radius
only` (the one fully-custom piece, deliberately stubbed this pass per the brief).

### Verification (run `python verify_req2.py`) — 11/11

- Real JSON from all four tools against a live server (spawn + 4 calls ≈ 4.6s;
  individual calls 0.15–0.30s — comfortably interactive)
- Untested-hotspot flag **flips**: probe symbol flagged with no test, unflagged
  after a covering test + `update` (upstream bar: degree ≥ 5 with no TESTED_BY
  edge, **and** top-20-by-degree — flask has 20 competing hotspots, so the
  probe uses 30 callers; the hotspot list nests under `gaps.untested_hotspots`)
- `--post-fix` after a real edit returns a risk-scored `detect_changes_tool`
  delta naming the edited file
- Taint stub line present in every run

## Taint-edge injector (spec Req 2, step 3) — the one fully-custom piece

```bash
python taint_inject.py apply --semgrep semgrep.json   # semgrep taint mode
python taint_inject.py apply --findings findings.json # CodeQL/SISA via neutral format
python taint_inject.py query --symbol run_query       # what taint touches this symbol
python taint_inject.py status | reapply | clear
```

Neutral findings format (what any analyzer adapter must emit):

```json
{"edges": [{"kind": "TAINTS" | "REACHES_SINK",
            "source": {"file": "src/x.py", "line": 12},
            "sink":   {"file": "src/x.py", "line": 40},
            "detail": "semgrep:rule-id", "confidence": 1.0}]}
```

### Design decisions (each source-verified)

- **Separate `taint_edges` table, not rows in CRG's `edges`.** CRG reconciles
  `edges` per file (`DELETE FROM edges WHERE file_path = ?` in both build and
  update paths), so injected rows there would be silently wiped. A table CRG
  doesn't know about survives both — *verified*, not assumed.
  This resolves the spec's open "serve-time join vs build-time merge" question
  in favour of build-time merge into the same DB: one connection, one query
  surface, no orchestration-layer join.
- **Keyed on `qualified_name`** (`file/path::Symbol`, POSIX slashes) — the
  same strings CRG's own edges use, so joins line up without FK work.
- **Node mapping is span containment**: a finding at `file:line` resolves to
  the smallest enclosing non-File node (`line_start <= line <= line_end`),
  falling back to the File node. This is what makes a SAST report's raw
  line numbers land on real functions.
- **Findings persist at `<data-dir>/taint-findings.json`** — the data dir *is*
  the branch slot, so taint data is branch-scoped for free, and
  `swap_or_build.py` calls `reapply` after every successful build/update so a
  rebuilt graph gets its taint edges back automatically.
- **Unresolved findings are reported, never dropped** — a security tool that
  silently loses findings is worse than one that fails loudly.

### Consumed by triage

`test_triage.py` now intersects the blast radius with `taint_edges` and prints
a `taint-exposed subset of blast radius` section — the appsec-relevant slice of
the structural radius (spec case 4). With nothing injected it says so
explicitly rather than omitting the line.

### Verification (run `python verify_taint.py`) — 16/16

Semgrep-shaped findings resolve to exact node ids (validated by joining
`taint_edges` against `nodes`), source lands on the enclosing
`read_user_input`, sink on `run_query`; a deliberately unresolvable finding is
reported; apply is idempotent; rows survive `update`; `reapply` re-populates
after a full rebuild; triage surfaces the exposed subset; and taint data is
genuinely branch-scoped (absent on a sibling branch that has the *same file*,
restored on swap back).

### Caveat

Per the spec's warning: do **not** cite CRG's benchmarked "recall 1.0" in
security documentation — their own README calls it graph-derived/circular.
Line-based findings are resolved against the graph's *current* line spans, so
re-run your analyzer (not just `reapply`) after code moves.

## Config/schema linkage pass (spec Req 2, case 6)

```bash
python config_link.py scan              # scan repo, resolve, inject
python config_link.py scan --dry-run    # findings JSON, no DB writes
python config_link.py query --symbol load_settings
python config_link.py status | reapply | clear
```

**Checklist precondition verified first** (the spec's "verify against source
whether such edges exist before building"): they do **not**. CRG v2.3.8 emits
exactly six edge kinds — CALLS, TESTED_BY, CONTAINS, IMPORTS_FROM, REFERENCES,
INHERITS — and `parser.py` has no env-var read detection at all
(`DEPENDS_ON` exists in the source but only for Ansible role deps and Solidity
`using` directives). `verify_config.py` re-asserts this as a live check, so a
future CRG release that *does* add config edges will fail the suite loudly
rather than leave this pass silently redundant.

Two link kinds in a `config_edges` table (same survives-build-and-update
architecture as the taint injector):

| kind | meaning |
|---|---|
| `READS_CONFIG` | code node → env var, with every config file that DEFINES it |
| `USES_SCHEMA` | code node → SQL table node (a real CRG node) |

Detection: env-var reads across Python/JS/TS/Go/Java/C#/Ruby/PHP; definitions
from `.env*`, `Dockerfile`, docker-compose, GitHub Actions/k8s YAML,
Terraform `variable` blocks, `.tfvars`, and `.properties`. Schema references
(`FROM`/`JOIN`/`INSERT INTO`/`UPDATE`/`DELETE FROM`, plus `__tablename__`) are
matched **only against tables CRG already parsed from `.sql` files**, so the
graph never gains edges pointing at nothing.

### Deliberately asymmetric

The **code side always resolves to a real graph node** (that's what triage
joins on to answer "which functions are affected"). The **config side** carries
a real `target_qualified` only where CRG actually parses the format — `.sql`,
`.tf`, `.yaml`, `.properties` get nodes; `.env` and `Dockerfile` get none — and
always carries honest `config_file`/`config_line`/`config_key` columns. Being
explicit about that beats inventing synthetic nodes that no join can reach.

### Verification (run `python verify_config.py`) — 25/25

Env vars link to their definition sites across `.env`, Dockerfile and
Terraform; a var read but never defined produces **no** edge, and one defined
but never read is not linked; SQL references link to flask's own pre-existing
`examples/tutorial/flaskr/schema.sql` nodes (not just probe fixtures); the code
side is join-verified against `nodes`; scan is idempotent, rows survive
`update`, `reapply` restores after a rebuild, triage surfaces the dependencies,
and the data is branch-scoped.

Two bugs this suite caught, both worth recording:

* **A real injector bug**: the dedup key omitted the config file, so a var
  defined in *both* `.env.example` and `infra.tf` collapsed to one row —
  total collapse for `READS_CONFIG`, whose `target_qualified` is always NULL.
  Each definition site is a distinct contract; the key now includes it.
* **A bug in the test itself**: `target_qualified LIKE '%.env%'` matches the
  *symbol* `werkzeug.test.EnvironBuilder`, so the "CRG has no config edges"
  precondition falsely failed. It now joins on `nodes.file_path` instead —
  the same false positive is worth avoiding in any query you write against
  this schema.

## Setup from scratch

```bash
# same venv as the graphify work
.venv/Scripts/pip install -e crg/crg-upstream
cd crg/sandbox-flask
code-review-graph build
python ../swap_or_build.py install-hook
python ../verify_req1.py && python ../verify_req2.py \
  && python ../verify_taint.py && python ../verify_config.py
```

All four suites (66 checks total) leave the sandbox clean — branches deleted,
probe files removed, `main` reset to its original commit.
