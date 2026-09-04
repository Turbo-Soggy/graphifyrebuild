# graphify-ext: from here to "done", in detail

> Tracked copy of the approved plan (source: ~/.claude/plans/curried-snacking-lemon.md).
> Progress log at the bottom is updated at the end of every gate day.

Decisions taken (2026-09-04): proving ground = a public large OSS corpus; scope =
general fixing **plus** appsec; eval budget ≈ $600 (Sonnet full matrix + Opus).

## Context

`graphify_ext` gives a coding agent fix-ready context over a graphify code graph.
Retrieval is measured and honest (68/70 seedable, bodies 0.636 / named 0.749 at
d2/6k, every gap disclosed). The direct measure, `bench/fixeval`, now says: on 23
small-repo tasks × 3 arms × 2 reps, the plain pack (21/46) is *not* ahead of grep
(25/46); pack + review checklist ties grep (25/46) with the best partial credit
(0.666); the pack won decisively on the single large multi-symbol task (11/12 vs
0/12). Joern is validated on a toy corpus (9/9). Overall the product is ~40% of
the way to "an agent fixes bugs autonomously *because* of this context".

"Done" below is a set of numeric gates, each with the workstream that closes it.

## Definition of done — the gates

| gate | statement | measured by |
|---|---|---|
| **G1 fix impact** | On ≥100 SWE-bench-Verified tasks from ≥3 large Python repos, the in-loop MCP arm resolves ≥ no-graph + 5 points with a paired bootstrap 95% CI excluding zero, **or** ties on resolve with ≥25% fewer turns at ≤ cost; direction consistent on Sonnet and Opus. Every miss classified. | WS1, WS2, WS11 |
| **G2 retrieval** | Shipped defaults reproduce their baseline (`regress.py`); named recall on the 70-task corpus ≥ 0.80 (from 0.749) via a co-change signal, with per-task diff and 0 regressions. | WS3 |
| **G3 verification** | `verify-fix` (tests + scanner + edge-diff) returns green on every resolved eval run and red on every unresolved one — zero false greens on ≥100 runs. | WS6 |
| **G4 appsec** | Joern chains on ≥10 real CVE/security fix commits (Django/Flask/requests history): ≥80% of flows land source and sink on the correct functions, 0 true-negative leaks on the corpus; Semgrep+Joern both inject into the same graph and `triage` shows chains. | WS10 |
| **G5 test pillar** | Coverage-derived `tests` edges: precision ≥ 0.9 and recall ≥ 0.7 against coverage.py ground truth on the Flask corpus; `related_tests` labels EXTRACTED vs INFERRED. | WS4 |
| **G6 config/schema** | ORM model / settings / schema key → code links on Django: precision ≥ 0.8 on a 50-item hand-labelled sample. | WS5 |
| **G7 freshness** | An agent edit followed by `context` never serves a stale slice: auto-refresh closes it within one call; measured on the eval traces (0 `definition_mismatch` after an edit). | WS7 |
| **G8 ranking** | One cross-pillar score; sweep reported; ≥ shipped defaults per task, 0 regressions. | WS8 |
| **G9 scale + platform** | Django-size graph (~30k nodes): `context` p95 < 2 s, `refresh` of 10 files < 10 s, memory < 1 GB; CI green on ubuntu, macos, windows for unit + e2e. | WS9 |
| **G10 release** | v1.0 tag; README/CLAUDE.md/MCP snippet current; every number carries its config; `scripts/check` green; settings.json fixed. | WS0, WS12 |

## Workstreams

Dependencies: WS0 → (WS1 ∥ WS3 ∥ WS4 ∥ WS5 ∥ WS7 ∥ WS10) → WS2 → WS6 → WS8 → WS11 → WS9 → WS12.
Effort figures are machine+agent days, not calendar days; eval wall time dominates.

### WS0 — Ops baseline (0.5 day)
1. Fix `.claude/settings.json` hook commands to `graphify hook-guard search|read` (needs your hand or a permission grant; the classifier blocks my edit).
2. `scripts/check.ps1` + a POSIX `scripts/check.sh` twin; add `python bench/fixeval/run.py report` and `python corpus/validate_taint.py --build all`.
3. GitHub Actions matrix (ubuntu/macos/windows × py3.11): `pip install -e ".[mcp]" pytest`, clone `graphify-upstream` at a pinned tag, run unit + `-m e2e`. Windows runner exercises copy-mode; ubuntu exercises symlink mode (both paths in `branch_cache.activate`).
4. Pin the upstream compatibility contract: a test that imports the five private upstream symbols `hooks_ext`/`branch_cache` rely on and fails loudly on drift (`_HOOK_SCRIPT`, `_detached_launch`, `_rebuild_code`, `detect_incremental`, `_pinned_python`).

### WS1 — In-loop MCP agent arm (1.5 days)
The product's real mode was never measured; the eval pasted the pack once.
1. `bench/fixeval/run.py`: new arm `mcp`. Launch `claude -p` with `--mcp-config <tmp.json>` pointing at `graphify-ext-mcp --graph <tree>/graphify-out/graph.supplemented.json` (stdio), `--allowedTools "Read,Edit,Write,Grep,Glob,mcp__graphify-ext__*"`, and a prompt that says only: "This repo has a code-graph MCP server; use `search_tool`/`context_tool` before reading files, `refresh_tool` after edits, and work `review_checklist` before finishing." No pasted pack.
2. Parse `--output-format stream-json` to count tool calls by name, tokens per turn, and whether `refresh_tool` was called after the first edit. Record in the results row (`tool_calls`, `first_edit_turn`, `refreshed_after_edit`).
3. Graph per tree: build once at the base commit with stock `graphify extract --code-only --no-cluster`, apply `supplement`, store beside the tree (reuse `regress._graph_for` pattern). The MCP server reads `graph` path from the config, so no cwd assumptions.
4. Arms for all later rounds: `nograph`, `graph` (pasted pack, kept for continuity), `mcp`. The checklist text lives in the MCP instructions already; `graph-guided` is retired.
5. Acceptance: on the existing 23-task set, `mcp` arm runs end to end, tool-call counts recorded, cost per run within 1.5× of `graph`.

### WS2 — Large public corpus: SWE-bench Verified subset (2 days build, ~4 days eval wall time)
Why SWE-bench Verified: human-validated tasks, real issue text as the problem statement, FAIL_TO_PASS/PASS_TO_PASS given, Dockerised grading — quotable and comparable. Repos with large Python codebases: django, sympy, scikit-learn, matplotlib, astropy, sphinx.
1. `bench/swe/`: `fetch.py` pulls `princeton-nlp/SWE-bench_Verified` (HF datasets), stratified sample: 100 tasks — 40 django, 20 sympy, 15 scikit-learn, 15 matplotlib, 10 astropy — seeded, frozen to `bench/swe/tasks.json` before any run.
2. Environment: Docker Desktop (WSL2 backend) on this machine; `pip install swebench`; pre-build the per-instance images for the sample (`swebench.harness.prepare_images`). Grading = `swebench.harness.run_evaluation` on the agent's `git diff` (model patch). This replaces our own venv logic for these repos.
3. Agent tree: `git clone` each repo once (bare mirror), worktree at `base_commit`, graph built + supplemented per task (Django ~30k nodes, ~90 s extract; cache by base_commit since many tasks share one).
4. Problem statement = the issue text (`problem_statement` field); hints field withheld.
5. Runs: `nograph` / `graph` / `mcp` × 2 reps on Sonnet (600 runs, est. $0.7–1.0 each on Django-size context → $450–600; cap per run $2; if the mean exceeds $0.9 after the first 60 runs, drop to 80 tasks). Opus confirmation: 40 tasks × `nograph`/`mcp` × 1 rep (~$150). Budget guard: `FIXEVAL_MAX_USD` global cap, dashboard shows spend.
6. Parallelism: 3 streams (one per arm), each `--rep` sequential; workers dedupe via the results file (already handled in `report`; also make `run` re-read `done` before each task to avoid duplicates).
7. Acceptance: `bench/swe/report.py` prints per-repo, per-arm resolve with paired bootstrap CIs (WS11).

### WS3 — Retrieval reach: co-change signal (1.5 days)
Deep-chain misses (Flask 11/19 zero) are structural blind spots; git history sees them.
1. `graphify_ext/cochange.py`: mine `git log --name-only -n 5000`, map changed files → symbols via `symbols.definitions_from_source` at each commit's parent (reuse `bench/agentctx/tasks.py::symbol_table` logic), build symbol-pair co-change counts with time decay; emit `cochanges` edges (INFERRED, `confidence_score` = normalised count, `origin: graphify-ext:cochange`) between symbols with count ≥ 3 and lift > 2. Persist to slot, re-apply like supplement.
2. `context.build_context`: `cochanges` gets weight 0.8 in `_REL_WEIGHT`; walked only at depth 1 from the seed by default (`--cochange` flag; config `cochange` in `regress.CONFIGS`).
3. Measure: `regress.py --config dyn300-mention-first-cochange` vs shipped; per-task diff; ship only with 0 regressions and named recall gain; target ≥ 0.80.
4. Guard: never let co-change pull in a node with no source (rationale/doc nodes) — reuse the file-node/unsliceable filters.

### WS4 — Test pillar with real coverage (1 day)
1. `bench/coverage/run.py`: on the 7 verifiable Flask tasks' trees, run `pytest --cov=src --cov-context=test` + `coverage json --show-contexts`; produce ground truth `{production symbol → tests}` via `test_link.from_coverage` resolution.
2. Compare `related_tests` (current name/import links) and `test-link --heuristic` against it: precision/recall per task; then inject coverage edges and re-measure `related_tests`.
3. Ship: `related_tests` entries carry `basis: coverage|import|name`, sorted coverage first; CLI trailer says which basis. Gate G5.

### WS5 — Config/schema linkage (2 days)
1. `graphify_ext/schema_link.py` producers → neutral findings `reads_config`/`defines_schema`:
   - Django: `settings.py` keys ↔ `settings.<KEY>` / `getattr(settings, "KEY")` reads; `models.Model` fields ↔ `Model.objects.filter(field=…)`/`.field` reads (tree-sitter attribute walk, same-file-first resolution as supplement's calls).
   - SQLAlchemy `Column(...)` fields, pydantic/dataclass schema fields, JSON Schema `properties`, YAML/TOML config keys (`config["key"]`, `cfg.get("key")`).
2. Hand-label 50 links on Django (25 settings, 25 model fields) in `bench/schema/labels.json`; precision/recall script. Gate G6.
3. `triage`/`context`: `config_dependencies` shows these with basis.

### WS6 — Strong `verify-fix` (1.5 days)
1. `graphify_ext/verify.py`: `verify(out_dir, tests=[...], scanner="semgrep"|"none", nodes=[...])` → runs the given tests (pytest ids or mocha titles, using the repo's own runner via a small per-language adapter), reruns Semgrep (and Joern if flows were injected) on changed files, runs `edge_diff.check`; returns `{tests: {id: status}, findings_before/after, edge_delta, verdict: green|red, reasons[]}`. Never green when any listed test is not PASSED or any pre-existing finding at the edited lines persists.
2. CLI `graphify-ext verify --tests ... --node ...`; MCP `verify_tool`. `CLAUDE.md` step 5 becomes: `verify` before saying done.
3. Gate G3 harness: `bench/fixeval` records `verify` verdict for every run; false-green rate must be 0 on the 23-task and SWE sets.

### WS7 — Freshness that needs no discipline (0.5 day)
1. `context_tool`/`graphify-ext context`: when `stale_files` would be non-empty, run `refresh.refresh(paths=stale)` first (bounded: ≤ 50 files, else report), then build the pack. Flag `--no-auto-refresh`. Record `auto_refreshed: [...]` in the pack.
2. Eval trace check (WS1 fields): `definition_mismatch` count after first edit must be 0. Gate G7.

### WS8 — Cross-pillar ranking (1 day, after WS3/4/5 land)
1. `context.score_node` extended: `score = w_rel(relation) × decay^depth × (1 + w_mention·mentioned + w_cochange·cochange_norm + w_test·covered + w_config·config_linked)`; weights swept on the 70-task corpus (`regress.py` configs), fitted not asserted (rule 8).
2. Ship only the combination that is ≥ shipped on named recall with 0 per-task regressions; report the sweep table in the roadmap. Gate G8.

### WS9 — Scale and platform (1.5 days)
1. `bench/scale/`: build the Django graph (~30k nodes, ~90k edges); measure `context` p50/p95 over 200 random seeds, `refresh` for 1/10/100 changed files, peak RSS; profile hot spots (`graphio.load` JSON parse ≈ once per call — add an in-process cache keyed by mtime in the MCP server; `symbols._FILE_CACHE` limit 256 → LRU by bytes).
2. Copy-mode branch cache on Django-size: measure swap time; fix anything > 30 s.
3. CI matrix from WS0 green; Linux symlink path and macOS exercised. Gate G9.

### WS10 — Appsec on real code (2 days)
1. `bench/joern/cve_set.json`: ≥10 security fix commits with known source→sink (Django CVE fixes, requests/Flask advisories); for each, run Joern (`run_export.sc` with per-repo sources/sinks/sanitizers), inject, check endpoints against the fixed function(s). Gate G4.
2. Semgrep on the same commits with `--taint-rule` declared; both engines into one graph; `triage` output shows chains with engine provenance.
3. Joern cost table (CPG build time per repo size) in the guide; `--joern` documented as opt-in.

### WS11 — Statistics and reporting (0.5 day)
1. `bench/fixeval/stats.py`: paired bootstrap (10k resamples) on per-task resolve rates and partial credit; McNemar on both-rep resolves; CIs printed in `report` and on the dashboard.
2. Miss classifier: for every unresolved run, `wrong_file | stopped_short (edited ≥1 target, missed ≥1) | broke_tests | no_edit | underspecified (all arms fail)`; per-arm histogram. Gate G1 requires this table.

### WS12 — Release (0.5 day)
1. README rating table replaced by measured gates G1–G10 with pass/fail; `bench/fixeval/README.md` and `bench/swe/README.md` carry method + caveats; roadmap addendum.
2. `pyproject` version 1.0.0, CHANGELOG, tag; `claude mcp add` snippet and a Claude Code skill file (`skills/graphify-ext/SKILL.md`) with the workflow from `CLAUDE.md`.

## Cadence (agreed 2026-09-04): one gate per day, paced to the usage window

Rules
- Start 18:30 today (2026-09-04). One gate's workstream per calendar day.
- After a gate is closed, nothing new starts until the later of: +5 h (the usage
  window refresh) and 18:30 the next day. Long unattended jobs that spend no plan
  usage (graph builds, Docker image prep, retrieval regressions, scale
  measurements) may run in the gap.
- Monitoring: a scheduled wake-up in this session at each start time (CronCreate),
  plus a wake-up 5 h after each gate closes that only *checks*, never starts. If
  the session is closed, this plan file + the memory note carry the state and
  "go" resumes at the next slot.
- Every day ends with: tests green, commit + push, plan file checkbox ticked,
  dashboard/board republished if numbers changed.

Day plan (each day = the workstream(s) that close that gate; heaviest usage days flagged)

| day | date 18:30 | gate | work | plan-usage spend |
|---|---|---|---|---|
| 1 | Sep 4 | G10 prerequisites | WS0 ops baseline; WS1 `mcp` arm built and smoke-run on 3 tasks | light (~3 agent runs) |
| 2 | Sep 5 | G7 freshness | WS7 auto-refresh + trace fields; `mcp` arm on the 23-task set, 1 rep | medium (~23 runs) |
| 3 | Sep 6 | G2 retrieval | WS3 co-change signal, regression sweep (no agent runs) | none |
| 4 | Sep 7 | G3 verification | WS6 strong verify-fix + false-green audit on recorded runs | none |
| 5 | Sep 8 | (G1 setup) | WS2 build: SWE-bench Verified sample frozen, Docker images, graphs | none |
| 6–9 | Sep 9–12 | G1 (part) | WS2 Sonnet matrix in daily slices of ≤150 runs; WS11 stats | heavy each day |
| 10 | Sep 13 | G5 test pillar | WS4 coverage ground truth + precision/recall | none |
| 11 | Sep 14 | G6 config/schema | WS5 producers + 50-label sample | none |
| 12 | Sep 15 | G4 appsec | WS10 Joern/Semgrep on CVE-fix set | none |
| 13 | Sep 16 | G8 ranking | WS8 cross-pillar sweep | none |
| 14 | Sep 17 | G1 (Opus) | Opus confirmation slice; miss classifier; CIs | heavy |
| 15 | Sep 18 | G9 scale/platform | WS9 measurements, CI matrix | none |
| 16 | Sep 19 | G10 release | WS12 docs, tag, README gates table | none |

Billing decision (2026-09-04): eval runs stay on the plan, in daily slices of
≤150 runs after 18:30 (days 6–9 and 14 as scheduled). The harness gets a
per-day run cap (`FIXEVAL_MAX_RUNS_TODAY`) so a slice cannot overrun the window.
No API key.

## Schedule (calendar, with eval wall time)

| week | work |
|---|---|
| 1 | WS0, WS1, WS7; start WS2 build (Docker, images, 100-task freeze); WS3 in parallel |
| 2 | WS2 Sonnet matrix running (~4 days wall, 3 streams); meanwhile WS4, WS5, WS10 |
| 3 | WS6, WS8 sweep, WS11; Opus confirmation run; WS9 scale measurements |
| 4 | Fix what the miss classifier says; re-run affected arms; WS12 |

Budget: Sonnet matrix $450–600, Opus $150, small-corpus reruns $60. Hard cap enforced by the harness.

## Verification (how each gate is checked, end to end)

- G1: `python bench/swe/report.py` shows per-arm resolve with CI; `mcp` ≥ `nograph`+5 with CI excluding 0, or turns −25% at ≤ cost; miss table present.
- G2: `python bench/agentctx/regress.py --config <shipped>` = no per-task change; named recall ≥ 0.80 in `compare_configs.py`.
- G3: `bench/fixeval/report` false-green column = 0.
- G4: `python bench/joern/validate_cve.py` ≥ 80% endpoints; `corpus/validate_taint.py --build all` 9/9 + 36/36.
- G5/G6: `bench/coverage/run.py`, `bench/schema/score.py` thresholds.
- G7: eval traces `definition_mismatch_after_edit == 0`.
- G8: sweep table, 0 regressions.
- G9: `bench/scale/report.py` thresholds; CI green on three OSes.
- G10: `scripts/check.ps1` and `.sh` exit 0; tag exists.

## Risks

- SWE-bench Docker on Windows/WSL2: image size (tens of GB) and time; fallback is a Linux box or cloud runner.
- Django-size graphs may expose graphify extraction limits (id collisions at scale); supplement mitigates, but G9 may need upstream fixes.
- The pack may still not beat grep on Sonnet; G1's "or fewer turns at ≤ cost" clause exists so an honest tie with efficiency counts, and a clear loss ends with a narrowed product claim ("large multi-symbol changes"), stated in the README.
- Budget: Django runs are longer; the harness cap and the 60-run checkpoint protect the $600.

## Progress log

| gate | status | closed on | evidence |
|---|---|---|---|
| G10 prerequisites (WS0 + WS1 smoke) | scheduled 2026-09-04 18:30 | | |
| G7 | scheduled 2026-09-05 18:30 | | |
| G2 | scheduled 2026-09-06 18:30 | | |
| G3 | scheduled 2026-09-07 18:30 | | |
| G1 setup | scheduled 2026-09-08 18:30 | | |
| G1 Sonnet slices | scheduled 2026-09-09 .. 12 | | |
| G5 | scheduled 2026-09-13 | | |
| G6 | scheduled 2026-09-14 | | |
| G4 | scheduled 2026-09-15 | | |
| G8 | scheduled 2026-09-16 | | |
| G1 Opus + stats | scheduled 2026-09-17 | | |
| G9 | scheduled 2026-09-18 | | |
| G10 release | scheduled 2026-09-19 | | |

