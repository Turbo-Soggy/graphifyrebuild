# Graphify-Ext: Correctness Roadmap (Retrieval-Reliability Only)

**Supersedes** the draft at `~/Downloads/graphify-ext-correctness-roadmap.md`.
Revised 2026-09-03 after verifying its checkable premises against the source and
measuring its open questions. Changes marked **[REV]**; the largest are that
Phase 1's premise was withdrawn on measurement, Phase 2 was scrapped on a scope
change, and the goal was reframed from completeness to disclosure.

**Scope:** graph/retrieval correctness — ranking and the two unbenchmarked
pillars (test coverage, config/schema). Taint-reachability was **scrapped**
(Phase 2). Excludes productization (multi-tenant, multi-language, compliance) —
separate track.

**Framing — the target is disclosure, not completeness.** No code graph reaches
zero-miss completeness: static analysis has permanent blind spots (dynamic
dispatch, reflection, framework magic) and this one additionally inherits an
upstream extraction ceiling. "The graph never misses" is not an achievable
engineering target and must not be the thing standing between a user and a
correct fix.

The achievable target is: **the graph never lies about what it is missing.**
Every gap disclosed, correctly, with a reason the agent can act on.

This is load-bearing rather than consolatory, because of where the guarantee
sits. With a verification layer after the agent (re-run the finding, delta-scan
the diff, run the tests), a *disclosed* gap costs a retry, while an *undisclosed*
gap produces a confident wrong fix built on context the agent believed was
complete — the one failure mode verification cannot cheaply prevent, because the
agent never attempted the right file. So gates below are stated as disclosure
properties wherever possible, and recall targets are treated as "good enough that
most fixes clear verification cheaply", never as completeness claims.

**Naming note:** `graphify-ext verify-fix` was renamed to **`edge-diff`**
(`edge_diff_tool` in MCP; `verify-fix` kept as a warning alias). It diffs a
node's graph edges and runs no scanner and no tests. The name `verify-fix` is
reserved for the customer-facing guarantee, which is a stronger claim and is
currently unbuilt.

---

## Evidence discipline (applies to every phase)

Eight rules, each adopted because it was violated at least once already — most
of them in the same session that wrote them down:

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
5. **"Arbitrary" and "harmful" are different claims, and so are "principled" and
   "performs at least as well."** Replacing an arbitrary rule with a justified
   one is a design improvement and says nothing about behaviour until measured.
   The alphabetical tie-break was replaced on exactly this reasoning, called a
   "safe independent win" by everyone, and turned out to be the ONLY source of a
   -0.071 recall regression — while the elaborate scoring function it shipped
   alongside contributed exactly zero. Any change justified on principle rather
   than on a number is a change that has not been evaluated yet.
6. **A plausible number that has not been re-derived is a liability, not a fact —
   and in this codebase specifically, that is a recurring risk category.** Four
   instances in one session, each surviving until re-derived from scratch:
   (i) "0.565 beats grep" (missing budget caveat); (ii) a -0.071 regression
   measured against a baseline that never existed (three stacked config
   mismatches); (iii) three successive wrong mechanisms for that regression;
   (iv) a 46% id-collision share that was really 100%, because a classifier
   tested "is a dunder" before "is nested" and mis-sorted every closure dunder.
   None was bad luck; each was a number produced by a script whose *categories
   or conditions* were never checked against a second derivation. Rule: any
   breakdown or comparison that will be quoted gets re-derived independently —
   different script, different precedence, or a control — before it leaves the
   session. A number quoted once is a number that will be quoted again.
7. **Any scoped/fast check needs a periodic full-scope backstop.** "Scoped" and
   "sound" are different axes; scoped checks are chosen for speed. This applies
   in at least three places and should be assumed to apply wherever the trade-off
   recurs: incremental graph rebuild vs full rebuild; diff-scoped SAST (a fix
   that changes a shared helper's contract can make an *unchanged* caller newly
   vulnerable, and the diff scan will not see it); and scoped test runs, whose
   scope depends on a test-coverage pillar that has never been accuracy-checked.
8. **A parameter described as "fitted" or "tunable" blocks shipping until it has
   actually been fit.** Satisfying a couple of hand-picked inequalities is not a
   fit. This rule exists because `decay = 0.5` shipped exactly that way: chosen
   to make two illustrative comparisons come out right, documented as
   "a parameter to be fitted, not a constant to assert", and then merged with a
   green suite — where the tests confirmed *the code implements the formula*,
   never *the formula is correct*. A passing suite around an unfitted parameter
   reads as validation and is not. Fit against the benchmark, report the sweep,
   or do not ship the parameter.

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

## Phase 1 — Ranking under truncation — **RESOLVED, and not as predicted**

**[REV 3] Every mechanism proposed for this phase was wrong, including two of
mine, and the cause was the one change everybody called safe.**

### What was actually wrong: the tie-break

Four-way isolation at depth 3 / budget 12,000 / `max_nodes=800`, one variable at
a time:

| rank function | tie-break | recall |
|---|---|---|
| legacy `_REL_RANK` | alphabetical (original) | **0.565** |
| weighted score | alphabetical | **0.565** |
| weighted score | degree descending | 0.530 |
| legacy `_REL_RANK` | degree ascending + id | 0.494 |
| weighted score | degree ascending + id (shipped) | 0.494 |

**The rank function contributes exactly zero. The tie-break is the whole
-0.071.** The relation weights are exonerated (both rank functions score
identically under both tie-breaks), and so is the unknown-relation weighting.

### Why alphabetical was winning — and it was not luck

graphify labels methods `.name()` with a **leading dot**, and `.` sorts before
every letter. Sorting by label was therefore an accidental **members-first**
rule. On a corpus whose ground truth is reached predominantly through
containment chains, members-first is exactly the right bias, which is why an
"arbitrary" rule beat two principled ones.

**Shipped:** the tie-break is now `members-first, then node id` — explicit,
disclosed, deterministic, and matching the best measured result:

| config | legacy | shipped | delta |
|---|---|---|---|
| depth 2, budget 6,000 | 0.494 | 0.494 | +0.000 |
| depth 3, budget 6,000 | 0.494 | 0.494 | +0.000 |
| depth 3, budget 12,000 | 0.565 | 0.565 | +0.000 |

`build_context(order="legacy")` is retained so any future ordering change can be
A/B'd in-code against the real prior behaviour, rather than against a
reconstruction. Reconstructing a baseline is how three config mismatches
(uniform weights, depth 4 vs 3, `max_nodes` 1200 vs 800) got stacked into one
bogus -0.071 that had to be retracted twice.

### The three mechanisms that were wrong, recorded so they are not re-proposed

1. **"Exponential depth penalty punishes containment chains."** Falsified: at
   `decay = 0.95` the depth penalty is nearly gone and the loss is unchanged.
2. **"The weighted score as primary key is the regression."** Falsified:
   reverting to depth-primary did not recover the loss.
3. **"The relation weights / unknown-relation weighting are the cause."**
   Falsified by the isolation table above.

Each survived until the next measurement killed it. The test suite was green
throughout — it only ever proved the code implemented the formula.

### Status

- **Tie-break — RESOLVED.** Members-first, measured equal to the best known.
- **Truncation legibility — SHIPPED.** `omitted` entries carry `score` and a
  `severity` of `truncated_high_rank` / `truncated_low_rank`, where high means
  the dropped symbol scored at least as well as the weakest symbol kept.
- **Relevance scoring — NEUTRAL, retained.** It orders symbols within a depth
  and measurably changes nothing. Kept because it is more legible than an opaque
  rank table, **not** because it was shown to help. It has not earned a claim.
- **Ranking as "the blocking correctness bug" — WITHDRAWN.** Reordering moved
  recall by 0.000 everywhere it was measured. On this corpus the binding
  constraint is reach and extraction coverage, not ordering.

---

## Phase 1 — original framing (kept for reference)


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

## Phase 2 — Taint-reachability — **SCRAPPED 2026-09-03**

Dropped from scope at the user's direction. The project is no longer
appsec-specific: the target is a code graph an agent can rely on for **general**
fixes, and "is this reachable by an attacker" is not part of that.

Shipped code is **retained, not deleted** — `from_semgrep`, the `taints` /
`reaches_sink` relations, and their tests all still work and cost nothing idle.
Scrapping a roadmap phase is free; deleting working, tested code is not, and it
is reversible in the wrong direction. Nothing further will be built on it.

---

## Substrate note (applies to Phases 3, 4 and 5)

**Build A on graphify is the substrate.** `code-review-graph` was frozen
2026-09-03 (`crg/FROZEN.md`) — unpackaged, never deployed, `pyproject.toml` has
no entry for it. Any framing of these phases as "extend what CRG already gives
you" is wrong: CRG's blast-radius, test-coverage and change-risk tooling are
**not inherited**. Adopting any of them means porting onto graphify's
`graph.json`, which is real work, not a banked subsystem.

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

## Tracked item — extraction ceiling — **RE-MEASURED, and it decomposes into two things**

**[REV 4] The earlier breakdown in this document was miscategorised, and the
mechanism of the mistake is the lesson** (see discipline rule 6): the classifier
tested "is a dunder" *before* "is nested inside a function", so every `__init__`
or `__iter__` defined inside a closure was counted as a *dunder* miss. That
inflated the dunder bucket from 4 to 144, and — by leaving mis-sorted symbols in
the denominator — diluted the id-collision share from 100% to a reported 46%.
The wrong number was quoted upstream before it was re-derived; a correction has
since been posted. Recomputed with function-nesting taking precedence, and with a
first-party/vendored split:

| cause | all files | first-party only |
|---|---|---|
| **nested inside a function** | 610 (86%) | 589 (93%) |
| plain / leading-underscore | 82 (12%) | 33 (5%) |
| decorated | 15 (2%) | 10 (2%) |
| dunder (not nested) | 4 (1%) | 4 (1%) |
| **totals** | 711 of 12,379 (5.7%) | 636 of 8,231 (7.7%) |

Two corrections to what was previously written here and reported upstream:

1. **Excluding vendored code makes the rate WORSE, not better** — 7.7% vs 5.7%.
   Vendored code was diluting it, not inflating it. The opposite was assumed.
2. **Id collisions account for 100% of non-nested, non-dunder misses**
   (97/97 all files, 43/43 first-party), not the 46% reported to upstream. That
   earlier figure was depressed by the same miscategorisation.

### Cause 1 — nested functions: a design choice, not a defect

**0 of 610** functions nested inside another function have a graph node. That is
total, so it is deliberate: graphify models module-level and class-level
definitions, not closures or inner helpers. **Not reportable upstream.** It is,
however, the single largest *undisclosed* absence in the graph — and under this
document's framing, an undisclosed absence is the worst kind.

### Cause 2 — id collisions: the upstream defect

Filed as [Graphify-Labs/graphify#3302](https://github.com/Graphify-Labs/graphify/issues/3302).
The public issue and its follow-up comment quote **46%**, which this re-measurement
supersedes with **100% of non-nested non-dunder misses**. The comment should be
corrected — it currently understates the defect's share.

### What shipped in response: disclose the absence

The graph cannot report what it has no record of, so `build_context` now recovers
definitions from source (`symbols.definitions_in`) and reports any that fall
**inside code the agent was actually shown** but have no graph node, as
`unmodelled` — with the symbol, its extent, its signature and the reason.

### Disclosure coverage — a separate metric, deliberately not in the recall table

> **Read this before reading any recall number for this feature.** The feature
> does not, cannot, and is not meant to move recall. A symbol with no graph node
> cannot be *retrieved*; it can only be *declared missing*. Flat recall here is
> the feature working as designed, not the feature failing. It gets its own
> metric so nobody's eye lands on "recall: unchanged" and concludes "no effect".

| disclosure-coverage measure | result |
|---|---|
| ground-truth symbols with **no graph node** (the un-retrievable set) | 3 |
| of those, now **declared missing** by the pack | **2 of 3 (67%)** |
| gaps declared across the 14 packs in total | 15 |
| recall — reproduced for completeness, **unchanged by design** | 0.494 (containment, d2, b6k) |

Anti-vacuity is tested: a graph that *does* know a symbol must not have it
reported as a gap (`test_symbols_the_graph_does_know_are_not_reported_as_gaps`),
and gaps outside the code the agent was shown are not reported. Without those
two tests, "disclose everything unmodelled" could be satisfied by listing every
definition in every file — technically true and practically useless.

### The disclosure system's own disclosed gap — decided, not deferred

Disclosure covers **only files the pack actually emitted**. The third case
(`to_key_val_list`, `src/requests/utils.py:376`) sits in a file the agent was
never shown, so nothing in the pack can mention it.

**Decision: this is documented as a boundary, not closed.** Reasons:

- Closing it means scanning files the pack did *not* select — but the pack has
  no signal about *which* unshown files matter, so the honest version is
  "scan the whole repo per query", which is a different product with a
  different cost model.
- The precise, defensible claim is therefore **"every gap in what we show you is
  disclosed"**, and never **"every gap is disclosed"**. Any customer-facing text
  must use the first form. The `to_key_val_list` case is the concrete proof of
  the difference and should travel with the claim.
- Recursively, then: even the gap-disclosure has a disclosed gap, and that is the
  correct place to land. A disclosure system that claimed completeness for
  *itself* would be committing exactly the error it exists to prevent.

Revisit only if Phase 0's larger corpus shows unshown-file gaps to be a large
share of misses; on 14 tasks it is 1 of 3.

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
