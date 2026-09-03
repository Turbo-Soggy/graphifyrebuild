# End-to-end fix evaluation

Everything else under `bench/` measures retrieval. This measures the thing
retrieval is a proxy for: **does a coding agent, given this context, produce a
fix that passes the maintainers' tests?**

## Method (SWE-bench style, on the frozen corpus)

For a fix commit `C` with parent `P`:

1. Take only the test files `C` changed. Apply that diff to the tree at `P`,
   run those files. Tests that fail there **and pass with the real fix applied**
   are the task's FAIL_TO_PASS set; tests that pass in both must keep passing.
   Tests failing in both are environment failures and are excluded, not scored.
   A task with an empty FAIL_TO_PASS set is not verifiable and is dropped.
2. A headless Claude Code agent (`claude -p`, `sonnet`, 30 turns, $1.50 cap)
   gets a copy of the tree at `P` with no `.git` and no `graphify-out`, plus the
   commit message as the whole problem statement. Tools: Read, Edit, Write,
   Grep, Glob. No Bash, so it cannot run tests it has not seen or read history.
3. Two arms, identical except for one thing: the `graph` arm's prompt also
   carries the shipped `graphify-ext context` pack for the entry symbol
   (depth 2, 6k tokens, containment on, index reserve 300, mention-first).
4. Apply the test diff to the agent's tree, run the same files.
   `resolved` = every FAIL_TO_PASS test passes and nothing in PASS_TO_PASS broke.

`python run.py select` builds `tasks.json`; `run.py run --arm both` appends to
`results.jsonl`; `run.py report` prints the table. Per-task venvs use Python
3.11 (2022-era Flask calls `pkgutil.get_loader`, which 3.12 turns into a
RuntimeError); Express runs under Node 24 with mocha.

## Verifiable tasks: 6 of 70

| repo | with test changes | verifiable | why the rest dropped |
|---|---|---|---|
| flask | 8 | 3 | 4 have test diffs that pass without the fix (refactors, removed asserts); 1 is a typing check, not a pytest file |
| requests | 5 | 2 | 3 are pre-2021 suites whose vendored `six` breaks on Python 3.11+ |
| express | 21 | 1 | selection stopped after 1 verified task: `npm install` per historical commit took 10-40 minutes each and one mocha run hung; the remaining 20 are untried, not failed |

## Results (2026-09-04, model `sonnet`)

| task | graph | nograph | turns (g/n) | cost (g/n) | files edited (g/n) |
|---|---|---|---|---|---|
| express/59e205a5 | RESOLVED | RESOLVED | 31/32 | $0.79/$0.40 | 3/3 |
| flask/96c97dec | RESOLVED | RESOLVED | 20/21 | $0.37/$0.22 | 3/3 |
| flask/9822a035 | failed | failed | 18/31 | $0.58/$0.73 | 2/2 |
| flask/daf1510a | failed (1 of 6 tests) | failed (0 of 6, **no edit made**) | 29/31 | $0.80/$0.55 | 2/0 |
| requests/99b3b492 | failed | RESOLVED | 10/31 | $0.21/$0.53 | 2/2 |
| requests/db575eee | RESOLVED | RESOLVED | 22/21 | $0.54/$0.28 | 3/4 |
| **total** | **3 / 6** | **4 / 6** | 21.7 / 27.8 mean | $3.29 / $2.72 | |

## Reading it honestly

- **n = 6, one run per cell, one model.** Nothing here separates the arms
  statistically; a single task flipping either way changes the headline. This
  is a pipeline that works end to end and a first data point, not a verdict.
- **The pack did not raise the resolve rate.** 3 of 6 with it, 4 of 6 without.
  The one task the graph arm lost (`requests/99b3b492`) is instructive: the
  agent refactored `rebuild_proxies` exactly as the pack framed it, declared
  itself done at turn 10, and never touched the *call site* in
  `Session.request` that the fix actually needed. The no-graph agent hit the
  30-turn cap and found it. Context that looks complete can end exploration
  early; that is the failure mode the disclosure fields exist for, and it
  happened anyway.
- **The pack changed how the budget was spent.** Mean turns 21.7 vs 27.8; on
  `flask/daf1510a` the no-graph agent used 31 turns and edited nothing, while
  the graph agent implemented the named method (1 of 6 tests) but not the two
  sibling methods and the blueprint mirrors the tests also required. Both
  arms were undercut by the same thing: the commit message names one symbol
  and the fix spans several.
- **Cost per run was higher with the pack** ($0.55 vs $0.45 mean), because the
  pack is ~6k tokens of prompt every turn; it bought fewer turns, not fewer
  dollars, at this task size.
- **Problem statements are commit subjects**, often one line ("refactor
  stream_with_context for async views"). Both arms failed that task. A real
  issue text would change both arms; it would not obviously favour either.

## What would make this evaluation worth quoting

- More verifiable tasks: Express needs the `npm install` done once per
  dependency set rather than per commit; requests needs a 3.9 interpreter for
  its 2016-2019 suites; a corpus mined for commits that *reference an issue*
  would give real problem statements.
- Repeats per cell (the agent is stochastic) and a second model.
- A third arm: the pack *plus* the instruction to also run `search` over every
  symbol the commit message names, since the two graph-arm misses were both
  "stopped one symbol short".
