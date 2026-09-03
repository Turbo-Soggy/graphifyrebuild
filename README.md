# graphify-ext

A layer on top of stock [graphify](https://github.com/Graphify-Labs/graphify)
(`graphifyy` on PyPI, vendored read-only under `graphify-upstream/` for the
tests) that turns its code graph into **fix-ready context for a coding agent**:
the source of the symbol under repair plus its neighbourhood, within a token
budget, with every gap in that context named rather than hidden. It also keeps
one cached graph per git branch so a branch switch reconciles incrementally
instead of rebuilding.

Nothing in `graphify-upstream/` is modified; everything lives in `graphify_ext`.

## What "good enough for autonomous remediation" means here, and where it stands

The target is **disclosure, not completeness** (`plans/04-correctness-roadmap.md`):
no static graph reaches zero misses, so the achievable guarantee is that the
graph never lies about what it is missing. Every retrieval result carries its
gaps as fields an agent can branch on.

Measured on the frozen 70-task corpus (`bench/agentctx/corpus.json`: real fix
commits from psf/requests, pallets/flask and expressjs/express; ground truth =
the symbols the fix actually changed, computed from git without consulting the
graph). All figures below are at **depth 2, budget 6,000 tokens, containment
on, `max_nodes` 800**, the `default` config in `bench/agentctx/regress.py`;
recall is over the symbols the agent had to *discover*, precision over
everything returned. Re-derived on 2026-09-04 by `bench/agentctx/compare_configs.py`.

| | stock graph (`default`) | + `supplement` | + class summaries | + index tier (reserve 300) |
|---|---|---|---|---|
| tasks scoreable (entry symbol has a node) | 53 of 70 | **68 of 70** | 68 | 68 |
| symbols shown **as source**, mean recall | 0.512 (n=53) | 0.622 (n=68) | 0.629 | 0.629 |
| symbols shown as source **or named in the index** | - | - | - | **0.723** |
| mean precision (bodies) | 0.158 | 0.118 | 0.118 | 0.118 |
| tasks regressed vs previous column | | **0** | **0** | **0** |
| tasks improved / newly scoreable vs previous | | 7 / 15 | 1 / 0 | 0 / 0 (named: many) |
| mean tokens used of 6,000 | - | 4,418 | 4,418 | 4,811 |

Each column changes exactly one thing against the one before it, at the same
total budget. Per-task detail, which matters more than the means, is in the
captured baselines under `bench/agentctx/` and is re-derived by
`compare_configs.py`. The caveats travel with the numbers: n is 70 with 1 to 7
symbols per task; co-change is a proxy for "context needed", not a definition;
precision falls because packs return more symbols; "named in the index" means
the agent still has to open that symbol, so the two recall rows are never
summed. Of the 175 ground-truth symbols, every one the graph has a node for is
*reachable*: the misses left are 16 functions nested inside functions (no node
by upstream design; disclosed as `unmodelled`) and symbols beyond three hops or
outside the budget.

The index reserve was fitted, not asserted: a fixed 1,200-token share reached
0.782 named recall but cost one task its bodies (bodies 0.629 to 0.614); a
dynamic reserve of 300 that also spends what the bodies leave unused regressed
no task. Sweep in `plans/04-correctness-roadmap.md`. Flask remains the weak
shape (11 of 19 tasks at zero for bodies) and is dominated by symbols reached
through deeper chains than depth 2 covers.

**The two tasks still unscoreable are by design and disclosed**: one entry is
a function nested inside a function (graphify emits no node for closures; the
pack lists them as `unmodelled`), the other is a four-segment JavaScript
binding the walker does not model.

### What an agent gets, end to end

```
graphify-ext search "send"                       # what could this name mean? (file, line, origin)
graphify-ext context "lib/response.js:239"       # seed by location, label, id or qualified name
graphify-ext blast-radius res.json --relation calls   # who is affected
graphify-ext overrides Base.method               # subclass overrides that need their own fix
graphify-ext edge-diff snapshot --node res.json  # ... edit ...
graphify-ext refresh                             # incremental graph update for the edited files
graphify-ext edge-diff check                     # exit 2 on an unexpected structural change
```

`context` returns the seed's real source (decorators included, exact extents,
signature) and its neighbours' source, ranked by depth then relation weight, a
related class as signature plus member list rather than its whole body, an
**index** tier of `file:line signature` lines for what did not fit or sits one
hop further out, the **tests** whose nodes link to anything shown (with the
edge's relation and confidence), and:

- `omitted` — symbols dropped for budget, each with a score and a severity
  (`truncated_high_rank` means it scored at least as well as something kept);
- `unresolved` — symbols the graph knows but no slice was emitted for, with a
  stable reason code. `definition_mismatch` means the graph's line for that
  symbol now holds a *different* definition: the graph is stale, and the pack
  refuses to serve the wrong body under the requested name;
- `unmodelled` — definitions present in the code shown that have **no graph
  node at all** (nested functions by design; id-collision victims by defect);
- `stale_files` — files the pack touched whose content hash no longer matches
  graphify's `manifest.json`;
- per-symbol `origin` (extractor vs supplement) and, on `tests` edges, the
  confidence label that separates coverage-measured from name-guessed.

`refresh` closes the edit loop: incremental graphify update of the edited files
(default: every file whose manifest hash changed), then re-application of the
supplement and injected edges, without a full rebuild.

Extents are recovered with the tree-sitter grammars graphify itself depends on,
for Python, JavaScript, TypeScript, Go, Java, Rust, Ruby, PHP, Kotlin and C#;
anything else is reported as `unsupported_language`, never guessed.

The same tools are exposed over MCP (`graphify-ext-mcp`, nine tools) with
errors returned as data, and ambiguous seeds come back as candidate lists. The
repo's own `CLAUDE.md` is the agent-facing workflow.

### `supplement`: making the missing queryable

Two upstream limits left the symbol a fix was *about* with no node in 17 of 70
tasks: assignment-bound JavaScript members (`res.json = function json(obj)`,
express's entire public API; upstream guard #1077) and id collisions
(`@overload` stubs, `_get_x` vs `get_x`; upstream #3302). A graph with no record
of a symbol cannot be asked about it, so `graphify-ext supplement` materialises
those definitions from source: a node in the extractor's own schema (tagged
`origin: "graphify-ext:supplement"`, with `qualified_name` and
`supplement_reason`), a `contains`/`method` edge from the owner, and
**INFERRED** `calls` edges only where a callee name resolves to exactly one
callable. Extractor nodes are never modified; nested functions are declined;
files the graph is stale for are refused whole; the pass is idempotent and
opt-in per output slot (the hooks re-apply it after rebuilds once enabled).

## Install

```
pip install -e .                 # into the SAME environment as graphifyy
pip install -e ".[mcp]"          # optional: fastmcp for graphify-ext-mcp
graphify-ext hook install        # replaces the stock hook bodies in .git/hooks
graphify-ext supplement          # once per repo, after the first graphify extract
```

For `uv tool` installs of graphifyy: `uv tool install graphifyy --with graphify-ext`.

## Per-branch caching (Requirement 1)

```
.graphify-cache/
├── main/                      # slot: graph.json, manifest.json, reports, meta
├── feature-x-<digest>/        # slash-y branch names get a digest suffix
├── @detached/                 # scratch slot for detached HEAD (always rebuilt)
graphify-out/                  # link (or copy) of the ACTIVE branch's slot
```

`graphify-out/` stays the stable path so every stock command keeps working.
`post-checkout` swaps slots and reconciles with graphify's own content-hash
incremental primitive instead of a full rebuild; `post-commit` does the stock
incremental update plus slot stamping and re-application of the ext layer
(supplement, then injected edges). Full rebuild fires on: no slot for the
branch, history rewrite (base commit no longer an ancestor), detached HEAD,
graphify version change. Symlink → junction → copy mode is chosen at runtime
and *functionally verified*. Measured revisit speedup vs stock's full rebuild:
1.9x on flask (83 files), 17.4x on scrapy (485 files) — see `TEST-RESULTS.md`.

## AppSec edges (Requirement 2, retained)

`inject --semgrep` (taint/sink edges; trace-less findings are mapped only for
declared taint rules and otherwise reported as skipped), `test-link --coverage`
(EXTRACTED) / `--heuristic` (INFERRED), `config-scan` (env-var reads →
defining config files), `triage` (per-vulnerability context bundle). Injected
edges carry `origin: "graphify-ext"`, persist in the slot and are re-applied
after rebuilds. Taint reachability is no longer a roadmap phase (scrapped
2026-09-03) but the code stays and is tested.

## Development

```
python -m venv .venv
.venv/Scripts/pip install -e ".[mcp]" pytest tiktoken
git clone https://github.com/Graphify-Labs/graphify graphify-upstream   # tests read its source
.venv/Scripts/python -m pytest tests -q            # 184 tests; add -m e2e for the real-graphify lifecycle
```

Benchmark (needs full clones of the three corpus repos, gitignored):

```
git clone https://github.com/psf/requests      bench/agentctx/repo
git clone https://github.com/pallets/flask     bench/agentctx/repo-flask
git clone https://github.com/expressjs/express bench/agentctx/repo-express
python bench/agentctx/build_all.py 4                 # 70 worktrees + graphs, ~2 min
python bench/agentctx/regress.py --config default    # must print "no per-task change"
python bench/agentctx/regress.py --config supplement-index-dyn300   # the shipped defaults
python bench/agentctx/compare_configs.py bench/agentctx/baseline-supplement.json bench/agentctx/baseline-supplement-index-dyn300.json
python bench/agentctx/diagnose.py --config supplement-index-dyn300 --only-zero   # why a task missed, per symbol
```

Any change to ranking, containment, edge generation or the supplement must be
run through `regress.py` and reported per task, budget-matched, before it is
described as an improvement. The evidence rules are in
`plans/04-correctness-roadmap.md`.

## Documents

- `plans/04-correctness-roadmap.md` — what is claimed, what is not, and why
- `AGENT-CONTEXT-COMPARISON.md` — the 14-task study that motivated `context`
- `TEST-RESULTS.md` — differential testing of the branch cache vs stock
- `CUSTOM-BUILD-GUIDE.md` — full command reference and the connected-repo record
- `CODE-GRAPH-RESEARCH.md` — how this maps onto the agent code-graph literature
