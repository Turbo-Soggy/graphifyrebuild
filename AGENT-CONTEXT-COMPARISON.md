# Agent code-context: custom graphify build vs stock vs no graph at all

**Measured:** 2026-09-03 · **Repo:** [psf/requests](https://github.com/psf/requests) (full clone, 6,494 commits)
**Tasks:** 14, derived from the repository's own fix commits · **Tokenizer:** tiktoken `cl100k_base`
**Raw data:** `bench/agentctx/` (`tasks.json`, `results.json`, `raw/*.txt` — one file per task per arm)

---

## 1. What was measured, and why it can be trusted

The question is whether this build gets an agent to **fix-ready context** better than stock
graphify, and better than an agent with only ripgrep.

Grading that against tasks I invent would be grading my own homework, so the ground truth
comes from the repository's history. For a real fix commit `C` with parent `P`:

- **`G`** — the symbols whose bodies `C` actually modified, resolved by mapping every diff
  hunk's *old-side* line range back to its enclosing function/class with tree-sitter, against
  each file **as it existed at `P`**.
- **`E`** — the entry point: a symbol named in the commit message, the way a real bug report
  names the function that misbehaved. Chosen by message word order alone.
- **`G_discover = G − {E}`** — what the agent must actually *find*. Recall is scored over this,
  because crediting a tool for returning the symbol it was handed measures nothing.
- Everything is evaluated on a graph built at **`P`**. Building at `C` would leak the answer.

**Task selection was frozen before any tool ran**, by objective filters: non-merge, touches
package source, `|G| ≥ 2`, an entry symbol findable in the message, no reverts (inverse
duplicates), one task per entry symbol (the `Response.content` series alone offered three
near-identical commits and would have weighted one method as heavily as five unrelated areas).
Of 900 commits scanned: 337 dropped for touching a single symbol, 173 for no symbol-level
change, 122 for no entry point, 11 reverts, 1 duplicate entry.

**The ground-truth extractor was validated before use**, with both controls:

| control | result |
|---|---|
| every ground-truth symbol contains ≥ 1 changed line | **41 / 41** |
| every symbol *outside* `G` contains **no** changed line | **565 / 565** |

Hand-verified on `db575eee` (`Response.json`): hunks at 885–890 and 910 map to `Response.json`
(extent 881–910, exact), 471/473 to `PreparedRequest.prepare_body` (455–527, exact), and the
import-line hunks at 32/41 correctly resolve to **no symbol**.

### The arms

| arm | what it is |
|---|---|
| `grep` | no graph — ripgrep the entry symbol's name. The baseline that says whether *any* graph earns its keep. |
| `stock-affected` | `graphify affected --depth 2` — stock's reverse impact analysis |
| `stock-explain` | `graphify explain` — stock's bidirectional depth-1 neighbourhood, included so stock is represented at its **strongest** |
| `ext-up` | `blast-radius` at its default `--direction up` |
| `ext-both` | `blast-radius --direction both` — the custom build's own addition |
| `ext-context` | `context` — the new command; the only arm that returns **source code** |

Stock's hits are resolved through its own `affected_nodes` API rather than by parsing its text,
because stock prints the *call site* line, not the definition line, and scoring it against lines
it never claimed were definitions would have been unfair to it.

---

## 2. Headline results (defaults, depth 2, 14 tasks)

| arm | recall | precision | mean tokens | max tokens | files agent must still open | returns code? |
|---|---|---|---|---|---|---|
| `grep` | **0.530** | 0.223 | 735 | 2,125 | 5.7 | no |
| `stock-affected` | 0.256 | 0.267 | 97 | 356 | 1.4 | no |
| `stock-explain` | 0.387 | 0.154 | 155 | 318 | 1.9 | no |
| `ext-up` | 0.351 | **0.343** | **94** | 348 | 1.6 | no |
| `ext-both` | 0.351 | 0.123 | 386 | 1,867 | 6.1 | no |
| `ext-context` | 0.351 | 0.131 | 2,684 | 5,984 | **0** | **yes** |

Total follow-up file reads across the 14 tasks: `grep` 80 · `stock-affected` 20 ·
`stock-explain` 27 · `ext-up` 22 · `ext-both` 85 · **`ext-context` 0**.

**On defaults, ripgrep has the highest recall of anything here.** That is the single most
important number in this document, and it is the custom build's problem, not ripgrep's virtue —
grep buys it at 0.223 precision and 80 file-opens.

---

## 3. Per-task recall — the spread matters more than the mean

| task | \|D\| | grep | stock-affected | stock-explain | ext-up | ext-both | ext-context |
|---|---|---|---|---|---|---|---|
| 3816cfa1 | 3 | 0.33 | 0.33 | 0.67 | 0.67 | 0.67 | 0.67 |
| aa1461b6 | 2 | 0.50 | 0.50 | 0.50 | 0.50 | 0.50 | 0.50 |
| 99b3b492 | 2 | 0.50 | 0.50 | **1.00** | 0.50 | 0.50 | 0.50 |
| db575eee | 1 | **1.00** | 0 | 0 | 0 | 0 | 0 |
| bd100472 | 2 | **0.50** | 0 | 0 | 0 | 0 | 0 |
| bf4a8133 | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| c3367d18 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |
| f002b730 | 1 | **1.00** | 0 | 0 | 0 | 0 | 0 |
| 31b35ab8 | 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 9e9d2c65 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| 6195edc0 | 1 | **1.00** | 0 | 0 | 0 | 0 | 0 |
| 3e3fc768 | 3 | 0.33 | 0 | **1.00** | **1.00** | **1.00** | **1.00** |
| 36093e69 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| 8ed941fa | 4 | 0.25 | 0.25 | 0.25 | 0.25 | 0.25 | 0.25 |

- **`grep` strictly beats every graph arm on 4 / 14 tasks.**
- **`ext-both` never once beats `stock-explain`**; stock-explain beats it on one task.
- **`ext-up` and `ext-both` are identical on recall in 14 / 14 tasks** — `--direction both`
  bought *nothing*, at 4× the tokens (386 vs 94) and 4× the file-opens (6.1 vs 1.6).
- 4 / 14 tasks are scored 0 by **every** arm including grep.

---

## 4. Where the custom build genuinely wins, and exactly why

`ext-up` beats `stock-affected` (0.351 vs 0.256) for one mechanical reason, verified rather
than assumed. Stock seeds its walk with the seed's own members **but deliberately does not
report them**. So on a *class* seed, stock returns nothing at all:

```
$ graphify affected "VendorAlias" --depth 2
Affected nodes for VendorAlias
Relations: calls, indirect_call, references, imports, ...
Depth: 2
No affected nodes found.
```

```
$ graphify-ext blast-radius requests_packages_init_vendoralias --depth 2
Blast radius of requests_packages_init_vendoralias (depth 2, up): 4 nodes, 3 edges, ~519 tokens
  [0] VendorAlias         requests/packages/__init__.py:L28
  [0] .find_module()      requests/packages/__init__.py:L38
  [0] .__init__()         requests/packages/__init__.py:L30
  [0] .load_module()      requests/packages/__init__.py:L42
```

The ground truth for that commit is exactly `__init__`, `find_module`, `load_module`.
**Stock scores 0.00; the custom build scores 1.00.** That is the whole of its measured
advantage over `affected` — a reporting-policy difference on class seeds.

Everywhere else the two are near-equivalent by construction: the custom build's
`DEFAULT_RELATIONS` is **byte-identical** to stock's `DEFAULT_AFFECTED_RELATIONS`, its default
direction is `up` like stock's, and it uses the same member-seeding trick stock does.

---

## 5. What context each arm actually hands the agent

This is the difference that the recall table cannot show.

**`stock-affected`** — 52 tokens, and the agent knows nothing about the code:

```
Affected nodes for VendorAlias
Relations: calls, indirect_call, ...
Depth: 2
No affected nodes found.
```

**`ext-up`** — names, files, lines. Precise, cheap, and still one file-open away from useful:

```
  [0] .find_module() requests/packages/__init__.py:L38
```

**`ext-context`** — the seed's real source, decorators included, with its neighbours:

```
=== SEED (depth 0 via inherits) requests/packages/__init__.py:28-104  VendorAlias ===
class VendorAlias(object):

    def __init__(self, package_names):
        self._package_names = package_names
        ...
    def find_module(self, fullname, path=None):
        if fullname.startswith(self._vendor_pkg):
            return self
```

and for a call relationship it follows the edge into the callee's body:

```
=== SEED (depth 0 via calls) requests/models.py:881-910  json ===
def json(self, **kwargs):
    ...
        encoding = guess_json_utf(self.content)

=== RELATED (depth 1 via calls) requests/utils.py:893-922  guess_json_utf ===
def guess_json_utf(data):
    sample = data[:4]
    if sample in (codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE):
        return 'utf-32'
```

Every symbol carries an exact extent, a signature, and the relation that pulled it in.
Extents are recovered by re-parsing with the same tree-sitter grammars graphify already
depends on — **graphify itself records only a start line** (`extract.py` writes
`f"L{node.start_point[0] + 1}"`; there is no end line, signature, or source anywhere in
`graph.json`). Where an extent cannot be resolved the symbol is listed as **unresolved with a
reason** and no slice is emitted, because a wrong slice looks exactly as authoritative as a
right one.

---

## 6. The recall ceiling: the graph is not the limit, the ranking is

Across the 25 symbols the agent had to discover:

| | count | share |
|---|---|---|
| no node in the graph at all | 3 | **12%** — extraction defect (§8) |
| node exists but unreachable at any depth | 0 | **0%** |
| reachable at some depth | 22 | 88% |
| …of those, reachable within depth 2 | 13 | **52%** |

**Connectivity is not the problem.** The graph does connect co-changed symbols — 0% unreachable.
Depth-2 traversal can structurally reach only 52%, which caps every depth-2 arm.

Going deeper does **not** help at a realistic budget:

| depth (budget 8,000) | recall | mean tokens | symbols omitted for budget |
|---|---|---|---|
| 1 | 0.351 | 983 | 0 |
| 2 | 0.351 | 3,104 | 53 |
| 3 | 0.351 | 4,889 | 514 |
| 4 | 0.351 | 5,280 | 987 |

Recall is **flat while cost rises 5×**. Remove the budget and the reachable symbols do come
back — 0.351 (d2) → 0.387 (d3) → 0.494 (d4) → **0.619 (d6)** — which proves the symbols were
reachable all along and were being **crowded out of the budget by irrelevant neighbours**.

**The binding constraint is relevance ranking, not traversal depth.** Depth-first expansion
without a relevance model spends the budget on noise.

### The default relation set is measurably wrong for this task

Containment (`method`, `contains`) is **off by default** — a decision previously justified on
the grounds that `contains` is ~61% of all edges and would swamp results. On real fix tasks
that is backwards, because co-changed symbols are very often siblings or members:

| configuration | recall | precision | mean tokens |
|---|---|---|---|
| default, depth 2 | 0.351 | 0.131 | 2,684 |
| **`--include-containment`, depth 2** | **0.494** | **0.146** | 4,539 |
| `--include-containment`, depth 3 | 0.494 | 0.079 | 5,670 |
| `--include-containment`, depth 3, budget 12k | **0.565** | 0.068 | 10,775 |

Containment raises recall **41% relative** *and* improves precision. It is the single largest
lever found, and it is switched off by default.

**With containment at depth 3 the context arm reaches 0.565 — the only configuration that
beats ripgrep's 0.530 — while still returning code and requiring zero file-opens.**

---

## 7. Cost

Graphs built with `graphify extract . --code-only --no-cluster` (AST only, no LLM):
**~9 s** for 963 nodes / 2,041 edges; task graphs ranged 902–2,120 nodes. That cost is stock
graphify's, paid identically by both graph arms — **the custom build performs no extraction of
its own; it queries stock's graph.** Its context quality is therefore bounded above by stock's
extraction, which §8 shows is not airtight.

Per-query cost is where the arms diverge: `ext-up` at 94 tokens is the cheapest way to get a
correct symbol *name*; `ext-context` at 2,684 is the cheapest way to get the *code*, once the
6 file-reads it replaces are counted.

---

## 8. A stock extraction defect found by this benchmark

3 of 25 ground-truth symbols had **no graph node at all**. One is a plain, undecorated public
Python method, and the cause is an **id collision**: graphify slugifies `_get_connection` and
`get_connection` to the same node id, and the second silently overwrites the first.

Minimal reproducer — three methods in, two nodes out:

```python
class Adapter:
    def _get_connection(self, url):    # L2
        return url
    def get_connection(self, url):     # L5  <-- vanishes
        return self._get_connection(url)
    def unrelated(self, x):            # L8  <-- control, extracts fine
        return x
```

```
graph nodes for mod.py:
    mod_adapter_get_connection | ._get_connection() | L2
    mod_adapter_unrelated      | .unrelated()       | L8
```

`Adapter.get_connection` is absent entirely. In `psf/requests` this loses the real
`HTTPAdapter.get_connection` (adapters.py L406), leaving only a stray docstring node at L407.
This is an upstream defect and a hard recall ceiling for **any** tool built on this graph,
including stock's own `affected` and `explain`. Draft report: `plans/artifacts/upstream-issue-draft.md`.

---

## 9. Verdict

**What the custom build genuinely does that stock cannot**

1. **Returns source code.** `context` is the only arm of six that hands an agent a symbol's
   actual body, with exact extents, signatures, decorators, and explicit truncation. It took
   **80 file-opens to 0** across 14 tasks. Nothing in stock does this, because the data it
   needs is not in `graph.json`.
2. **Reports a class's members.** Worth 1.00 vs 0.00 recall on class seeds, where stock
   `affected` returns nothing.
3. **Honest degradation.** Unresolvable symbols are named with a reason rather than guessed or
   dropped.

**What stock does as well or better**

- `graphify explain` **matches or beats every blast-radius configuration on recall at default
  settings** (0.387 vs 0.351) for 155 tokens. On raw impact-analysis quality, stock is not behind.
- The custom build's relation defaults, direction default, and member-seeding are all
  inherited from stock. The traversal is not an improvement on stock's; it is stock's.

**Where neither graph is worth its cost**

- On 4 / 14 tasks ripgrep strictly beat every graph arm, and on 4 / 14 nothing scored at all.
- `--direction both` should be considered a **failed feature on this evidence**: identical
  recall to `up` on 14/14 tasks, 4× the tokens, 4× the file-opens.

**Recommendation — worth continuing, but narrowed.** The value is concentrated almost entirely
in **§5 (delivering code)** and it is real: an agent that gets bodies instead of line numbers is
doing a different job. The traversal layer is not where the value is, and should stop being
presented as the product. Three concrete next steps, in order of measured payoff:

1. **Turn containment on by default** (or make it the default for `context`): +41% relative
   recall, better precision, and it is the difference between losing and beating ripgrep.
2. **Build a relevance ranker.** The symbols are reachable; depth alone cannot surface them
   within a budget. This is the highest-value remaining work by a wide margin.
3. **Report and track the id-collision defect** — it is a recall ceiling nothing downstream can
   fix.

**Caveats.** One repository, one language, 14 tasks, `|G_discover|` of 1–4 — the per-task spread
is wide and the means are not precise to more than about ±0.1. Co-change is a proxy for
"context needed to fix", not a definition of it; a symbol an agent should have read but did not
need to edit is invisible to this scoring. Every number here traces to a file in
`bench/agentctx/`.
