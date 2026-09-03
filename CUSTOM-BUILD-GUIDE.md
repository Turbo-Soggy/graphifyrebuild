# Custom Fix-Layer Build — What It Does, How To Call It

Reference for the custom graph tooling in `C:\projects\graphifyrebuild`, written
for evaluation *before* connecting anything to a real repository.

---

## Connection status: **CONNECTED to `C:\projects\appsec-fix-layer`**

Build A (`graphify-ext`) is live in that repo. It was an *upgrade*, not a fresh
install — the repo already ran stock graphify (hooks, a 5,059-node
`graphify-out/`, and graphify references in `CLAUDE.md` / `AGENTS.md`).

| | state |
|---|---|
| Target repo | `C:\projects\appsec-fix-layer` — on `master`, 3,451 source files, graph 5,059 nodes / 6,944 edges |
| Hooks | `post-commit` + `post-checkout` replaced **in place** with the ext variants (stock markers reused, so exactly one graphify block each). Pinned to `C:\Python314\python.exe` — the interpreter the repo already used. |
| Interpreter | `graphify-ext` installed into `C:\Python314` (user-site). **`graphifyy` left untouched at 0.9.47** |
| Output path | `graphify-out/` is now a link to `.graphify-cache/master/`; contents preserved byte-for-byte |
| Git cleanliness | `graphify-out` (bare) and `.graphify-cache/` added to `.git/info/exclude` — *not* the tracked `.gitignore`. `git status` is clean. |
| Backup | Original hooks + `exclude` saved to `.tmp/graphify-ext-connect-backup/` |
| Other repos | nothing else connected. The three benchmark sandboxes are clean (only git's own `post-update.sample`). |

### Version note

graphify-ext was verified against `graphifyy` **0.9.53**, but this repo runs
**0.9.47**. Compatibility was checked explicitly before connecting rather than
assumed — the hook-template splice succeeds and every private API
(`_rebuild_code`, `detect_incremental`, and the `watch` helpers) has the
expected signature under 0.9.47. Per the compatibility contract below, re-check
after any graphify upgrade.

### Undoing it

```bash
cd C:\projects\appsec-fix-layer
cp .tmp/graphify-ext-connect-backup/post-commit .git/hooks/post-commit
cp .tmp/graphify-ext-connect-backup/post-checkout .git/hooks/post-checkout
# restore a real output directory from the active slot
rm graphify-out && cp -r .graphify-cache/master graphify-out
rm -rf .graphify-cache
```

Re-running stock `graphify hook install` also reverts the hooks on its own,
since both builds share the same markers.

---

## What is in this repo, and why there are two builds

The work was specified twice. The second spec explicitly superseded the first,
but the first implementation was already complete and verified, so both remain.

| build | directory | built on | status |
|---|---|---|---|
| **A. graphify-ext** | `graphify_ext/` | stock **graphify** (`graphifyy` 0.9.53) | complete; 89 unit tests + differential suites vs stock, all passing |
| **B. crg** | `crg/` | stock **code-review-graph** 2.3.8 | complete, 66 verification checks passing |

Both implement the same two requirements:

1. **Per-branch incremental graph caching** — reuse a cached graph per branch
   instead of a full rebuild on every branch switch.
2. **AppSec fix-context layer** — give a coding agent blast radius, overrides,
   taint exposure, test coverage, config/schema dependencies, and post-fix
   verification before it edits vulnerable code.

They differ because the underlying tools differ: graphify stores a JSON graph
plus a manifest; CRG stores a single SQLite database and already ships native
blast-radius, test-coverage, and change-risk tooling.

**If "rebuild of graphify" means Build A, benchmark it against stock
`graphifyy`. If it means the fix layer generally, Build B is the more complete
implementation and benchmarks against stock `code-review-graph`.** Both
baselines are already installed in the same venv, so either comparison can be
run without further setup.

---

## Build A — `graphify_ext` (on stock graphify)

### What it adds over stock graphify

| capability | stock graphify | this build |
|---|---|---|
| `git commit` | incremental AST diff | unchanged, plus per-branch slot stamping and re-application of injected edges |
| `git checkout` (branch switch) | **full rebuild, every time** | per-branch cache slot, reused on switch-back, reconciled incrementally |
| History rewrite (rebase/force-push) | no detection | base-commit ancestry check → falls back to full rebuild |
| Detached HEAD | no stable key | dedicated scratch slot, always rebuilt |
| Graph schema/version drift | no field to check | version stamp in a slot sidecar → full rebuild on mismatch |
| Transitive blast radius | none (only point-to-point `path A B`) | `blast-radius --depth N`, relation-filterable, reports `estimated_tokens` |
| Symbol source for the agent | none (graph.json has no extents or code) | `context`: seed + neighbours as source within a token budget, gaps disclosed (`omitted`/`unresolved`/`unmodelled`/`stale_files`) |
| Seeds by location / qualified name | id, label, bare name, path | plus `file:line` and `res.json`-style names; `search` lists candidates |
| Definitions the extractor misses | absent (assignment-bound JS members, id-collision victims) | `supplement` materialises them; 53 → 68 of 70 corpus tasks scoreable |
| Overrides of a base method | `inherits` edges exist, no query | `overrides` command |
| Taint reachability | **none** | `inject --semgrep` → `taints` / `reaches_sink` edges |
| Test-coverage edges | **none** | `test-link --coverage` (coverage.py) or `--heuristic` |
| Config/env linkage | **none** | `config-scan` → `reads_config` edges |
| Post-fix edge diff | **none** | `verify-fix snapshot` / `check` |
| Cross-repo | `global add` / `merge-graphs` | unchanged (documented, not re-implemented) |
| Agent transport | CLI only | MCP server (`graphify-ext-mcp`), nine tools over stdio or HTTP |
| Graph refresh after an edit | `graphify update .` by hand | `refresh`: stale files detected from the manifest, incremental update, ext layer re-applied |
| Languages sliced to source | n/a | py, js, ts, go, java, rs, rb, php, kt, cs |

Injected edges carry `confidence: "EXTERNAL"` and `origin: "graphify-ext"`, so
they are idempotent to re-apply and never confusable with extractor output
(stock vocabulary is `EXTRACTED` / `INFERRED` / `AMBIGUOUS`).

### Install

```bash
cd C:\projects\graphifyrebuild
.venv\Scripts\pip install -e .
```

Requires `graphifyy` in the **same** environment for the hook and cache
commands; the pure-JSON commands (blast-radius, triage, verify-fix, inject)
work against any `graph.json` without it.

### Command reference

Every command below was executed end-to-end against a scratch graph, and the
agent-facing ones against the connected repo's real graph.

```bash
graphify-ext blast-radius "<node>" [--depth N] [--direction up|down|both]
                                   [--max-nodes N] [--relation REL ...]
                                   [--include-containment] [--list-relations]
                                   [--graph PATH] [--json]
```
Scoped subgraph around a node. `--direction up` (default) walks callers —
who is affected by changing this. Output is depth-tagged nodes plus a closed
edge set, with a `truncated` flag and an `estimated_tokens` figure.

**`--relation` is the effective token lever.** Measured on a hot node in a real
repo: the default 2-hop bidirectional walk returns 50 nodes / ~14,700 tokens
while `--relation calls` returns 23 / ~4,200 — a 3.5x reduction. `--max-nodes`
is a poor proxy for budget; at its 500 default it never fired in that
measurement, so `truncated` stayed `false` while the cost was large.

`--include-containment` additionally follows `contains`/`method`, answering
"what is in this class/file". It is opt-in because containment is typically the
single most common relation in a graph (4,256 of 6,944 edges on the connected
repo), so following it by default floods every radius. `--list-relations`
prints what a given graph actually contains and which relations are followed by
default.

```bash
graphify-ext context "<node>" [--depth N] [--direction up|down|both] [--budget TOKENS]
                              [--per-symbol-cap LINES] [--relation REL ...]
                              [--no-containment] [--graph PATH] [--json]
```
The agent-facing command. Returns the seed symbol's **source** plus its
neighbours' source, ordered and cut to a token budget, with every gap named:
`omitted` (budget, with `truncated_high_rank`/`truncated_low_rank` severity),
`unresolved` (with a reason code — `definition_mismatch` means the graph's line
for that symbol now holds a different definition, i.e. the graph is stale),
`unmodelled` (definitions in the shown code that have no graph node at all) and
`stale_files` (files whose content hash no longer matches graphify's
`manifest.json`). Containment is ON by default here, unlike `blast-radius`.

**Seeds.** Every `<node>` argument accepts a node id, a label, a bare name, a
source path, a qualified name (`res.json`, `Widget.build`) or a location
(`lib/response.js:239`, the shape a stack trace or a SAST finding uses). An
ambiguous seed exits with the candidate list instead of a bare failure.

`--index-budget` (default 300) reserves part of `--budget` for an **index
tier**: one `file:line signature` line per symbol that did not fit as a body
or sits one hop beyond `--depth`; the index also spends whatever the bodies
leave unused. Related classes are rendered as signature + member list. The
pack ends with the **tests** whose nodes link to anything shown (relation and
confidence per link), so "what do I run afterwards" is answered in the same
call. Extents are sliced for Python, JavaScript, TypeScript, Go, Java, Rust,
Ruby, PHP, Kotlin and C#.

```bash
graphify-ext refresh [PATH ...] [--out DIR] [--json]
```
Incremental graphify update for the given files (default: every file whose
`manifest.json` hash no longer matches the tree), then re-application of the
supplement and injected edges. Never a full rebuild. Run it after editing and
before re-querying; `!!! GRAPH STALE` in a context pack is the cue.

```bash
graphify-ext search "<query>" [--limit N] [--graph PATH] [--json]
```
Every node a query could mean, best first (exact → bare name → substring;
callables before non-callables), with file, line, qualified name and origin.

```bash
graphify-ext supplement [--dry-run] [--graph PATH] [--json]
```
Materialise definitions the extractor has **no node for**, so they become
queryable: JavaScript members bound by assignment (`res.json = function …`,
`proto.param = function …` — express binds its whole public API this way and
stock graphify emits none of it) and definitions lost to id collisions
(`@overload` stubs, `_get_x` vs `get_x`; upstream #3302). Each gets a node in
the extractor's own schema (`origin: "graphify-ext:supplement"`, plus
`qualified_name` and `supplement_reason`), a `contains`/`method` edge from its
owner, and conservative **INFERRED** `calls` edges (unambiguous name match
only; same file first). Extractor nodes are never modified. Functions nested
inside functions are declined (graphify omits them by design; the pack
discloses them). Files the graph is stale for are **refused whole** and
listed. Idempotent; running it once opts the output slot in to re-application
by the hooks after every rebuild. Measured effect: see `README.md`.

```bash
graphify-ext overrides "<node>" [--graph PATH]
```
Subclass implementations that override a base method — each needs its own fix.

```bash
graphify-ext inject <findings.json> [--graph PATH] [--no-store]
graphify-ext inject --semgrep <out.json> [--taint-rule ID ...] [--assume-taint]
graphify-ext inject --joern <flows.json>
```
Merge external edges into `graph.json`. Findings are persisted so a later
rebuild can restore them.

**Joern as the taint engine.** Semgrep's `dataflow_trace` is absent on most
real findings (measured 9 of 9). Joern's `reachableByFlows` always yields the
full interprocedural path, so `--joern` emits the endpoints (`taints`,
`reaches_sink`) **and the chain**: one `taints` edge per consecutive pair of
path elements that land in different functions. An agent shown only the
endpoints cannot see where along the flow the sanitiser belongs; the chain is
that answer. Produce the JSON in a Joern shell with
`bench/joern/export_flows.sc` (`exportFlows(out, sources, sinks, rule)`); the
adapter also accepts Joern's raw `toJson` of a `List[Path]`. Joern and the JVM
are not dependencies of this package; the adapter is pure JSON and every
element that fails to resolve to a graph node is reported, never dropped.

**Validated end to end (2026-09-04).** Joern CLI v4.0.617 (`pysrc2cpg`) on
`corpus/vuln_app` via `bench/joern/corpus_flows.sc`, injected with
`from_joern`, scored by `python corpus/validate_taint.py --build joern`:
**9 of 9 checks pass** -- every true-positive flow lands on the expected source
and sink nodes, the multi-hop shell flow is a chain of two distinct nodes, and
no true-negative function is exposed. The last one needed a lesson: Joern does
not know `sanitize` is a sanitizer, so it reported
`tn_sanitized_sql -> sanitize -> run_sql` as a flow; the export scripts drop
any path that enters a declared sanitizer's body or call
(`--param sanitizers=<regex>` on `run_export.sc`). Declare your sanitizers or
the chain edges will include neutralised paths. `joern.bat` passes `--param`
values through `cmd.exe`, which eats `|` in regexes; `corpus_flows.sc` is the
parameter-free form for that reason.

**On trace-less semgrep findings.** Semgrep emits a `dataflow_trace` only when
a flow has a multi-step path; when source and sink are the same expression it
emits none. Measured against a real scan, **9 of 9 taint findings had no
trace**. Semgrep's JSON also carries no indication of whether a rule ran in
taint mode, so such findings are neither dropped nor assumed: declare the rule
with `--taint-rule <id-substring>` (or `--assume-taint` when the whole scan was
taint rules) to map them, and anything not mapped is listed as **skipped** in
the output rather than disappearing.

```bash
graphify-ext test-link --coverage <coverage.json>   [--graph PATH] [--dry-run]
graphify-ext test-link --heuristic                  [--graph PATH] [--dry-run]
```
`tests` edges. Coverage-context ingestion is ground truth; the heuristic is
conservative and emits nothing for ambiguous names rather than falsely
claiming coverage.

```bash
graphify-ext config-scan [PATH] [--graph PATH] [--dry-run]
```
`reads_config` edges linking env-var reads to the config files that define them.

```bash
graphify-ext reapply [--out DIR]
```
Re-inject stored findings after a rebuild rewrote `graph.json`.

```bash
graphify-ext triage <report.json> [--depth N] [--max-nodes N]
                                  [--graph PATH] [--out PATH]
```
Per-vulnerability agent context. Input is `[{"id","description","file","line"}]`
(optional `"function"` overrides location lookup). Prints a summary; `--out`
writes the full context JSON.

```bash
graphify-ext edge-diff snapshot --node X [--node Y ...] [--out DIR]
graphify-ext edge-diff check [--out DIR] [--json]
```
Pre/post-fix structural edge diff (was `verify-fix`, kept as a warning alias;
it runs no scanner and no tests). `check` exits 2 on an unexpected delta.
Community/cluster attributes are excluded, so clustering churn is never a
false positive.

### MCP server (agent-native surface)

```bash
pip install -e ".[mcp]"          # fastmcp is an OPTIONAL extra
graphify-ext-mcp                 # stdio (what an agent spawns)
graphify-ext-mcp --tools blast_radius_tool,triage_tool
graphify-ext-mcp --http --host 127.0.0.1 --port 5599
graphify-ext-mcp --list-tools
```

Exposes nine tools — `search_tool`, `context_tool`, `blast_radius_tool`,
`overrides_tool`, `triage_tool`, `edge_diff_tool`, `supplement_tool`,
`refresh_tool`, `list_relations_tool` — so an agent calls a tool in-loop
instead of shelling out and re-parsing stdout. Structure is copied from
`code-review-graph`'s own server, so both builds behave the same for a client:
plain-dict returns, errors returned as data (`{"status": "error", ...}`) rather
than raised, and `--tools` filtering via `local_provider.remove_tool`.

**fastmcp is optional on purpose.** `graphify_ext` stays dependency-free so the
CLI and the git-hook path import cleanly under an interpreter that has graphify
but not fastmcp — which is the case for the connected repo. The import happens
inside the server factory, so `import graphify_ext` works without it and the
command exits with an install hint rather than a traceback.

#### `verify-fix` pass/fail contract (exact)

This command is meant to gate CI, so the threshold is specified rather than
left implicit in the code.

**Fingerprint.** For each `--node`, every edge where that node is the source
*or* the target, projected onto exactly these fields:
`source`, `target`, `relation`, `confidence`, `origin`.

**Everything else is ignored**, including `community`, `weight`,
`confidence_score`, `context`, `source_file`, and `source_location`.

**Threshold: zero tolerance.** Any fingerprint entry added or removed is a
delta. There is no count-based tolerance and no per-relation filtering — a
single changed edge fails the check.

**Scope.** Only the nodes you named. Edges elsewhere in the graph, however
large the change, do not affect the result.

**Exit codes.** `0` clean · `2` delta detected · `1` error (no snapshot found,
or `snapshot` could not resolve a named node).

Consequences worth knowing before wiring this into CI:

| change | flagged? |
|---|---|
| A call moves to a different line | **no** — `source_location` is excluded |
| A call is added or removed | **yes** |
| Confidence tier changes (`EXTRACTED` → `INFERRED`) | **yes** |
| Community/cluster reassignment | **no** — excluded by design |
| Unrelated edges change elsewhere in the graph | **no** — node-scoped |
| The fixed function is **renamed** | **yes** — the old node has no edges, so its whole fingerprint reads as removed. Snapshot under the new name, or expect a delta on every rename. |

```bash
graphify-ext hook install | uninstall | status
graphify-ext swap [--branch B]
```
**`hook install` is the only command that connects anything.** It replaces the
stock hook bodies in `.git/hooks` (sharing stock markers, so exactly one
graphify block ever exists; stock `graphify hook install` reverts it).
`swap` runs the branch-cache logic manually, which is how to exercise
per-branch caching without installing hooks at all.

### Tests

```bash
.venv\Scripts\python -m pytest tests -q
```
73 passed, 1 skipped. Includes an end-to-end branch-cache lifecycle run against
the real `graphifyy` package, executed twice — once in link mode and once in
copy mode.

---

## Build B — `crg` (on stock code-review-graph)

### What it adds over stock CRG

| capability | stock CRG | this build |
|---|---|---|
| Branch switch | one shared SQLite DB, **not branch-aware** | per-branch slot, `.code-review-graph` linked to the active slot |
| History rewrite / detached HEAD | not handled as distinct events | explicit labeled fallbacks to full build |
| Blast radius | native `get_impact_radius_tool` | **unchanged — used as-is** |
| Test coverage | native `TESTED_BY` edge type | **unchanged — used as-is** |
| Post-fix risk | native `detect_changes_tool` | **unchanged — used as-is** |
| Taint reachability | **none** | `taint_inject.py` → `taint_edges` table |
| Config/schema linkage | **none** (verified in source, not assumed) | `config_link.py` → `config_edges` table |
| Cross-repo | native registry | unchanged |

Both injectors write to **private tables CRG does not know about**, which is
what lets injected data survive `build` *and* `update` — CRG reconciles only
its own per-file rows. Findings JSON lives in the data directory, which *is*
the branch slot, so injected data is branch-scoped automatically.

### Command reference

Run these from inside the target repository (or pass `--repo`).

```bash
python crg\swap_or_build.py                     # swap/build for current branch
python crg\swap_or_build.py install-hook [--with-post-commit]
```
Prints exactly one labeled outcome per run: `FULL BUILD`, `CACHE HIT + UPDATE`,
`CACHE INVALID, REBUILDING`, or `DETACHED HEAD, FULL BUILD`.
**`install-hook` is the only connecting command** — it writes a plain
`.git/hooks/post-checkout`. Uninstall by deleting that file.

```bash
python crg\taint_inject.py apply --semgrep <out.json>     [--repo PATH]
python crg\taint_inject.py apply --findings <findings.json>
python crg\taint_inject.py query --symbol X | --file F    [--json]
python crg\taint_inject.py status | reapply | clear
```

```bash
python crg\config_link.py scan [--dry-run]                [--repo PATH]
python crg\config_link.py query --symbol X | --file F     [--json]
python crg\config_link.py status | reapply | clear
```

```bash
python crg\test_triage.py --file src/x.py --symbol name [--post-fix] [--repo PATH]
```
Spawns a live `code-review-graph serve` restricted to five tools and prints
each tool's raw JSON, plus the taint-exposed subset of the blast radius and the
config/schema dependencies.

**Injection ordering matters:** inject only while `.code-review-graph` is
already slot-linked (i.e. after a `swap_or_build.py` run). Injecting into a
not-yet-linked real directory means the next swap replaces it with the cached
slot copy.

### Verification suites

```bash
cd crg\sandbox-flask
python ..\verify_req1.py     # 14/14  per-branch caching
python ..\verify_req2.py     # 11/11  triage context via live MCP
python ..\verify_taint.py    # 16/16  taint injector
python ..\verify_config.py   # 25/25  config/schema linkage
```
66 checks total. Each prints PASS/FAIL per acceptance criterion and leaves the
sandbox clean (branches deleted, probe files removed, `main` reset).

---

## Benchmarking against stock

### Measured results

**Both builds now have real-repo numbers.** Full detail, methodology, and
caveats are in [TEST-RESULTS.md](TEST-RESULTS.md); the suites live in `bench/`.

Build A (graphify-ext), link mode, within-run minimums:

| operation | Flask (83 files) | scrapy (485 files) |
|---|---|---|
| **Branch switch — stock** | 9.6 s | 55.7 s |
| **Branch revisit — ext** | **5.0 s (1.9x)** | **3.2 s (17.4x)** |
| Commit-triggered incremental | 4.7 s → 5.4 s | 26.7 s → 25.9 s |
| Disk per branch | 4.7 MB (stock output 3.4 MB) | 27.3 MB (stock 26.7 MB) |

**The advantage grows with repo size** — stock's cost tracks the whole corpus,
the revisit tracks the diff. On a repo as small as Flask, graphify's per-rebuild
fixed overhead dominates and the win is modest (1.5x–2.4x across runs).

Forced **copy mode** passes the same 31/31 and costs 6.2 s versus link mode's
5.0 s, so it gives up about a quarter of the benefit.

Build B (CRG) on the same project: stock full `build` 10.6–13.5 s, no-op
`update` 1.4 s (essentially all process startup), branch revisit 2.9 s.

> **Do not compare absolute times across runs.** The same stock full build
> measured 20.7 s in one run and 9.1 s in another purely from machine load.
> Only within-run ratios are meaningful.

### What to measure

1. **Cold full build** — stock vs custom. Should be *equal*; the custom build
   delegates to the same builder. Any gap is overhead worth explaining.
2. **Branch switch A → B → A** — the whole point. Stock pays a full rebuild on
   each switch; custom should pay a diff-sized reconcile on the return trip.
3. **Commit-triggered incremental** — should be near-identical to stock, since
   both call the same incremental path. Confirms the customization adds no
   regression on the common case.
4. **Disk footprint** — the per-branch cache stores one graph per branch. Cost
   is roughly (graph size × number of branches you visit).
5. **Fallback correctness, not just speed** — a fast wrong answer is worse than
   a slow right one. The suites above already assert that history rewrites and
   detached HEAD fall back to full builds.

### Confounds to control on this machine

- **Background hooks.** If hooks are ever installed, a rebuild fires
  asynchronously and pollutes timings. Set `GRAPHIFY_SKIP_HOOK=1` (Build A) or
  `CRG_CACHE_SKIP=1` (Build B) while benchmarking. Currently no hooks are
  installed anywhere.
- **Process startup dominates small runs.** A no-op CRG `update` costs 1.4 s of
  pure startup. Subtract it before quoting incremental speedups, or measure
  several operations per process.
- **First run includes migrations.** CRG ran schema migrations v1→v9 on its
  first build; discard the first measurement.
- **Clustering determinism.** Build A pins `PYTHONHASHSEED=0` in its hooks
  because networkx community detection is hash-order sensitive. Set it manually
  for reproducible comparisons.
- **Antivirus / filter drivers.** This machine's file scanning inflates
  file-heavy operations, and a filter driver breaks directory-junction
  traversal under the user profile — Build B therefore probes the link and
  falls back to copy mode there. **Copy mode makes swaps more expensive than
  link mode**, so benchmark under `C:\projects\...` (where links work) or
  report which mode was active; both builds print it.
- **Warm OS file cache.** Alternate stock and custom runs rather than doing all
  of one then all of the other.

---

## Upstream version compatibility contract

Both builds are verified against **exact pinned versions**: `graphifyy` 0.9.53
and `code-review-graph` 2.3.8.

> **Operational rule: after any upstream upgrade, re-run the full verification
> suite before trusting either build.** Nothing enforces this automatically.
> The commands are in the "Verification suites" and "Tests" sections above.

### What each build actually couples to

**Build A → graphify** (private API; upstream gives no stability guarantee):

| coupling | breaks how |
|---|---|
| `watch._rebuild_code(path, changed_paths=, force=, ...)` | signature change → `TypeError` at run time — **loud** |
| `watch._apply_resource_limits`, `_read_build_excludes`, `_read_build_gitignore` | renamed/removed → `ImportError` — **loud** |
| `detect.detect_incremental(root, manifest_path=, kind=, ...)` | signature change → caught, falls back to full rebuild — **silent but safe** |
| `hooks._HOOK_SCRIPT` / `_CHECKOUT_SCRIPT` / `_detached_launch` templates | layout change → `_compose_scripts()` raises with an explicit message — **loud, by design** |
| `hooks` markers, `_install_hook`, `_pinned_python`, `_git_root`, `_hooks_dir` | renamed → `ImportError` — **loud** |
| graph.json shape (`nodes`, `links`/`edges`, `relation`, `confidence`, `origin`, `source_location` = `"L<n>"`) | schema drift → wrong or empty results — **silent**, the weakest link |
| graphify package version | recorded per slot; a change invalidates cached slots → full rebuild — **automatic** |

**Build B → code-review-graph** (mostly SQLite schema, not Python API):

| coupling | breaks how |
|---|---|
| DB at `.code-review-graph/graph.db` | path change → "no graph DB" error — **loud** |
| `nodes(qualified_name, kind, file_path, line_start, line_end, name)` | column rename → SQL error — **loud** |
| `edges(kind, source_qualified, target_qualified)` | same — **loud** |
| `metadata` key `git_head_sha` | removed → falls back to the sidecar stamp — **silent but safe** |
| qualified-name format `<posix/path>::<Symbol>` | format change → mappings miss, reported as unresolved — **silent but reported** |
| CLI `build` / `update` / `--version` output | change → parse/exec failure — **loud** |
| MCP tool names and response shape (`gaps.untested_hotspots`) | change → triage sections read empty — **silent** |
| private tables `taint_edges`, `config_edges` | a future CRG table of the same name would collide — **silent**, watch for it |
| CRG version | recorded per slot; a change invalidates cached slots — **automatic** |

### Enforcement that already exists

- **Hook template drift fails loudly.** `hooks_ext._compose_scripts()` raises
  if its rebuild body cannot be spliced into upstream's template.
- **Version change invalidates caches.** Both builds stamp the upstream version
  into each slot and force a full rebuild when it differs, so a cache built by
  another version is never reused.
- **`verify_config.py` asserts its own precondition** — that stock CRG emits no
  config edges — so a release that adds them fails the suite instead of leaving
  the pass silently redundant.

The genuine gap in both builds is **graph/DB schema drift**: a field quietly
changing meaning would produce wrong results rather than an error. That is what
the verification suites are for, and why the operational rule above exists.

## Honest limitations

- **Taint findings are line-based.** They resolve against the graph's *current*
  line spans, so after code moves you must re-run the analyzer, not just
  `reapply`. `reapply`'s real guarantee is surviving a graph *rebuild*, not
  code motion.
- **Taint requires an external analyzer.** Neither build detects taint itself;
  they map Semgrep/CodeQL/SISA findings onto graph nodes. Validating that
  mapping against a known-vulnerable corpus at scale is still outstanding.
- **The config pass is regex-based** for env-var reads and SQL table
  references. It matches only tables the graph already knows, so it will not
  invent edges, but it will miss dynamically constructed names.
- **The heuristic test-link is conservative by design** — ambiguous names
  produce no edge, so absence of a `tests` edge is not proof of no coverage.
  Prefer coverage-context ingestion.
- **Do not cite CRG's benchmarked "recall 1.0"** in security documentation.
  Their own README calls it graph-derived and circular.
- **Build A's copy-mode path exists because links are not always available.**
  It is correct and tested, but slower than link mode.

---

## File map

```
graphify_ext/            Build A package
  branch_cache.py          per-branch slots, activation, swap/build
  hooks_ext.py             hook composition from upstream templates
  blast_radius.py          BFS subgraph + overrides
  edge_inject.py           external edge merge + Semgrep adapter
  test_link.py             coverage / heuristic tests edges
  config_link.py           env-var → config file edges
  triage.py                per-vuln agent context
  verify_fix.py            pre/post edge diff
  graphio.py               graph.json access + node resolution
  __main__.py              graphify-ext CLI
tests/                   Build A test suite (73 passed, 1 skipped)

bench/                   Differential testing of Build A vs stock graphify
  setup_sandbox.py         pristine sandbox + pinned `bench-base` tag
  harness.py               shared: reset, timing, graph compare, hook control
  regression_a.py          Section A — stock behaviour untouched (19/19)
  bench_b.py               Section B — per-branch caching, timed (31/31)
  cross_c.py               Section C — cross-cutting (11/11)

corpus/                  Known-vulnerable corpus for taint-mapping validation
  vuln_app/                3 true-positive flows, 4 true negatives
  ground_truth.json        expectations as (function, call) pairs, AST-resolved
  validate_taint.py        runs against both builds (24/24)

crg/                     Build B
  swap_or_build.py         per-branch cache + hook installer
  taint_inject.py          taint_edges injector
  config_link.py           config_edges pass
  crg_graphdb.py           shared DB access + node resolution
  test_triage.py           live-MCP triage client
  verify_req1/req2/taint/config.py   verification suites (66 checks)
  crg-upstream/            read-only clone of stock CRG 2.3.8
  sandbox-flask/           throwaway Flask clone used as test corpus
  README.md                deeper design notes + verified upstream facts

graphify-upstream/       read-only clone of stock graphify (v8 branch)
CUSTOM-BUILD-GUIDE.md    this file
TEST-RESULTS.md          differential results vs stock + critique responses
```

Design rationale, the upstream facts each build was verified against, and the
spec assumptions that turned out to be wrong are recorded in
`crg/README.md` and `README.md`. Measured comparisons against stock, the bugs
those tests found, and the methodology caveats are in `TEST-RESULTS.md`.
