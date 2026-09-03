# Graphify-Ext: Correctness Roadmap (Retrieval-Reliability Only)

**Supersedes** the draft at `~/Downloads/graphify-ext-correctness-roadmap.md`.
Revised 2026-09-03 after verifying its checkable premises against the source and
measuring two of its open questions. Four structural changes, marked **[REV]**.

**Scope:** graph/retrieval correctness — ranking, taint-reachability, and the two
unbenchmarked pillars (test coverage, config/schema). Excludes productization
(multi-tenant, multi-language, compliance) — separate track.

**Framing:** current verdict is "not sufficient alone for autonomous remediation."
Each phase is a gate; the verdict doesn't move until all gates pass, because an
agent operating on incomplete or wrongly-ranked context fails silently.

---

## Evidence discipline (applies to every phase)

Three rules, adopted because each was violated at least once already:

1. **Every comparison states its token budget in the same table row.** The
   "0.565 beats grep" claim was measured at a doubled 12k budget; at matched 6k
   budget nothing beats grep. Budget-matched or it doesn't ship.
2. **Per-task breakdown, never a bare mean.** The containment lift was 3 of 14
   tasks with 11 unchanged — the aggregate read as a uniform 41% gain.
3. **The small-n caveat applies symmetrically.** n=14 is not trustworthy for a
   default-flip in one direction and untrustworthy in the other. Any interim
   result on the current corpus carries the caveat explicitly, including results
   that support a change we want to make.
4. **The caveat travels with the number, permanently — in the same sentence.**
   Not in a footnote, not in a "limitations" section at the bottom, and not only
   when someone asks. If a metric has a methodological weakness, the weakness is
   stated wherever the metric is stated, including in summaries and chat replies.
   **Checklist item for any doc or reply reporting numbers: does every figure
   carry its caveat inline?** This exists because the failure is drift, not
   dishonesty — "0.565 beats grep" was true in its table (which said *budget 12k*)
   and false the moment it was repeated one sentence later without the budget.
   Prior art worth copying: `code-review-graph` declines to quote a number for
   its co-change eval mode at all while that mode is broken, rather than
   publishing a number it cannot stand behind.

---

## Phase 0 — Eval infrastructure (prerequisite for *validation*, not for *fixes*)

**[REV] Gating clarified.** Phase 0 gates the *validation* of a change and any
default flip. It does not gate fixing a defect already identified by inspection —
otherwise a known-wrong heuristic stays in the agent's path for as long as
corpus-building takes. Phase 1's ordering fix therefore starts in parallel, and
is re-validated here.

**Requirements**
- Expand to ≥50 fix commits across ≥3 repos of different shapes (small
  single-package, large monorepo, one non-Python codebase). **Note:** the
  differential harness currently selects source files by `*.py` only
  (`bench/harness.py:101`), so a non-Python repo needs that generalised first.
- Per-task results reported for every change, not aggregates.
- **Ground-truth completeness screening.** For a sample, check whether the fix
  commit is itself complete — no follow-up commit touching the same symbols, no
  CVE amendment. Partly automatable: search later history for commits touching
  the same symbol set. Annotate or exclude; do not score recall against a target
  that is itself an incomplete fix.
- Budget discipline per the rules above.

**Acceptance**
- Corpus size and diversity documented; single-repo results never again the basis
  for a default-flip.
- Regression suite reruns the benchmark on any change to ranking, containment or
  edge generation, surfacing per-task diffs automatically.

### "Is your ground truth circular?" — no, and here is the one-line answer

A fair question to expect, because a comparable tool fails it: `code-review-graph`
grades its blast-radius F1 against a ground truth **derived from the same graph
the predictor walks**, and flags that circularity itself.

**This benchmark's ground truth is independent of the graph.** It is the set of
symbols a real fix commit modified, recovered from git history with tree-sitter
against the parent commit — computed without consulting `graph.json` at all. A
graph that returns nothing still scores against the full ground truth; a graph
that invents symbols gets no credit for them. The extractor was validated with
both controls (41/41 symbols contain a changed line; 565/565 non-ground-truth
symbols contain none).

The honest limits are different ones, and they are not circularity:
- **co-change is a proxy** for "context needed to fix", not a definition — a
  symbol the agent should have *read* but not *edited* is invisible to scoring;
- **the target may itself be an incomplete fix** — see the completeness screening
  requirement above, which exists precisely for this.

---

## Phase 0.5 — Test-edge provenance **[REV: new, jumps the queue]**

A live correctness bug, not a research question. Verified in source:
`test_link.py` emits `relation: "tests"` from **two semantically different
paths** — `from_coverage()` (coverage.py dynamic contexts = *this test executed
this line*) and `heuristic()` (test name matches a production symbol name =
*not coverage at all*).

**Neither sets a `confidence` field.** The only distinguishing mark is a
free-text `detail` string (`coverage:<test_id>` vs `heuristic:name-match`), and
`edge_inject` stamps every injected edge with the same `confidence: "EXTERNAL"`
(`edge_inject.py:107`). So an agent cannot distinguish measured coverage from a
name guess without string-parsing `detail`.

That is the dangerous direction of error for this pillar: the agent believes a
fix is covered by a test that never executes it.

**Requirements**
- Distinct confidence per path: coverage-derived `EXTRACTED`, heuristic-derived
  `INFERRED`. Preserve `detail`.
- `context` / `blast-radius` output must surface the distinction where a `tests`
  edge is reported.
- Regression test asserting a heuristic edge can never be emitted as `EXTRACTED`.

**Acceptance** — mixed-source graph where every `tests` edge is attributable to
its method by field, not by parsing prose.

### Schema decision: align with graphify, do NOT lift CRG's

Checked both before implementing, because they look interchangeable and are not:

| | label field | numeric field |
|---|---|---|
| **graphify** (our substrate) | `confidence` = `EXTRACTED`/`INFERRED`/`AMBIGUOUS` | `confidence_score` = float |
| `code-review-graph` | `confidence_tier` | `confidence` = float |

**The field names are inverted.** Since injected edges are merged into graphify's
own `graph.json` and read back by stock `graphify query`/`explain`, adopting CRG's
naming would put a float where every graphify reader expects a label. Align with
graphify.

What CRG does independently confirm is the *discipline*: `scoped_resolver.py:44`
tags rewritten edges `INFERRED` "to distinguish a resolved edge from an extracted
one". Two implementations reaching the same rule is a reason to keep it, not a
reason to copy the spelling.

**Status: implemented.** `test_link` emits `EXTRACTED` (coverage) / `INFERRED`
(heuristic); `edge_inject.apply()` honours a producer's label instead of
hardcoding `EXTERNAL`; `from_semgrep`'s numeric severity moved from `confidence`
to `confidence_score` (it had been emitting a float under the label key — the
same one-key-two-meanings defect, in a second place). Not yet regression-tested.

---

## Phase 1 — Ranking under truncation (blocking correctness bug)

Highest-severity open item. **[REV] Diagnosis corrected.** The original draft
suspected insertion-order truncation. Verified: the sort key is

```
(is_seed, blast_depth, relation_class, label)      # context.py:109-117
```

Depth-major, then relation class, then **alphabetical by label**. Measured at the
truncation boundary across 12 tasks: **3 of 12 boundaries were decided purely
alphabetically**; the rest were decided by relation class.

So the alphabetical tie-break is real but narrow. **The larger defect is one
level up: depth is the primary key**, so a depth-3 `calls` edge always loses to a
depth-1 `imports` edge — backwards for remediation.

### Candidate scoring function — a hypothesis to test, not a settled fix **[REV]**

"Demote depth" is a diagnosis; this is the spec. Replace the lexicographic key
with a **single multiplicative score**, so depth and relation trade off rather
than one lexically dominating the other:

```
score(n) = relation_weight[via(n)] × decay^depth(n)      # decay ≈ 0.5
relation_weight: calls/indirect_call 1.0 · method/contains 0.7
                 extends/inherits/implements 0.6 · references/uses 0.4
                 imports/imports_from/re_exports 0.2
```

Properties this is designed to have, each falsifiable:

- a depth-3 `calls` (1.0 × 0.125 = **0.125**) outranks a depth-1 `imports`
  (0.2 × 0.5 = **0.100**) — the case that motivated the change;
- a depth-8 `calls` (1.0 × 0.0039) does **not** outrank a depth-1 `imports` —
  the failure mode a relation-major lexicographic order would introduce, and the
  reason this is a product and not a second sort key;
- ties broken deterministically and **disclosed** (node degree ascending — prefer
  specific symbols over hubs — then node id), never silently alphabetical.

Weights and `decay` are **parameters to be fitted and reported**, not constants
to assert. The hypothesis is falsified if recall-within-budget fails to improve,
or if per-task diffs show regressions concentrated in any task shape.

**Requirements**
- Truncation ranks by score, not by traversal order.
- Once Phase 2 lands, taint-reachability enters as a term with enough weight that
  a tainted-reachable symbol is never truncated ahead of an untainted one at the
  same distance.
- `omitted` entries need a **severity field**: `truncated_high_rank`,
  `truncated_low_rank`, `excluded_out_of_scope` are different failure modes.
- New metric: **recall among symbols a budget-unconstrained pull would include** —
  isolates ranking failure from traversal/coverage failure. (Measurable today:
  the unconstrained numbers are 0.351 → 0.619 across depths 2–6.)

**Acceptance**
- On the Phase 0 corpus, recall-within-budget does not degrade as depth rises.
- Manual audit of ≥10 truncation events confirms the fix-relevant symbol was not
  in the omitted set, or was ranked below symbols that scored lower on
  independent judgement.

---

## Phase 2 — Taint-reachability as a first-class edge

**Blocked dependency, not schedulable work.** "SISA's taint-analysis pipeline"
is not in this repository and has no interface spec here. Until there is one —
what it emits, at what granularity, and its staleness model — this phase cannot
be estimated. Today taint edges exist only after a manual Semgrep injection and
are absent on a cold graph.

**Requirements** (unchanged from the draft, plus)
- Define the interface before the work: emitted format, per-file or per-repo,
  and what invalidates a taint edge.
- Taint edges computable and cached in the normal incremental-build cycle.
- Define what taint-reachability *does* in output: rank term, hard filter, or a
  `--taint-only` mode.
- Defined degraded mode for cold start — not silent absence.

**Acceptance** — taint edges on a cold build without a manual step; a quantified
precision/recall improvement over blast-radius alone on the taint-relevant subset
of the Phase 0 corpus; documented cold-start behaviour.

---

## Phase 3 — Test-coverage pillar (never benchmarked)

**[REV] Feasibility constraint.** The proposed ground truth — run coverage.py on
the pre-fix commit — assumes the historical suite still runs. Much of `requests`'
history is Python-2 era and will not. Either restrict to commits whose suite runs
under a current interpreter, or choose a repo with a runnable historical suite,
and state which.

Edge semantics are partly answered by Phase 0.5: `--coverage` means *executed at
least one line of*; `--heuristic` means *names matched*. Neither means *asserts on
the behaviour of*, which is the semantics an agent actually wants for "is this
fix covered" — that gap should be stated in the docs rather than closed silently.

**Acceptance** — coverage-edge precision/recall against a real coverage-tool
baseline; false positives reported separately, as the more dangerous direction.

---

## Phase 4 — Config/schema pillar **[REV: split into 4a build / 4b measure]**

**The draft treats this as a measurement gap. It is mostly a build gap.** Verified
in `config_link.py`: the pillar is **environment-variables only**.

- Read side: `os.environ` / `os.getenv` (Python) and `process.env` (JS/TS). No
  other language.
- Definition side: `.env*`, `docker-compose*`, `Dockerfile*`, GitHub workflow
  YAML, `k8s/*`, `helm/**`.
- **No ORM models, no JSON Schema, no app-config (YAML/TOML/INI) key→code
  linkage.** The "schema" half does not exist.

For appsec remediation this is likely load-bearing rather than optional: a large
share of injection and validation defects sit exactly at the ORM/schema boundary.
The earlier at-scale result (0 edges on the connected repo) is consistent with
env-only scope and was correct for what it measured.

### Phase 4a — build schema linkage
Size honestly; this is new capability, not a measurement pass. Decide the target
set (ORM models first is the appsec-relevant choice), define edge semantics
before implementing, and expect per-format fidelity to vary.

### Phase 4b — measure completeness
Only once 4a exists. Completeness ground truth by manual enumeration of
config/schema touchpoints on a commit sample; recall-style metric matching the
rigour applied to blast-radius; per-format breakdown, never averaged across
formats.

---

## Phase 5 — Cross-pillar integration

Unchanged. Single ranking function arbitrating across all four pillars, not four
concatenated lists. Re-run the Phase 0 benchmark end-to-end, budget-matched
against the blast-radius-only baseline. **Verdict re-assessed only here** —
"sufficient for autonomous remediation" is not claimed before these numbers exist.

---

## Tracked item — upstream extraction ceiling **[REV: new]**

No phase above moves this number, and perfect ranking over a graph missing
symbols still misses them.

**Measured** across 14 checkouts of `psf/requests`: **12,379 definitions found by
tree-sitter, 711 with no graph node at their definition line — 5.7%.** (The 12%
quoted earlier was the rate within the 25-symbol ground-truth set; 5.7% is the
repo-wide rate. The smaller number is the more honest one.)

**The loss is systematic, not random:**

| cause | share of misses |
|---|---|
| nested functions (function-in-function) | 50% |
| "plain" top-level/method definitions | 27% |
| dunder methods | 20% |
| decorated | 2% |
| leading underscore | 1% |

Of the non-nested, non-dunder remainder, **46% (97 of 211) collide on a slugged
node id with a symbol that *is* present** — and the collision classes are wider
than the leading-underscore case originally reported upstream:

- `session()` @ L705 vs the `Session` class @ L270 in `requests/sessions.py` — **case collapse**
- `__get_module` vs `_get_module` — underscore collapse
- the same name defined twice under conditional branches

Filed upstream as [Graphify-Labs/graphify#3302](https://github.com/Graphify-Labs/graphify/issues/3302),
with a follow-up comment carrying the broader measurement.

**Investigation step before any local repair**
1. Establish whether the loss correlates with security-relevant code, or is
   merely dense in vendored compat shims — `six.py` alone contributes heavily and
   inflates the collision share. Re-measure with vendored paths excluded.
2. Decide nested-function policy: is 50% of the loss an upstream defect or a
   deliberate design choice? That determines whether it is reportable at all.
3. Only then consider a local post-extraction repair pass — and **a repair pass
   needs its own correctness gate**, since naive de-collision can mint new false
   edges (re-pointing an existing edge at a newly created node) while fixing a
   missing one. Any repair ships with a test that asserts edge endpoints are
   unchanged for symbols that were never colliding.

---

## Deliberately excluded

Multi-repo/multi-language support, multi-tenant deployment, compliance,
pricing/packaging, and known build-maturity gaps (verify-fix exit-code-2
threshold, copy-mode correctness beyond speed, upstream-version compatibility
contract). Productization and operational hardening, not retrieval correctness.

## Not a benchmark to beat: graphify's published numbers

Upstream graphify publishes rigorous evals (LOCOMO, LongMemEval-S, judge-validated
against a second grader). **Those measure conversational-memory recall and
academic QA — not code-graph traversal**, and nothing in them speaks to the
traversal this project depends on.

Both of these are true at once and must not be blurred:

- graphify's parent runs careful evaluation on *that* task;
- on *this* task, measured first-hand, stock graphify's `affected` scores 0.256
  and `explain` 0.387 — both below plain ripgrep's 0.530.

A rigorous benchmark on an unrelated task does not vouch for traversal quality
here. Do not cite the former as evidence about the latter, and do not treat those
numbers as a bar this project is trying to clear — different task, different
metric, not comparable.

**One honest limit on the whole track:** every metric here is retrieval-side.
None of them measure whether the agent, given the context, produces a correct
fix. That is deliberate given the stated scope, but retrieval quality is a proxy
for fix quality, and the proxy should not be mistaken for the thing.
