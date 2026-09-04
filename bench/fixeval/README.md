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

## Verifiable tasks: 23

Mined 3,000 commits per repo (`bench/agentctx/wide-*.json`, 208 candidates)
on top of the frozen 70, and kept only tasks with a test that fails before the
fix and passes with it.

| repo | candidates with test changes | verifiable | why the rest dropped |
|---|---|---|---|
| flask | 50 | 7 | test diffs that pass without the fix (refactors, removed asserts), one typing check |
| requests | 51 | 3 | 36 refactor-only test diffs, 6 Python-2-era suites, 2 environment failures |
| express | 141 | 13 | 47 no test change; 36 2012-2013 trees whose dependencies no longer install; 12 mocha 1.x trees that crash under Node 24 |

Environments: shared per-era venvs (Python 3.9 for requests before 2021, 3.11
for everything else), `node_modules` cached per dependency set, a shared
mocha 10 for Express trees older than mocha 4, the tree self-linked on
`NODE_PATH`.

## Results (2026-09-04, model `sonnet`, 23 tasks x 3 arms x 2 repetitions = 138 runs, $58)

| arm | runs resolved | tasks resolved in **both** reps | in **any** rep | mean fraction of fail-to-pass tests passing | mean turns | mean cost | runs that broke a passing test |
|---|---|---|---|---|---|---|---|
| no graph | **25/46 (54%)** | 11/23 | 14/23 | 0.628 | 19.9 | $0.37 | 1 |
| graph pack | 21/46 (46%) | 9/23 | 12/23 | 0.610 | 18.2 | $0.43 | 3 |
| graph pack + "before you stop" checklist | **25/46 (54%)** | 11/23 | 14/23 | **0.666** | 19.4 | $0.44 | 2 |

Repeatability: each arm flips on exactly 3 of 23 tasks between its two
repetitions, so a one-repetition comparison of two arms is noise up to about
three tasks. Two repetitions are the minimum at which these rows mean
anything, and they still do not separate the top two.

Per-task table: `python run.py report`. Raw rows: `results.jsonl` (one JSON
line per run, with the agent's turns, cost, edited files and per-test outcome).

## Reading it honestly

- **The pack on its own did not help; with the checklist it matched no-graph.**
  46% vs 54% vs 54% on 46 runs each. The four-run gap between `graph` and the
  other two is inside the three-task repetition noise, so the defensible
  statement is: no arm is better than another at this size, and the plain pack
  is the one that is *not* ahead.
- **Where the pack does something no-graph cannot:** `express/f41d09a3`, a
  12-test refactor across the router, went 11/12 and 10/12 with the pack and
  0/12 without, twice each. On big multi-symbol changes the pack's map of
  neighbours is the difference between getting most of it and getting none.
- **Where it hurts:** the pack shortens exploration (18.2 turns vs 19.9) and
  the agent stops sooner. `express/bad55f79` and `express/7f26cfca` were
  solved without the pack and not with it; the graph agent edited the shown
  symbol and did not look further. The checklist recovers exactly that and
  costs about a turn per run.
- **Over-editing is real but rare:** 3 graph runs and 2 guided runs broke
  previously passing tests, versus 1 without the graph. The first wording of
  the checklist broke 18 tests on one run; the shipped wording did not repeat
  that.
- **Cost:** the pack is about 6k tokens of prompt on every turn, so it costs
  more per run ($0.43 to $0.44 vs $0.37) while using fewer turns. On these
  small libraries the token cost of showing the code exceeds the turn cost of
  finding it. That ratio should invert on larger codebases, and this corpus
  cannot show it.
- **Problem statements are commit subjects.** All arms fail identically on
  the underspecified ones (`flask/9822a035`, `express/1e2951a8`,
  `express/cec0c06a`); no context fixes a one-line spec.

## What would make this evaluation worth quoting

- Larger codebases. Every repo here fits in an agent's head with grep in 20
  turns; the pack's value proposition is codebases where it does not.
- Real problem statements (issues, not commit subjects) and a second model.
- More repetitions: at 3 flips per 23 tasks per arm, separating a 5-point
  difference needs roughly four repetitions.
