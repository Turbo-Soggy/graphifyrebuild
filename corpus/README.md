# Taint-mapping validation corpus

A small Python application with **documented ground truth**, used to answer
critique #2: *does a `taints` / `reaches_sink` edge land on the right graph
node, and does the exposed-subset filter separate real flows from noise?*

## What this validates — and what it does not

Neither build detects taint. Both **map findings produced by an external
analyzer** (Semgrep, CodeQL, a SISA pipeline) onto graph node identifiers. So
the correctness question that belongs to *this* code is:

- **In scope:** given a finding at `file:line`, does it resolve to the correct
  enclosing function node? Do multi-hop flows land on the right node at both
  ends? Does the exposed-subset filter include exactly the functions on a real
  flow and exclude sanitized / constant-fed / unreachable ones? Are findings
  that cannot be resolved reported rather than silently dropped?
- **Out of scope:** whether the *analyzer* found the right flows in the first
  place. That is the analyzer's accuracy, not the mapper's. Conflating the two
  would let a mapping bug hide behind analyzer quality, or vice versa.

The corpus therefore ships findings with known-correct locations (as an
analyzer would emit them) **and** deliberately wrong/unresolvable ones, then
asserts what the mapper does with each.

## Ground truth

`ground_truth.json` is the machine-readable source of truth; each handler in
`vuln_app/handlers.py` also states its classification in its docstring.

| function | class | why |
|---|---|---|
| `tp_direct_sqli` | true positive | query param → SQL sink, one hop |
| `tp_multihop_shell` | true positive | header → helper → shell sink, two hops |
| `tp_reflected_xss` | true positive | query param → unescaped HTML |
| `tn_sanitized_sql` | true negative | sanitised before the sink |
| `tn_constant_sql` | true negative | reaches a sink, but the value is a trusted constant |
| `tn_unreached_sink` | true negative | holds a sink, never called from untrusted input |
| `tn_pure_helper` | true negative | no source, no sink |

The three true negatives are the load-bearing ones: each would be flagged by a
naive "this function calls a sink" heuristic, so they distinguish genuine flow
mapping from structural proximity.

### Boundary cases (M8) — `vuln_app/boundaries.py`

`graph.json` stores no function extents, only a start line, so Build A infers
containment. These cases pin that inference. They exist because it was measured
wrong: **2 of 4 boundary positions were mis-attributed** before the guards, with
a module-level statement blamed on whichever function preceded it.

| case | position | must resolve to |
|---|---|---|
| B1 | the `def` line itself | that function |
| B2 | an indented line in the body | its enclosing function |
| B3 | module-level statement after a function ends | the **file** node |
| B4 | an indented line in the *next* function | that function |
| B5 | module-level statement after the last function | the **file** node |
| B6 | a line past end-of-file | **unresolved** |

B3 and B5 are the load-bearing ones: a hardcoded secret or a taint source in
module-level config code must not be reported as living inside a function that
does not contain it. Disabling the top-level guard makes exactly those two fail
(verified by mutation test), so they cannot pass vacuously.

Both builds run the same cases. Build B answers from real `line_start`/
`line_end` extents, Build A from the guards — they agree, which is the point.
Positions are resolved through the AST at run time, so editing
`boundaries.py` shifts them automatically instead of silently invalidating
the expectations.

## Running

```bash
python corpus/validate_taint.py --build a    # graphify_ext  (graph.json)
python corpus/validate_taint.py --build b    # crg           (SQLite)
python corpus/validate_taint.py --build both
```
