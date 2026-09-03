# Plan 02 — Agent code-context benchmark: custom graphify vs stock, on a real GitHub repo

**Created:** 2026-09-03
**Supersedes:** the fix-layer-deployment framing of the earlier draft of plan 02.
**Predecessor:** `plans/01-close-research-gaps.md` (COMPLETE)

## What this project actually is

A **code-graph tool for agents**. Its job is to hand an agent everything it needs to go and
perform a fix: the symbol's own code, its signature, its callers and callees to depth *n*,
and the related symbols it must not break. The AppSec fix layer was only ever the first
consumer; it is **not** the product and is out of scope from here on.

**The question to answer:** on a real GitHub repository, with real fix tasks, does this
custom build get an agent to fix-ready context better than stock graphify — and better than
an agent with no graph at all? Answer it with measurements, and be stringent, including
about where the custom build loses.

---

## Phase 0 — Discovery ✅ DONE (verified 2026-09-03; read before any other phase)

Four findings below change the plan. Two of them **contradict claims I previously made**,
and the contradictions are the point: they are why this phase exists.

### F1 — the graph carries NO code. This is the central finding.

Field inventory taken from a real 5,059-node graph (`json.load` + `Counter` over node keys):

```
NODE FIELDS: id, label, _callable, _callable_class, _origin, community,
             community_name, file_type, norm_label, source_file,
             source_location, node_kind, frontmatter
EDGE FIELDS: source, target, relation, _origin, confidence, confidence_score,
             context, source_file, source_location, weight
```

`source_location` is written at `graphify-upstream/graphify/extract.py:337,383,460,…` as
**`f"L{node.start_point[0] + 1}"` — a start line and nothing else.**

So the graph contains **no end line, no extent, no signature, no parameter list, no return
type, no docstring, and no source text**. Every answer it gives an agent is
*name + file + start line*, which the agent must then follow with a file read. **The graph
is a navigation index, not a context provider.** Tree-sitter's `end_point` is available
right there at extraction time and is simply not recorded.

This is the gap the product has to close, and it is shared by **both** builds — the custom
build inherits it. Phase 3 closes it; Phases 1-2 measure honestly without it first.

### F2 — stock graphify already has relation-filtered, depth-limited impact analysis

`graphify affected "X" --relation R --depth N` exists
(`graphify-upstream/graphify/affected.py:190 affected_nodes(graph, seed, *, relations, depth)`).
It even performs **the same member-seeding trick** the custom `blast_radius` does — seeding the
walk with the seed's own `method`/`contains` members so callers bound to a class's method node
are reachable (upstream comment cites their #1669/#1634).

**This retracts my earlier claim that "stock has no counterpart for blast-radius".** It has
a close one. Stock also ships `query --budget N` (token-capped BFS), `path A B`, `explain X`,
`god-nodes`, and a `benchmark` command that measures token reduction vs reading the corpus.

The **real** differences, from reading both implementations:

| | stock `affected` | ext `blast_radius` |
|---|---|---|
| direction | reverse only | `up` / `down` / **`both`** |
| returns | flat list of hits | **closed subgraph** — nodes + all edges *between* included nodes |
| relation filter | yes | yes |
| depth | yes | yes |
| budget signal | — | `estimated_tokens` |

"Closed and self-describing subgraph" vs "flat hit list" is a genuine difference for an agent,
and forward/bidirectional traversal is a capability stock lacks. Both must be **measured**,
not asserted.

### F3 — relation mix on a real graph (why `contains` matters)

```
contains 4256 · calls 780 · re_exports 642 · extends 610 · imports 384
imports_from 111 · references 64 · method 45 · indirect_call 40 · inherits 8 · cites 4
```

`contains` is 61% of all edges — structural nesting, not semantics. `calls` (780) plus
`indirect_call` (40) is the entire call graph. Any "transitive call" claim lives or dies on
those 820 edges, so Phase 2 must report call-edge **recall against real call sites**, not just
traversal depth.

### F4 — the differential harness cannot benchmark a non-Python repo

`bench/harness.py:101 source_dir()` counts only `*.py` and raises otherwise. Irrelevant if the
eval repo is Python; it becomes a blocker the moment a second language is added. Noted, not
fixed here — **out of scope unless a chosen repo needs it.**

### Allowed APIs (verified present — do not invent beyond this)

| API | Source |
|---|---|
| `graphify affected "X" [--relation R] [--depth N] [--graph P]` | stock `--help`; `affected.py:190` |
| `graphify query "Q" [--budget N] [--context C] [--dfs]` | stock `--help` |
| `graphify path "A" "B"` · `explain "X"` · `god-nodes [--top N] [--json]` | stock `--help` |
| `graphify update <path>` · `extract <path> --code-only` | stock `--help` |
| `graphify benchmark [graph.json]` | stock `--help` |
| `blast_radius(data, seed, *, depth, relations, direction, max_nodes)` | `graphify_ext/blast_radius.py:64` |
| `graphify-ext blast-radius NODE [--relation R] [--include-containment] [--json]` | `graphify_ext/__main__.py:55` |
| `graphify_ext.graphio.repo_root_for / resolve_by_location / node_index / edges` | `graphify_ext/graphio.py` |
| tree-sitter grammars incl. `tree-sitter-python` | `graphifyy` dependency list |

### Anti-patterns

- Do NOT claim a capability is unique to the custom build without checking stock's `--help`
  and source first (F2 is exactly that mistake, caught).
- Do NOT add `contains` to default relations (61% of edges — it swamps the result).
- Do NOT derive a repo root with `Path.resolve()` (follows the cache symlink — Bug 7).
- Do NOT make `fastmcp` a hard dependency.

---

## Phase 1 — Eval harness and a task set built from the repo's own history

**Build the measuring stick before building the feature**, so the feature cannot be tuned to
a benchmark invented after the fact.

### Repo choice

One primary repo, **full clone (not `--depth 1`)** — the history *is* the ground truth.
Requirements: real GitHub project, Python (F4), rich commit history with self-contained
function-level fixes, small enough to graph in minutes. Record the exact commit SHAs used.
State the choice and its justification in the harness docstring; if the chosen repo turns out
to have too few clean single-symbol fixes, say so and pick another rather than forcing it.

### Ground truth — from real fix commits, not from me

For each task, from a real bug-fix commit `C` with parent `P`:

- **`G` (ground truth)** = the set of symbols whose bodies `C` modified. Derive by mapping each
  changed hunk's line range in `C` back to its **enclosing function/class** via tree-sitter on
  the file at `P`. Not "files changed" — symbols changed.
- **`E` (entry point)** = where a real report would land the agent: the public symbol named in
  the commit message / issue, or the outermost changed symbol. Chosen **from `C`'s message and
  the repo's issue text, never by looking at `G`**.
- Everything is evaluated against the graph built at **`P`** — the pre-fix state. Building at
  `C` would leak the answer.

**Task selection must be blind to tool performance.** Pick tasks by objective filters (touches
≥ 2 symbols, is a bug/security fix, has a usable entry point), fix the list, *then* measure.
Discarding a task after seeing a bad score is result-fitting; if a task must be dropped, record
which and why.

### Three arms — the no-graph baseline is mandatory

1. **`grep` baseline** — what an agent does with no graph: search for `E`, read the hits.
2. **stock graphify** — `affected` / `query` / `explain` / `path`.
3. **custom build** — `blast-radius` and friends.

Without arm 1 the whole exercise cannot tell you whether *any* graph beats ripgrep.

### Metrics, per task per arm

| metric | definition |
|---|---|
| **recall** | fraction of `G` surfaced |
| **precision** | fraction of returned symbols that are in `G` |
| **tokens** | measured on the actual bytes returned |
| **round-trips** | tool invocations an agent needs to reach that context |
| **actionable?** | did the agent receive **code**, or only names+lines it must go read? |
| **file-reads implied** | follow-up reads needed to obtain the code |

Fix depth, relation set, and token budget **identically** across arms; report them.

### Verification checklist

- [ ] Ground-truth extractor tested on ≥ 3 hand-checked commits — the symbols it names match
      what the diff actually changed
- [ ] Entry points recorded with their source (commit message / issue), provably not from `G`
- [ ] Graphs built at `P`; assert the graph's `built_at_commit` is `P`, not `C`
- [ ] Task list frozen and committed **before** any arm is scored
- [ ] Token counting method stated and identical across arms

### Anti-pattern guards

- Do NOT hand-pick tasks the custom build is good at.
- Do NOT use `estimated_tokens` (chars/4) as the measured token metric — measure real output.
- Do NOT let a task's `E` be chosen with `G` in view.

---

## Phase 2 — Measure the three arms as they are today

Run the frozen task set. Produce the raw table before changing any product code, so Phase 3's
work is provably driven by measured deficits.

Expected from F1: **all three arms score 0 on "actionable?"** — no arm returns code. If stock or
ext surprises us and does return code, that changes Phase 3 entirely, which is why this runs first.

### Verification checklist

- [ ] Every task × arm cell populated, including failures and crashes
- [ ] Cases where `grep` beats both graphs reported prominently, not buried
- [ ] Cases where stock beats the custom build reported prominently
- [ ] Raw outputs saved under `bench/agentctx/` so any number can be traced to its run

### Anti-pattern guards

- Do NOT tune anything during this phase. Measure, then stop.
- Do NOT report an average without the per-task spread — one task can carry a mean.

---

## Phase 3 — Build what the measurement shows is missing: symbol-level context

Scope is **set by Phase 2's results**. Based on F1 the expected work is:

1. **Symbol extents.** Record `end_point` alongside `start_point`. Re-parse the file with the
   tree-sitter grammar graphify already depends on and resolve each node's true extent; do not
   approximate with "next node's start line".
2. **Signature and shape.** Name, parameters, return annotation, decorators, and whether it is
   a method (`_callable_class` already exists) — the call contract an agent needs to change a
   function without reading it.
3. **Code slices.** A `context` command returning, within a token budget: the seed symbol's
   source, plus its depth-*n* callers and callees each with their own slice, ordered so the
   budget is spent on the most relevant first.
4. **Truthful degradation.** Where an extent cannot be resolved, say so per symbol — never emit
   a guessed slice. A wrong slice is worse than no slice.

### Verification checklist

- [ ] Extents verified against the real files: slice boundaries land on the symbol's actual
      first and last line for a hand-checked sample across ≥ 3 languages present in the repo
- [ ] Nested / decorated / async / overloaded definitions covered by explicit tests
- [ ] Unresolvable extents reported, never silently approximated
- [ ] Budget enforcement tested at a size that actually truncates
- [ ] `pytest tests -q` still ≥ 89 passed, 1 skipped

### Anti-pattern guards

- Do NOT re-implement a parser — use the tree-sitter grammars already installed.
- Do NOT emit a slice whose extent was inferred from line arithmetic.
- Do NOT let this phase drift into anything the Phase 2 table did not show missing.

---

## Phase 4 — Re-measure and write the detailed context comparison

Re-run the **same frozen task set** across all arms, now with the custom build's new surface.

Deliverable: **`AGENT-CONTEXT-COMPARISON.md`**, containing

1. the three-arm metric table, per task and aggregate, with spread;
2. **a detailed inventory of exactly what context each arm hands the agent** — verbatim
   output for 2-3 representative tasks, side by side, so the difference is visible rather
   than summarised;
3. transitive-call behaviour at depth 1/2/3 — including where `calls` edges are *missing*
   versus real call sites in the source (F3);
4. every case where stock or `grep` wins;
5. cost: graph build time and disk, since context quality bought with a 10× build is a
   different product than one that is free.

### Verification checklist

- [ ] Same task set, same budgets, same depths as Phase 2 — differences attributable to the change
- [ ] Before/after for the custom arm shown, so Phase 3's value is isolated
- [ ] Verbatim outputs are real captures, not reconstructions
- [ ] Every number traceable to a file under `bench/agentctx/`

---

## Phase 5 — Stringent verdict

Write the honest overall assessment, with the burden of proof on the custom build.

Must state plainly:
- what the custom build does that stock **cannot** — with the measurement, not the intent;
- what stock does **better or equally well** (F2 is already one candidate);
- where **neither** graph beats `grep`, and whether the graph is worth its build cost there;
- the **residual gaps**, including the known stock extraction defect (a `.mjs` file present in
  the manifest with 0 nodes while siblings extract fine) — an extraction gap is a direct
  recall ceiling for any graph tool, so it belongs in this verdict;
- a clear recommendation: is this worth continuing, narrowing, or folding into stock?

Housekeeping to close out in this phase:
- **Freeze Build B.** `crg/` is not packaged (`pyproject.toml` has no `crg` entry) and is not
  deployed. Add `crg/FROZEN.md` recording what it proved and the conditions to revive it;
  keep `crg/verify_*.py` runnable. Do not delete, do not merge.

### Verification checklist

- [ ] Full suite from scratch: `pytest tests -q`, `regression_a`, `bench_b`, `cross_c`, `corpus`
- [ ] Anti-pattern greps clean
- [ ] The verdict names at least one thing the custom build does worse — if it genuinely names
      none, that itself needs justifying
- [ ] `crg/verify_*` re-run, not cited from old logs

---

---

## EXECUTION RECORD — all phases complete 2026-09-03

**Deliverable: `AGENT-CONTEXT-COMPARISON.md`.** Repo: `psf/requests`, full clone, 14 tasks from
its own fix commits, ground truth validated 41/41 positive and 565/565 negative.

| phase | outcome |
|---|---|
| 1 — harness + task set | `bench/agentctx/tasks.py`; 14 tasks frozen before scoring |
| 2 — baseline | 6 arms measured; **grep had the best recall (0.530)** of anything |
| 3 — build | `graphify_ext/symbols.py`, `context.py`, `context` CLI + `context_tool` MCP |
| 4 — re-measure | context arm: same recall, **80 → 0 follow-up file reads** |
| 5 — verdict | written; `crg/FROZEN.md` added; upstream draft prepared |

**Findings that corrected this plan's own assumptions**

1. **`--direction both` is a failed feature.** Identical recall to `up` on 14/14 tasks, 4× the
   tokens, 4× the file-opens. F2 predicted it was a genuine differentiator; measurement says no.
2. **Containment should be on, not off.** The documented default (off, because `contains` is
   61% of edges) costs recall: 0.351 → **0.494** with it on, *and* precision improves. Enabled
   by default in `context_tool` on this evidence; the `blast-radius` default is left alone.
3. **Depth is not the lever; ranking is.** Recall is flat 0.351 from depth 1→4 at an 8k budget
   while cost rises 5×. With the budget removed it climbs to 0.619 at depth 6 — the symbols
   were reachable and were being crowded out. Highest-value remaining work.
4. **The custom build extracts nothing.** It queries stock's graph, so its ceiling is stock's
   extraction — 12% of ground-truth symbols had no node at all.
5. **A better upstream bug than the `.mjs` one**, found by this benchmark: Python methods
   differing only by a leading underscore collide on node id and the public one is dropped.
   Reproduced minimally with a control, re-verified on **0.9.53** (latest).

**Verification run**: `pytest` 104 passed / 1 skipped (was 89/1) · `regression_a` 19/19 ·
`cross_c` 11/11 · MCP server exposes 6 tools.

**Left undone, deliberately:** the relevance ranker (finding 3) — it is a project, not a
follow-up; `triage` still has no `--relation` CLI flag; the `.mjs` 0-node report stays unfiled
for want of a public reproducer.

---

## Standing rules

- **Verify, do not assume.** F2 was an assumption that turned out false; check stock's source
  and `--help` before every uniqueness claim.
- **Measure before building.** Phase 2 precedes Phase 3 for a reason.
- **Ground truth comes from the repo, not from me.**
- **Report what loses.** Ten product bugs were found so far because failures were written down.
- **Zero and "worse" are acceptable findings.** Do not tune toward a flattering number.
