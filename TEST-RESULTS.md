# graphify-ext vs Stock Graphify — Test Results

Answers `graphify-ext-test-cases-and-critiques.md` point by point. Every number
here was measured on this machine against real repositories; nothing is
projected or estimated.

**Scope note.** The differential testing in Part 1 was run entirely in throwaway
sandboxes under `bench/`, before anything was connected; the suites uninstall
hooks and reset the sandbox when they finish. Build A has **since been connected
to `C:\projects\appsec-fix-layer`** — see `CUSTOM-BUILD-GUIDE.md` for what
changed there and how to undo it. Work done after that point (the follow-up
section below) was measured against that live repo, always read-only or against
a copy; the repo is verified untouched after each run.

---

## Summary

| suite | result |
|---|---|
| Section A — regression vs stock | **19/19** |
| Section B — Flask, link mode | **31/31** |
| Section B — Flask, **copy mode** (B11) | **31/31** |
| Section B — **scrapy, 5.8x larger** (B4) | **31/31** |
| Section C — cross-cutting | **11/11** |
| Taint-mapping corpus (critique #2) | **36/36** (24 at the time of this section; +12 boundary cases) |
| Build A unit suite | **89 passed, 1 skipped** (73 at the time of this section; +16 from the follow-up work below) |
| Build B (CRG) suites, re-verified | 14/14 · 11/11 · 16/16 · 25/25 |

**Headline — the number Build A was missing.** Returning to a
previously-visited branch, versus stock's full rebuild:

| repo | stock switch | ext revisit | speedup |
|---|---|---|---|
| Flask (83 `.py` files) | 9.6 s | 5.0 s | **1.9x** |
| **scrapy (485 `.py` files)** | **55.7 s** | **3.2 s** | **17.4x** |

The swapped graph is verified branch-correct in both cases. **The advantage
grows with repo size** because stock's cost tracks the whole corpus while the
revisit is bounded by the diff — so the Flask figure is close to the
worst case, not the typical one.

**Six product bugs were found and fixed** by these tests — and four more by the
follow-up work below, **ten in total**. The suites are worth more than the
numbers: every one was invisible to the 69 unit tests that already passed, and
four produced confidently wrong *security* context rather than an error.

---

## Follow-up: closing the CODE-GRAPH-RESEARCH.md gaps

After the differential testing above, `CODE-GRAPH-RESEARCH.md` reviewed both
builds against the agent-facing code-graph literature and named five gaps. All
five were worked through in `plans/01-close-research-gaps.md`. That work found
**four further product bugs**, every one of them silent — wrong output with a
zero exit code, the same family as everything else this project has turned up.

| # | bug | found by | why it mattered |
|---|---|---|---|
| 7 | **Location guards were inert in the live deployment.** `edge_inject` derived the repo root with `Path.resolve()`, which follows the per-branch cache's `graphify-out` symlink and lands in `.graphify-cache` — a directory holding no source. Every file-reading guard silently did nothing, in exactly the deployment they were written for. | Phase 1 | The boundary fix was effective only in tests |
| 8 | **`triage` passed no repo root at all**, so the guards never ran in the main agent-facing entry point even where the path was right. | Phase 1 | Agent context, specifically |
| 9 | **Module-level findings were blamed on the preceding function.** Measured **2 of 4 boundary positions mis-attributed**: a hardcoded secret or a taint source in module-scope config code was reported as living inside a function that does not contain it. | Phase 2 corpus | Confidently wrong *security* context |
| 10 | **`from_semgrep` dropped every real finding.** Real Semgrep taint findings carried no `dataflow_trace` in **9 of 9** cases; the adapter required one and emitted zero edges, silently. | Phase 5 at-scale | The toy-corpus/real-repo gap, exactly as predicted |

Bug 10 is the one worth dwelling on: it was **structurally invisible** to the
24/24 corpus, because the corpus hand-writes findings *with* traces. Only a real
scan of a real repository could surface it — which is the entire argument for
gap #5 being on the list.

The naive fix for bug 10 would have been worse than the bug. Semgrep's JSON
carries no indication of whether a rule ran in taint mode, so treating every
trace-less finding as taint would label ordinary pattern matches as
taint-exposed. Trace-less findings are now mapped only when the caller declares
the rule (`--taint-rule` / `--assume-taint`), and are otherwise reported as
**skipped** — never dropped, never assumed.

### What shipped

| gap | change |
|---|---|
| 1 + 3 | `blast-radius --relation` (repeatable), `--include-containment`, `--list-relations`; `relations` threaded through `triage` so `neighbors` can no longer contradict the radius |
| 2 | Three containment guards; boundary cases **2/4 mis-attributed → 6/6 correct**, pinned as M8 in the corpus and mutation-tested |
| 4 | `graphify-ext-mcp` — five tools over stdio/HTTP, fastmcp an optional extra |
| 5 | Real Semgrep-under-WSL scan of the connected repo; adapter fixed |

### A measured correction to the "token-bounded" claim

`--max-nodes` is a poor proxy for context budget. On a hot node in the connected
repo the default 2-hop bidirectional walk returns 50 nodes and **~14,700
tokens** — while the 500-node cap never fires, so `truncated` stays `false` and
the caller gets no signal. The radius now reports `estimated_tokens`, and
relation filtering is the real lever: the same walk narrowed to `calls` costs
**~4,200 tokens (3.5x less)**.

---

## Part 1 — Test cases

### Section A — regression (19/19)

Run first, on the principle that a caching layer on top of a broken base
extraction just caches the wrong answer faster.

| case | result |
|---|---|
| **A1** commit-triggered incremental, stock vs ext | Graphs **identical** — 1503 nodes / 2541 edges on both sides, compared with upstream's own `watch._canonical_graph_for_compare` rather than a comparator of my own devising. Injected `EXTERNAL` edges excluded, as specified. |
| **A2** `git checkout -b` (HEAD unchanged) | No rebuild, under **both** stock and ext hooks |
| **A3** `git checkout -- <path>` | No rebuild, under both |
| **A4** `query` / `explain` / `god-nodes` before any ext command | Byte-identical output; `graphify-out` still a real directory, proving the slot machinery is inert until `swap` is invoked |
| **A5** `graphify update .` with no ext hooks | Exits 0, documented success message, no slot side effects |

A2/A3 each carry a **positive control** — a genuine branch switch that *must*
fire. Without it, "no rebuild fired" would pass vacuously if the hook were
simply broken. The control redirects `GRAPHIFY_OUT` to a throwaway directory so
the rebuild it legitimately spawns cannot disturb later cases.

### Section B — per-branch caching (31/31 link · 31/31 copy · 31/31 scrapy)

Flask sandbox, 83 `.py` files → ~1500 nodes / ~2540 edges. Final run, link mode,
all product fixes in place:

| case | result |
|---|---|
| **B1** cold full build, ext vs stock (same code path) | 11.2 s vs 10.4 s = **1.08x** — indistinguishable, as it should be for the same builder |
| **B2** stock branch switch (warm) | **9.6 s**, with no revisit speedup (confirming stock has no cache) |
| **B3** ext branch revisit | **5.0 s → 1.9x faster**, and branch-correct |
| **B5** commit incremental, stock vs ext | 4.7 s vs 5.4 s (**+0.7 s**) |
| **B6** disk footprint | stock output 3.4 MB; cache 4.7 MB/branch (**1.38x per branch**) |
| **B7** history rewrite | full rebuild, with the ancestry precondition asserted separately |
| **B8** detached HEAD | full rebuild into a `@detached` scratch slot |
| **B9** long-diverged branches | **no false "cache invalid"**, caches correctly isolated |
| **B10** corrupted `graph.json` / `manifest.json`, **with and without pending changes** | graceful fallback, healthy graph, no crash |

**The Flask speedup is run-dependent: 1.5x, 1.9x and 2.4x across three valid
runs.** That spread is machine noise, not a changing product — which is exactly
why the larger repo below matters more than any single Flask figure. On a repo
this small the fixed overhead graphify pays on every rebuild (detect, cluster,
analyse, report) dominates the extraction the cache actually avoids.

**B3 is branch-correct, not just fast**: after swapping, the feature-only
symbol appears on `feature` and is absent from `main`.

**B7 and B9 are asserted, not inferred.** B7 first proves the cached base
commit is genuinely *not* an ancestor of the rewritten HEAD, so a pass cannot
come from some unrelated cause producing the right log line. B9 first proves
the two branches are genuinely diverged (neither an ancestor of the other) —
the case most likely to produce a false invalidation, since divergence is not
a history rewrite and the two must not be conflated.

#### B4 — larger repo: the gap widens sharply

Repeated on **scrapy — 485 `.py` files, 5.8x Flask** — producing a 9,300-node /
24,500-edge graph (26.7 MB) against Flask's ~1,500 nodes (3.4 MB). **31/31.**

| | Flask (83 files) | scrapy (485 files) |
|---|---|---|
| Stock branch switch (full rebuild) | 9.6 s | **55.7 s** |
| Ext branch revisit | 5.0 s | **3.2 s** |
| **Speedup** | **1.9x** | **17.4x** |

This is the result the critique demanded, and it confirms the *mechanism*, not
just the outcome: **stock's cost scales with the whole corpus (9.6 s → 55.7 s)
while the ext revisit does not — it went slightly down (5.0 s → 3.2 s)**,
because it is bounded by the diff. Had the gap failed to widen, the incremental
path would not have been scaling as designed.

Other scrapy numbers: commit-triggered incremental **25.9 s vs stock 26.7 s**
(ext marginally faster — i.e. within noise, no regression on the common path);
disk **27.3 MB per branch against a 26.7 MB stock output = 1.02x**, so a branch
costs essentially one graph with no hidden overhead. B7, B8 and B9 pass
identically on the larger repo.

**One honest caveat on first-visit cost.** B1 reports ext's cold first visit at
80.1 s against stock's 58.4 s on scrapy, but those are not like-for-like: the
harness seeds a graph before timing stock's `_rebuild_code`, so stock runs with
a warm AST cache while ext's first visit runs cold. Against stock's own *cold*
build (54.7 s) the gap is 1.46x. Either way this is a **one-time per-branch
cost**; the recurring branch-switch cost is the 3.2 s figure. Worth tightening
the B1 methodology if first-visit latency ever becomes the thing you care
about.

#### B11 / critique #4 — copy mode is equally correct

Copy mode previously could not be tested on demand: it is only reached when
links fail, which is not a thing you can arrange. A `GRAPHIFY_EXT_LINK_MODE=copy`
switch now forces it, and **the entire B-series passes identically in both
modes — 31/31 each**. Copy mode is a verified-safe fallback, not merely a
slower one.

Its cost is real but modest. Against the same 9.6 s stock branch switch, an ext
revisit costs **5.0 s in link mode (1.9x) and 6.2 s in copy mode (1.6x)** — so
copy mode gives up roughly a quarter of the benefit. Its cache is also slightly
larger (5.6 MB vs 4.7 MB per branch) because the active output exists in two
places at once.

### Section C — cross-cutting (11/11)

| case | result |
|---|---|
| **C1** `graphify-out` resolution | Tracks the active branch across repeated switches; a **deleted slot** is rebuilt rather than served as a dangling pointer |
| **C2** concurrent swaps | Both succeed (exit 0/0), graph well-formed, correct slot active — **after a real bug fix, below** |
| **C3** `GRAPHIFY_SKIP_HOOK=1` | Genuinely suppresses both post-checkout and post-commit, with a control proving the hook fires when unset. **Benchmark isolation under this flag is trustworthy.** |
| **C4** `PYTHONHASHSEED` | Pinned runs give identical community assignments across 2002 clustered nodes. Three unpinned runs did **not** vary on this repo — so the pin is unproven here, though networkx community ordering is hash-sensitive by construction. Keep it; it is free. |

---

## Part 2 — Critiques

### #1 — Build A had no real-repo timing data — **CLOSED**

Now measured on two repositories, with correctness verified alongside every
timing: **1.9x on Flask** (5.0 s vs 9.6 s) and **17.4x on scrapy** (3.2 s vs
55.7 s) in link mode; 1.6x on Flask in copy mode. See B3 and B4.

The honest reading is not a single number but a slope: the benefit is modest on
a small repo, where graphify's per-rebuild fixed overhead dominates, and large
on a realistically-sized one, where extraction dominates and the cache avoids
it. **Requirement 1 is justified for repos worth caching, and barely worth the
machinery for tiny ones.**

### #2 — Taint-mapping validation — **CLOSED, and it found two real bugs**

A purpose-built corpus (`corpus/`) with documented ground truth: three true
positives (including a **cross-function** flow whose two ends must resolve to
*different* nodes) and four true negatives — sanitized, constant-fed,
unreachable, and inert — each of which a naive "this function calls a sink"
check would wrongly flag.

Ground truth is expressed as (function, call) pairs resolved through the AST at
run time, never as hardcoded line numbers, so editing the corpus cannot
silently invalidate the expectations.

**Result: 24/24 across both builds** — after fixing the two defects it exposed
(below). Scope is stated honestly in `corpus/README.md`: neither build *detects*
taint, so what is validated is mapping fidelity and the exposed-subset filter,
not an analyzer's accuracy.

### #3 — `verify-fix` threshold undefined — **CLOSED (documentation)**

Specified exactly in `CUSTOM-BUILD-GUIDE.md`: fingerprint fields are
`source`/`target`/`relation`/`confidence`/`origin`; everything else is ignored
(notably `source_location`, so a call moving lines is *not* a delta); tolerance
is **zero**; scope is node-local; exit codes 0 / 2 / 1. The consequence table
spells out the non-obvious cases — including that **renaming the fixed function
always trips the check**.

### #4 — Copy mode unverified for correctness — **CLOSED**

See B11 above: 31/31 in both modes, on both repos.

### #5 — No version compatibility contract — **CLOSED (documentation)**

`CUSTOM-BUILD-GUIDE.md` now enumerates every coupling point for both builds and
classifies each as loud-on-break or silent, alongside the enforcement that
already exists (hook-template splice failure, version-stamped cache
invalidation, and `verify_config.py` asserting its own precondition). The
genuine remaining gap is identified plainly: **graph/DB schema drift would
produce wrong results rather than an error**, which is why the operational rule
is to re-run the full suites after any upstream upgrade.

---

## Bugs found and fixed

None of these were caught by the 69 unit tests that already passed.

| # | bug | found by | fix |
|---|---|---|---|
| 1 | A corrupt slot `graph.json` passes the exists/size checks, fails the reconcile, and left `swap` exiting 1 with a malformed graph in place | B10 | Clear the slot and fall back to a full rebuild |
| 1b | **A corrupt cached graph was served unvalidated** when nothing had changed: the zero-changed fast path returns without touching `graph.json`, so `swap` exited 0 and every consumer read `{ this is not valid json` | B10 **on the larger repo only** | Validate the cached graph before taking the fast path |
| 2 | Location lookups resolved to **docstring nodes instead of functions** — graphify emits a non-callable docstring node one line *below* each function, so nearest-preceding-node attached every finding to prose | Corpus M1/M2 | Prefer the nearest preceding **callable**; fall back to any node only if none precedes |
| 3 | A line **past the end of a file** silently resolved to the last definition, turning a bogus finding into a plausible-looking edge | Corpus M7 | Bound by real file length (Build A) and by the File node's `line_end` (Build B); unresolvable refs are now reported, never downgraded to a coarser file-level edge |
| 4 | **Concurrent swaps raced inside graphify's own graph writer** (`WinError 2: .graph.tmp.json -> graph.json`), failing nondeterministically | C2 | Cross-process swap lock held across activate + rebuild + stamp |
| 5 | Lock contention was indistinguishable from a corrupt slot, so it would clear the cache and rebuild for nothing | C2 | `block_on_lock=True` — a swap waits, matching `graphify update`'s posture |

Bugs 2 and 3 are the ones that mattered most: both produced **confidently wrong
security context** rather than an error.

Bug 1b deserves its own note on test design. B10 **passed on Flask and failed
only on scrapy** — not because the products differ, but because on Flask the
scenario happened to leave file changes pending, so the swap went through the
rebuild path (which fails loudly and recovers) instead of the fast path (which
does not). The Flask pass was luck. B10 now sets up the zero-changed case
explicitly rather than relying on incidental state, and **running the same
suite against a second repository is what exposed it** — a good argument for
B4 being more than a scaling measurement.

### Bugs in the tests themselves

Worth recording, because two of them produced *plausible but invalid numbers*:

- **The benchmark baseline was captured at run time.** Since the B-series
  creates branches and amends commits, the second run reset to a commit the
  first run had polluted — `app.py` ended up containing the probe twice. This
  produced a "2.7x faster" result and then a "1.0x" result on the same code;
  **both were discarded.** Fixed with a `bench-base` tag pinned at clone time
  plus an `assert_pristine` guard.
- **The default branch was assumed to be `main`.** scrapy uses `master`, and
  git checkout failures are captured rather than raised, so the suite would
  have carried on against an unreset tree. Worse, `reset_repo` derived the
  default from *current HEAD* to decide which branches to delete — so when it
  ran while a feature branch was checked out it kept that and **deleted the
  real `master`**. Now recorded at clone time, with `origin/HEAD` as the
  fallback and current HEAD only as a last resort.
- **Source paths were hardcoded to `src/flask/`.** On any other repo the
  "changed" file set would have been empty and every incremental timing would
  have silently measured a no-op.
- **B1 compared different code paths** (`graphify . --code-only`, the extract
  CLI, against ext's `_rebuild_code`), charging ext for work the two paths do
  not share. Now compares against the same-code-path baseline, with the CLI
  number kept as context.
- **B9 read the graph without swapping first**, so it inspected the previous
  branch's slot.
- **B2's "flatness" check measured machine noise**, not caching behaviour.

---

## Methodology and caveats

Read these before quoting any number.

- **Cross-run absolute times are not comparable.** The copy-mode run recorded a
  stock full build at 9.1 s where the link-mode run recorded 20.7 s — the same
  operation, on the same repo, differing purely by machine load. Only
  **within-run** ratios are meaningful, which is why the speedups above are
  computed against each run's own stock baseline.
- **Minimum, not mean.** Wall-clock timings on this machine vary roughly 2x
  (antivirus, scheduler). The minimum of several runs is the sample least
  polluted by unrelated work; medians are reported alongside.
- **Cold vs warm matters.** graphify keeps a content-hash-keyed AST cache
  inside its output directory. B1 wipes it before each build (cold: ~21-25 s);
  B2/B3 leave it in place (warm: ~10-12 s). The branch-switch comparison is
  **warm vs warm**, which is the fair real-world case — stock's full rebuild is
  already partly accelerated by that cache.
- **All measurements are AST-only** (`--code-only` / `_rebuild_code`), the path
  the hooks actually run. Semantic extraction would make timings depend on
  network latency.
- **Hooks were uninstalled for all timing runs**, and C3 independently confirms
  `GRAPHIFY_SKIP_HOOK=1` works, so benchmark isolation is verified rather than
  assumed.

---

## How to re-run

```bash
# Pristine, tagged baselines (records the repo's real default branch)
python bench/setup_sandbox.py --name sandbox-a
python bench/setup_sandbox.py --name sandbox-big \
       --repo-url https://github.com/scrapy/scrapy.git

python bench/regression_a.py                                    # Section A
python bench/bench_b.py --mode link --iterations 3              # Section B
python bench/bench_b.py --mode copy --iterations 3              # B11
python bench/bench_b.py --sandbox bench/sandbox-big \
       --mode link --iterations 2                               # B4 (scaling)
python bench/cross_c.py                                         # Section C
python corpus/validate_taint.py --build both                    # critique #2
python -m pytest tests -q                                       # Build A units
```

Runtimes on this machine: Flask Section B ≈ 12 min, scrapy Section B ≈ 35 min,
everything else a few minutes each.

Every suite prints PASS/FAIL per acceptance criterion and restores the sandbox
to its pinned baseline. Per critique #5, re-run all of them after any upstream
version change.
