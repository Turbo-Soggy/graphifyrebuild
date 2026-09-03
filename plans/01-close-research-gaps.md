# Plan 01 — Close the CODE-GRAPH-RESEARCH.md gaps

Source: `CODE-GRAPH-RESEARCH.md` (5 ranked gaps).
Target: `graphify_ext` (Build A) and `crg` (Build B) in `C:\projects\graphifyrebuild`.

---

## ⚠️ READ FIRST — live-repo hazard and in-flight state

**`graphify_ext` is installed EDITABLE into `C:\Python314`, which is the
interpreter the connected repo `C:\projects\appsec-fix-layer` pins in its git
hooks.** Verified: `graphify_ext.__file__` resolves to
`C:\projects\graphifyrebuild\graphify_ext`. The import chain
`branch_cache → edge_inject → graphio` sits in the post-commit / post-checkout
hook path.

**Consequence: every edit to `graphify_ext/` takes effect immediately in a real
repo, with no build or install step.** There is no staging buffer. Treat the
working tree as production.

**In-flight, UNVERIFIED changes already on disk (made before this plan):**

| file | change | status |
|---|---|---|
| `graphify_ext/graphio.py` | `resolve_by_location()` rewritten with 3 guards: length bound, exact-definition-line, top-level/column-0 | Measured 7/7 on boundary cases. **pytest regression run was interrupted — NOT verified.** |
| `corpus/vuln_app/boundaries.py` | new file, boundary fixtures | Created. **Not referenced by `ground_truth.json` or `validate_taint.py` — currently dead weight.** |

**Expected failure:** `tests/test_fix_context.py::test_line_past_end_of_file_is_unresolved`
(`tests/test_fix_context.py:63-65`). Its fixture writes 60 lines of `"line {i}"`
— all at column 0 — so the new top-level guard correctly demotes L35 to the file
node, while the test asserts `== "app.validate"`. **The test's expectation
predates the guard; the fixture is unrealistic, not the guard wrong.** Phase 1
decides this explicitly rather than by reflex.

Phase 1 is mandatory and blocking. Do not start Phase 2+ with an unverified
resolver live in a connected repo.

---

## Phase 0 — Documentation discovery (COMPLETE)

Two read-only discovery passes were run; findings below are cited, not assumed.
Re-read the cited lines before implementing — do not trust this summary alone.

### 0.1 Allowed APIs — FastMCP (for Phase 4)

Installed: **fastmcp 3.4.7**, **mcp 1.29.1** (in `.venv`; *not confirmed present
in `C:\Python314` — Phase 4 must check*).

| fact | source |
|---|---|
| `from fastmcp import FastMCP` | `crg/crg-upstream/code_review_graph/main.py:20` |
| Server constructed at module level: `FastMCP("name", version=..., instructions=...)` | `main.py:90-98` |
| Tool decorator is **`@mcp.tool()`** — empty parens, no args, on all 30 tools | `main.py:101,153,...,1034` |
| Minimal sync tool to copy | `main.py:415-429` |
| Minimal zero-arg tool to copy | `main.py:1024-1032` |
| Async tool wrapping blocking work: `return await asyncio.to_thread(_run)` | `main.py:153-190` |
| Tools return a **plain `dict`**; no Pydantic models anywhere | `main.py` + `tools/registry_tools.py:40-44` |
| Errors are **returned as data**, not raised: `{"status":"error","error":str(exc)}` | `tools/registry_tools.py:45-46`; helper `tools/_common.py:22-26` |
| `--tools` allow-list filter: `_apply_tool_filter()` | `main.py:1114-1173`; called at `main.py:1216` |
| Tool removal uses `mcp.local_provider.remove_tool(name)` (NOT the removed private `_tool_manager._tools`) | `main.py:1171-1173`; confirmed in installed `fastmcp/server/server.py:496` |
| stdio: `mcp.run(transport="stdio", show_banner=False)` — banner **must** be off or it corrupts the JSON-RPC handshake | `main.py:1238` (+ comment `1236-1237`) |
| http: `mcp.run(transport="streamable-http", host=..., port=..., middleware=...)` | `main.py:1249-1254` |
| Windows: set `asyncio.WindowsSelectorEventLoopPolicy()` before `mcp.run()` | `main.py:1221-1231` |
| Impl modules under `tools/` contain **zero** fastmcp imports — decorated wrappers live only in `main.py` | verified by grep |

**Minimum viable server = `main.py:11-20` + `90-98` + `1024-1032` + `1238`.**

### 0.2 Allowed APIs — current `graphify_ext` traversal surface (for Phase 3)

**Headline: relation-typed traversal already exists in the core and is merely
unexposed.** This makes gap #3 a CLI + plumbing change, not a core rewrite.

| fact | source |
|---|---|
| `blast_radius()` **already accepts** `relations: tuple[str, ...] = DEFAULT_RELATIONS`, keyword-only | `graphify_ext/blast_radius.py:55-63`, param at `:60` |
| Relation filter applied at | `blast_radius.py:82` (`relation_set` built at `:73`) |
| `DEFAULT_RELATIONS` — 14 entries, currently **equal to** upstream's, not a superset | `blast_radius.py:21-36` |
| `TAINT_RELATIONS = ("taints", "reaches_sink")` | `blast_radius.py:39` |
| `_MEMBER_RELATIONS = ("method", "contains")` | `blast_radius.py:41` |
| `_INHERIT_RELATIONS` | `blast_radius.py:166` |
| **No CLI relation flag exists** — every `add_argument` for `blast-radius` enumerated | `__main__.py:53-59` |
| CLI forwarding call site that omits `relations` | `__main__.py:120-121` |
| `overrides_of()` has **no** relations param | `blast_radius.py:169` |
| `taint_exposed()` has **no** relations param | `blast_radius.py:153` |
| Repeatable-flag precedent already in this file (`action="append"`) | `__main__.py:95-96` |
| Upstream's own `--relation` style (empty list means default) | `graphify-upstream/graphify/cli.py:1355-1360, 1386` |

**Three landmines the plan must handle explicitly:**

1. **`triage._neighbors` reads `DEFAULT_RELATIONS` directly** (`triage.py:49`),
   independently of what `blast_radius` traversed (`triage.py:95`). Narrow the
   traversal without updating `_neighbors` and triage emits a caller under
   `neighbors` that appears nowhere in `blast_radius.nodes` — an internally
   contradictory agent context.
2. **The subgraph-closure loop deliberately ignores the relation filter**
   (`blast_radius.py:124-131`, comment at `:119-120`). That is why injected
   taint edges survive into output, and `tests/test_fix_context.py:106` pins it.
   Relation-typed traversal needs an explicit, documented policy here.
3. **`relations=` is passed by no caller anywhere in the repo** — the parameter
   is untested code despite existing. Do not assume it works; test it.

### 0.3 Anti-patterns to avoid

- Do NOT use `mcp._tool_manager._tools` — private path removed in fastmcp >= 3.0.
- Do NOT call `mcp.run(transport="stdio")` without `show_banner=False`.
- Do NOT return Pydantic models or raise from tools — crg returns dicts and
  encodes errors as data.
- Do NOT put `@mcp.tool()` decorators in impl modules — crg keeps them in one file.
- Do NOT add a `relations` parameter to `blast_radius()` — **it has one**.
- Do NOT shrink or rename `DEFAULT_RELATIONS` — `tests/test_hooks_ext.py:78`
  asserts upstream's set is a subset and fails first.
- Do NOT edit `graphify_ext/` without re-running the suites (live-repo hazard).

---

## Phase 1 — Stabilize the in-flight resolver change (BLOCKING) — ✅ DONE

**Outcome: green, and it found two further bugs that the plan did not anticipate.**

| check | result |
|---|---|
| `pytest tests -q` | **77 passed, 1 skipped** (was 73) |
| `corpus/validate_taint.py --build both` | **24/24** |
| `bench/regression_a.py` | **19/19** |
| `C:\Python314` imports the edited module | clean, all three guards present |
| Connected repo read-only smoke | works; `git status` clean, `graphify-out` still a link, hooks still ext variant, graph unchanged at 5,059 / 6,944 |

**The one predicted failure was classified as a bad fixture, not a bad guard.**
`test_line_past_end_of_file_is_unresolved` wrote 60 lines of `"line {i}"` — all
at column 0, i.e. all at module scope — so it asserted that a module-scope line
resolves to a *function*, the exact mis-attribution the guards prevent. Replaced
with a realistic fixture (`_realistic_source`: defs at column 0 matching the toy
graph's L10/L30/L50, bodies indented, one module-level statement) and split into
four tests, one per guard. Each carries a `root=None` contrast so it cannot pass
vacuously.

### Two additional bugs found and fixed during Phase 1

Both were **silent**: no error, no exit code, just the pre-guard behavior.

1. **The guards were inert in the live deployment.** `edge_inject` derived the
   repo root with `Path(graph_path).resolve().parent.parent`, and `.resolve()`
   *follows symlinks*. In the connected repo `graphify-out` is a link into
   `.graphify-cache/master/`, so the computed root was
   `C:\projects\appsec-fix-layer\.graphify-cache` — a directory under which no
   source file exists. Every file-reading guard therefore did nothing, in
   exactly the deployment they were written for. Fixed with
   `graphio.repo_root_for()` (`os.path.abspath`, which normalises without
   following links), and pinned by a regression test that builds a real
   symlink/junction. Verified live: a line 5,000 past EOF now returns `None`
   where the old path returned `materializeRepository()`.
2. **`triage` passed no root at all** (`triage.py:36`), so the guards never ran
   in the main agent-facing entry point even when they worked elsewhere. `root`
   is now threaded `triage_report` → `triage_one` → `resolve_target`.

**Note for Phase 4:** the new symlink regression test reuses
`branch_cache._make_link` rather than `Path.symlink_to`, because plain symlink
creation fails under this machine's user-profile temp (no Developer Mode) — a
test that skips on the platform where the bug was found protects nothing.

---

## Phase 1 — original instructions (kept for reference)

**What to implement:** nothing new. Verify or revert what is already on disk.

1. Run `.venv/Scripts/python.exe -m pytest tests -q`.
2. For each failure, classify against `graphify_ext/graphio.py:133-212` (the new
   docstring states each guard's intent):
   - **Fixture unrealistic** → fix the fixture. `test_line_past_end_of_file_is_unresolved`
     (`tests/test_fix_context.py:63-65`) writes column-0 lines; rewrite it to
     write an indented function body so it tests the length bound it was written
     for, not the top-level guard.
   - **Guard wrong** → revert `graphio.py` to nearest-preceding-callable and
     re-plan. Do not "fix" a test to hide a real regression.
3. Re-run the taint corpus: `python corpus/validate_taint.py --build both`
   (was 24/24; both builds share resolution semantics via `crg/crg_graphdb.py`).
4. Re-run `python bench/regression_a.py` (19/19) — A1 proves ext still produces
   a graph identical to stock.

**Verification checklist**
- [ ] `pytest tests -q` green, with every changed test justified in the commit message
- [ ] `corpus/validate_taint.py --build both` still 24/24
- [ ] `bench/regression_a.py` still 19/19
- [ ] `C:\Python314\python.exe -c "import graphify_ext.graphio"` imports clean
      (the live repo's interpreter, not just `.venv`)
- [ ] Read-only smoke on the connected repo:
      `cd C:\projects\appsec-fix-layer && C:\Python314\python.exe -m graphify_ext blast-radius "MaterializedRepository" --depth 2`

**Anti-pattern guards**
- Do not weaken a guard to make a test pass.
- Do not leave `graphify_ext/` half-verified across a session boundary.

---

## Phase 2 — Lock boundary resolution into permanent ground truth (gap #2) — ✅ DONE

**Outcome: green. Gap #2 is now closed and regression-protected.**

| check | result |
|---|---|
| `corpus/validate_taint.py --build both` | **36/36** (was 24/24; +12 = 6 boundary cases × 2 builds) |
| `pytest tests -q` | **77 passed, 1 skipped** |
| `crg/verify_taint.py` · `crg/verify_config.py` | **16/16** · **25/25** |

**What was added**
- `ground_truth.json` gained `boundary_module` + `boundary_cases` (B1–B6),
  expressed **declaratively** (function name / assignment name / `past_eof`)
  and resolved to lines through the AST by `boundary_positions()` — no
  hardcoded line numbers, matching the existing `flows` style.
- `validate_taint.py` gained `boundary_positions()`, `classify()` and
  `check_boundaries()`, wired into **both** build runners as case id `M8`.
- Builds are compared on the resolved **name** (`function` / `file` / `none`),
  never on node ids — the two builds use different id schemes by design, so an
  id comparison would test the schemes rather than the resolution.

**Both builds agree on all six cases**, despite deriving them differently:
Build B from real `line_start`/`line_end` extents, Build A from the guards.
That convergence is the strongest available evidence the guards reproduce true
containment rather than merely passing a bespoke test.

**Mutation test (the anti-vacuity check).** Run against a *copy* of the package
so the live connected repo was never modified:

| run | result |
|---|---|
| control (unmutated copy) | 18/18, 0 M8 failures |
| Guard 3 (top-level) disabled | **16/18 — B3 and B5 FAIL** |

They fail with precisely the original bug: L20 resolves to `first_function`,
L28 to `second_function`. The cases bite.

**Note:** running the corpus from a copied tree requires the `.venv`
interpreter, not `C:\Python314` — `validate_taint.py` derives the CLI directory
from `Path(sys.executable).parent`, which is the Scripts dir for a venv but not
for a system Python.

---

## Phase 2 — original instructions (kept for reference)

Gap #2 is the doc's own "act on this before connecting to an agent platform"
item. The fix is in; the *regression protection* is not.

**What to implement**

1. Wire the existing `corpus/vuln_app/boundaries.py` into
   `corpus/ground_truth.json` — add a `boundary_cases` block in the **same
   AST-resolved style** already used for `flows` (see `validate_taint.py`'s
   `call_lines()` / `flow_locations()`, which resolve lines via `ast` rather
   than hardcoding them). Cases, with measured ground truth:
   - `def` line itself → that function
   - in-body line → enclosing function
   - module-level assignment after a function ends → **file node**
   - module-level assignment after the last function → **file node**
   - line past EOF → unresolved
2. Add matching checks to `corpus/validate_taint.py` (new `M8` case id,
   following the existing `check(build, cid, name, ok, detail)` pattern) so both
   builds are exercised. Build B has real `line_end` extents and should pass by
   construction; Build A passes via the guards.

**Documentation references**
- `graphify_ext/graphio.py:133-212` — the three guards and why each exists
- `crg/crg_graphdb.py` `resolve_location()` — Build B's extent-based path
- `corpus/README.md` — the in-scope / out-of-scope framing to preserve

**Verification checklist**
- [ ] `python corpus/validate_taint.py --build both` passes with the new cases
- [ ] Deliberately revert one guard locally → the new cases FAIL (proves they bite)
- [ ] `corpus/README.md` ground-truth table updated to list boundary cases

**Anti-pattern guards**
- Do NOT hardcode line numbers — resolve via `ast`, as the existing corpus does.
- Do NOT assert Build A and Build B produce identical node ids; they use
  different id schemes. Compare resolved **function names**, as the corpus does.

---

## Phase 3 — Relation-typed traversal (gaps #1 + #3) — ✅ DONE

**Outcome: green. Gaps #1 and #3 closed.**

| check | result |
|---|---|
| `pytest tests -q` | **81 passed, 1 skipped** (was 77) |
| `corpus/validate_taint.py --build both` | **36/36** |
| `bench/regression_a.py` | **19/19** |
| `test_default_relations_superset_of_upstream` | passes (defaults unchanged) |
| `crg/verify_taint.py` · `crg/verify_config.py` | 16/16 · 25/25 |
| Connected repo | clean, still linked, hooks still ext variant |

**What shipped**
- `blast-radius --relation REL` (repeatable; empty means the default set,
  matching stock graphify's `affected --relation`).
- `blast-radius --include-containment` — the opt-in LocAgent-style
  "what is in this class/file" query. Verified on the connected repo: it
  surfaces the containing `repository-source.ts` file node, which the default
  structural walk does not reach.
- `blast-radius --list-relations` — prints what a given graph actually
  contains and which relations are followed by default, so the flag is
  discoverable rather than guessed.
- `MEMBER_RELATIONS` made public (the CLI depends on it); `_MEMBER_RELATIONS`
  kept as an alias.
- `relations` threaded through `triage_report` → `triage_one` → both
  `blast_radius` **and** `_neighbors`, closing the drift.
- Closure policy documented as a **decision**: `relations` bounds which edges
  are FOLLOWED, closure bounds what is REPORTED. Filtering closure too would
  hide injected `taints`/`tests`/`reads_config` edges — never in the structural
  set — so a narrowed walk would discard the very context it was narrowed to
  find. Pinned by a new test.

**`contains` stays out of `DEFAULT_RELATIONS`** — confirmed on the connected
repo it is 4,256 of 6,944 edges (61%), so following it by default floods every
radius.

### §1.5 answered: the budget constraint is tokens, not `--max-nodes`

Measured on the hottest callable node in the connected repo (`runFix()`, degree 22):

| variant | nodes | edges | truncated | ~tokens |
|---|---|---|---|---|
| default, up, d2 | 4 | 4 | no | 656 |
| default, **both**, d2 | 50 | 122 | no | **14,681** |
| default, both, d3 | 71 | 172 | no | 20,724 |
| **`--relation calls`**, both, d2 | 23 | 25 | no | **4,151** |
| `--include-containment`, both, d2 | 58 | 131 | no | 16,053 |

`--max-nodes 500` never fires, so `truncated` stayed `False` while the caller
spent ~15k tokens. A node count is a poor proxy for context budget. The radius
now reports `estimated_tokens` (and the CLI prints it), so the token-bounded
claim is measurable rather than aspirational. Relation filtering is the
effective lever: **3.5x reduction** on the same walk.

**Follow-up not taken (out of plan scope):** `triage` accepts `relations` in the
Python API but has no `--relation` CLI flag, so an agent can narrow
`blast-radius` but not `triage`. Worth adding for consistency.

---

## Phase 3 — original instructions (kept for reference)

Gaps #1 and #3 collapse into one: `contains`/`method` exist (61% of edges) but
are excluded from traversal, and the fix for both is letting the caller choose
the relation set. **The core already supports this** (`blast_radius.py:60`).

**What to implement**

1. **CLI flag.** Add a repeatable `--relation` to `blast-radius`, copying the
   `action="append"` style already in the file at `__main__.py:95-96`, inserted
   at `__main__.py:58` (before `--json` / `_graph_arg`, keeping option ordering).
   Resolve empty to default exactly as upstream does at
   `graphify-upstream/graphify/cli.py:1386`.
2. **Forward it** at `__main__.py:120-121` (`relations=...`).
3. **Add an opt-in containment path** — either `--include-containment` or
   documented `--relation contains --relation method` — so the LocAgent-style
   "what is in this class/file" query becomes reachable. Keep `contains` OUT of
   `DEFAULT_RELATIONS`: upstream CRG documents why containment is not traversed
   by default (`crg/crg-upstream/code_review_graph/constants.py`,
   `IMPACT_EDGE_DIRECTIONS` marks CONTAINS as `IMPACT_DIRECTION_NONE` because a
   changed file already seeds every node in it and containment bridges into
   unrelated structure). Opt-in, not default.
4. **Close the `_neighbors` drift** (`triage.py:49`): thread the same relation
   set through `triage_one` / `triage_report` (both already have keyword-only
   blocks — `triage.py:86`, `:130-131`) and pass it to both `blast_radius`
   (`:95`) and `_neighbors`.
5. **Decide the closure policy explicitly** for `blast_radius.py:124-131` and
   write the decision into the docstring. Recommended: **keep closure
   universal** (current behavior) so injected taint/test/config edges stay
   visible inside a narrowed radius — that is the entire point of the closure
   pass — and say so, in the docstring, as a decision rather than an oversight.
   `tests/test_fix_context.py:106` already pins it.

**Verification checklist**
- [ ] `--relation calls` returns a strict subset of the default radius on a real node
- [ ] `--relation contains --relation method` on a class returns its members
      (the query the research doc says an agent currently cannot make)
- [ ] Under a narrowed traversal, every node in triage's `neighbors` also appears
      in `blast_radius.nodes` (the drift flagged by discovery)
- [ ] `tests/test_hooks_ext.py::test_default_relations_superset_of_upstream` still passes
- [ ] New test passing a non-default `relations` — currently **untested despite existing**
- [ ] Measure on the connected repo (research doc §1.5, still unanswered):
      `blast-radius <hot node> --direction both --depth 2` — does it hit
      `--max-nodes` on structural noise before reaching taint/test/config edges?
      Record the number; it decides whether the default depth is right.

**Anti-pattern guards**
- Do NOT add `contains` to `DEFAULT_RELATIONS` — it floods every radius.
- Do NOT change `blast_radius()`'s signature; only pass the parameter it has.
- Do NOT narrow traversal while `_neighbors` still reads the module constant.

---

## Phase 4 — MCP surface for Build A (gap #4) — ✅ DONE

**Outcome: green. Build A now speaks MCP, matching Build B.**

| check | result |
|---|---|
| `pytest tests -q` | **87 passed, 1 skipped** (was 81; +6 MCP) |
| `corpus/validate_taint.py --build both` | 36/36 |
| `bench/regression_a.py` | 19/19 |
| Connected repo | clean, still linked, CLI works |

**What shipped** — `graphify_ext/mcp_server.py` exposing five tools:
`blast_radius_tool`, `overrides_tool`, `triage_tool`, `verify_fix_tool`,
`list_relations_tool`. Console script `graphify-ext-mcp`; optional extra
`pip install -e ".[mcp]"`.

Copied from crg rather than invented: module-scope `FastMCP(name, version=,
instructions=)`, bare `@mcp.tool()`, plain-dict returns, errors returned as
data (`{"status": "error", ...}`), `--tools` allow-list via
`mcp.local_provider.remove_tool`, and stdio with `show_banner=False`.

**fastmcp is optional and stays that way.** It is **not installed in
`C:\Python314`** — the connected repo's interpreter — and was deliberately not
added there: the hook path (`branch_cache`) must import without it. The
`fastmcp` import lives inside `build_server()`, so `import graphify_ext.mcp_server`
succeeds without fastmcp and `main()` exits with a clear install hint instead of
a traceback. Verified on the live interpreter.

**One deliberate divergence from crg.** crg sets
`WindowsSelectorEventLoopPolicy` before `mcp.run` to pre-warm
sentence-transformers/torch on the main thread. This server does pure JSON work
— no torch, no threads, no subprocesses — and the call is deprecated in Python
3.14 (removed in 3.16). Verified by removing it and re-running the stdio suite:
**6/6 still pass**. Copying it would have shipped a deprecated call for a reason
that does not apply. Recorded in the module docstring.

**Tests are real, not mocked** (`tests/test_mcp_server.py`): each spawns the
actual server as a subprocess and drives it with the MCP client SDK, using the
client pattern from `crg/test_triage.py`. They cover tool enumeration, a scoped
subgraph, relation filtering through the tool, errors-as-data, default-membership
reporting, and that `--tools` genuinely removes tools from `list_tools()`.

Verified live against the connected repo's graph over a real stdio session:
`runFix` default `50 nodes, 122 edges, ~14730 tokens` vs `--relations ["calls"]`
`23 nodes, 25 edges, ~4157 tokens`.

---

## Phase 4 — original instructions (kept for reference)

Build B speaks MCP; Build A is CLI-only, so an agent must shell out and parse
stdout. Copy crg's pattern verbatim rather than inventing one.

**What to implement**

1. New `graphify_ext/mcp_server.py`. Copy structure from
   `crg/crg-upstream/code_review_graph/main.py:11-20` (imports), `:90-98`
   (server object), `:415-429` (one sync tool), `:1024-1032` (zero-arg tool),
   `:1177-1264` (`main()` plus both transports).
2. Wrap existing commands as thin adapters calling the **existing functions**
   (`blast_radius.blast_radius`, `blast_radius.overrides_of`,
   `triage.triage_report`, `verify_fix.check`) — keep impl modules free of
   fastmcp imports, matching crg.
3. Return plain dicts; encode errors as `{"status": "error", "error": ...}`
   (`tools/_common.py:22-26`).
4. Add a `--tools` allow-list by copying `_apply_tool_filter`
   (`main.py:1114-1173`), including `mcp.local_provider.remove_tool`.
5. Register a console script in `pyproject.toml` beside the existing
   `graphify-ext` entry point.

**Verification checklist**
- [ ] `fastmcp` present in the target interpreter — **check `C:\Python314`, not
      just `.venv`**; crg's deps were only installed into `.venv`
- [ ] Server starts over stdio with `show_banner=False`
- [ ] Drive it with a client copied from `crg/test_triage.py:90-109` (spawn) and
      `:75-87` (unwrap `structuredContent`)
- [ ] `--tools` filtering actually removes tools from `list_tools()`
- [ ] Windows event-loop policy set before `mcp.run()` (`main.py:1221-1231`)

**Anti-pattern guards**
- Do NOT make fastmcp a hard dependency of `graphify_ext` — it is deliberately
  dependency-free so the JSON commands work anywhere. Use an optional extra.
- Do NOT install fastmcp into `C:\Python314` without checking it does not
  disturb the connected repo's hooks.

---

## Phase 5 — At-scale taint/config validation (gap #5) — ✅ DONE

**Outcome: green, and it found a real adapter defect the toy corpus could not.**

Run against the **connected repo** (`appsec-fix-layer`, 105 TypeScript/`.mjs`
source files, 5,059-node graph) with **real Semgrep 1.173.0**. Semgrep's native
Windows core fails rule validation (`semgrep-core rule validation failed`), so
the scan ran **under WSL** against the same working tree.

### Taint mapping

| measure | value |
|---|---|
| semgrep findings emitted | 9 |
| convertible to edges | 9 |
| skipped (reported, not dropped) | 0 |
| resolved onto graph nodes | 4 |
| unresolved (reported) | 2 |
| collapsed by node-level dedup | 3 |

**The 4/9 figure is not loss.** Seven findings resolved to four nodes because
several sit inside the same function (`evaluation/src/cli.ts` L121/L137/L138 all
inside `main()`); the graph is node-level, so they dedup. Reporting "4 applied
from 9" without that breakdown would misread as a 44% failure.

**Spot-check: correct wherever it resolved.** Every resolved finding was checked
against the real source — `evaluation/src/cli.ts:121` is inside `async function
main()` at L99, `orchestrator/src/cli.ts:85` inside `main()` at L82. Both
correct.

**The 2 unresolved are a stock-graphify extraction gap, not a mapping error.**
`fix-layer/scripts/codemod-runner.mjs` is 7.3 KB, is listed in graphify's
manifest, and contains `function main()` at L131 — but has **0 nodes in the
graph**, while sibling `.mjs` files have 5–6 each. The mapper correctly reported
the findings as unresolved instead of attaching them to a neighbouring file.
That is the "report, don't guess" design working on real data.

### Real defect found and fixed: `from_semgrep` dropped every real finding

**All 9 findings had no `dataflow_trace`** — semgrep emits one only when there
is a multi-step path, and these have source and sink in the same expression.
`from_semgrep` required a trace, so on this real scan it produced **0 edges from
9 legitimate taint findings, silently**. Exactly the toy-corpus/real-repo gap
the research doc predicted.

The naive fix — treat every trace-less finding as taint — is wrong, and
measurement shows why: semgrep's JSON carries **no indication of whether a rule
ran in taint mode** (`metadata: {}`, `engine_kind` only, no mode field). That
would label ordinary pattern matches as taint-exposed. So the behaviour is now
explicit in both directions:

* trace present → `taints` + `reaches_sink` from the traced source (unchanged);
* no trace, rule declared taint via `--taint-rule ID` / `--assume-taint` →
  self `reaches_sink` edge marking the node sink-reaching, with no invented
  source;
* otherwise → recorded in a new `skipped` list and printed by the CLI.

Nothing is dropped silently and nothing is over-claimed. Two regression tests
pin both directions.

### Config linkage

| measure | value |
|---|---|
| env-var read sites | 19 across 10 files |
| distinct vars read | 7 (`PATH`, `HOSTNAME`, `TOKEN`, `SEMMLE_DIST`, …) |
| definitions found | 2 (`GEMINI_API_KEY`, `OLLAMA_API_KEY`) |
| vars both read and defined | **0** |
| edges emitted | **0 — correct** |

Zero is the right answer: there is genuinely no overlap between the variables
this code reads and those its config files define. The pass declines to invent
links rather than producing a reassuring non-zero number.

### Method notes

- The measurement ran against a **copy** of the graph; the connected repo was
  never mutated. Verified after: 5,059 nodes / 6,944 edges, 0 injected edges,
  `git status` clean.
- A resolution *rate* measures the mapper, not Semgrep. The rules used here are
  two hand-written TS taint rules plus `p/javascript`; a different ruleset would
  change the numerator without saying anything about mapping quality.
- `pytest tests -q`: **89 passed, 1 skipped** (was 87).

---

## Phase 5 — original instructions (kept for reference)

The corpus proves **mapping fidelity**, not **recall** on a real codebase.

**What to implement**

1. Run a real Semgrep taint scan against `C:\projects\appsec-fix-layer` (or a
   large OSS repo) and inject via `graphify-ext inject --semgrep`.
2. Record: findings emitted, findings resolved, findings **unresolved** — the
   unresolved list is the real output, since the injector reports rather than
   drops them.
3. Spot-check ~20 resolved edges by hand, with Phase 2's boundary cases
   specifically in mind.
4. Re-run `config-scan` at scale and record the same three numbers. On the
   connected repo it currently yields 0 edges — **correctly**: code reads
   `BOOK_LANG`/`HOST`/`PATH` while `.env` defines `GEMINI_API_KEY`/
   `OLLAMA_API_KEY`, so there is no overlap. Do not "fix" that to a nonzero
   number.

**Verification checklist**
- [ ] Resolution rate recorded, unresolved reasons categorised
- [ ] Manual spot-check of >= 20 edges; disagreements filed as corpus cases
- [ ] Any newly-found mis-attribution class added to `corpus/ground_truth.json`

**Anti-pattern guards**
- Do NOT report a resolution *rate* as an accuracy claim — it measures the
  mapper, not Semgrep. Keep `corpus/README.md`'s scope framing.
- Do NOT run a write-mode inject against the connected repo before Phase 1 is green.

---

## Phase 6 — Final verification — ✅ DONE — **PLAN COMPLETE**

Every suite re-run from scratch after all five phases (`bench/phase6-sweep.log`):

| suite | result |
|---|---|
| `pytest tests -q` | **89 passed, 1 skipped** (73 at plan start) |
| `bench/regression_a.py` | **19/19** |
| `bench/bench_b.py --mode link` | **31/31** — revisit 4.5 s vs stock 9.2 s = **2.0x** |
| `bench/bench_b.py --mode copy` | **31/31** — revisit 6.1 s vs stock 10.0 s = 1.6x |
| `bench/cross_c.py` | **11/11** |
| `corpus/validate_taint.py --build both` | **36/36** |
| `crg/verify_req1 · req2 · taint · config` | **14/14 · 11/11 · 16/16 · 25/25** |

**Anti-pattern greps — all clean:**

| check | result |
|---|---|
| `_tool_manager` (removed in fastmcp>=3) | only in comments explaining why it is *not* used |
| stdio `mcp.run` without `show_banner=False` | none |
| `contains`/`method` leaked into `DEFAULT_RELATIONS` | no — 14 relations, containment opt-in |
| `resolve().parent.parent` repo-root derivation (symlink trap) | none — `repo_root_for` everywhere |
| module-scope `fastmcp` import (would break the hook path) | none — lazy only |

**Connected repo healthy:** `master`, `git status` clean, `graphify-out` still a
link to `.graphify-cache/master`, both hooks report the graphify-ext variant,
graph unchanged at 5,059 nodes / 6,944 edges with **0 injected edges** —
confirming the Phase 5 at-scale measurement ran against a copy and left the live
repo untouched.

**Docs reconciled:** `CUSTOM-BUILD-GUIDE.md` (new flags, MCP section, refreshed
counts), `TEST-RESULTS.md` (follow-up section; the stale "nothing is connected"
and "six bugs" claims corrected to ten), `CODE-GRAPH-RESEARCH.md` (status block
marking all five gaps closed, plus the three findings that correct the original
analysis), `corpus/README.md` (boundary cases).

### Outstanding, deliberately not done

- `triage` accepts `relations` in the Python API but has no `--relation` CLI
  flag, so an agent can narrow `blast-radius` but not `triage`.
- Stock graphify has an **extraction gap**: `codemod-runner.mjs` sits in the
  manifest with 0 nodes while sibling `.mjs` files extract fine. Upstream issue,
  not a fix-layer one; it is the sole cause of the 2 unresolved findings in
  Phase 5.
- `estimated_tokens` is a ~4-chars-per-token approximation, not a tokenizer.

---

## Phase 6 — original instructions (kept for reference)

Run the whole suite and confirm the docs match reality.

- [ ] `pytest tests -q` (was 73 passed, 1 skipped)
- [ ] `bench/regression_a.py` (19/19)
- [ ] `bench/bench_b.py --mode link --iterations 3` (31/31)
- [ ] `bench/bench_b.py --mode copy --iterations 3` (31/31)
- [ ] `bench/cross_c.py` (11/11)
- [ ] `corpus/validate_taint.py --build both` (24/24 plus new boundary cases)
- [ ] `crg/verify_req1|req2|taint|config.py` (14 / 11 / 16 / 25)
- [ ] Connected repo still healthy: `git status` clean, `graphify-out` still a
      link to `.graphify-cache/master`, hooks still report "graphify-ext variant"
- [ ] Update `CUSTOM-BUILD-GUIDE.md`, `TEST-RESULTS.md` and
      `CODE-GRAPH-RESEARCH.md` §2 with what actually changed
- [ ] Grep for anti-patterns: `_tool_manager`; a `mcp.run(transport="stdio"`
      without `show_banner`; `contains` inside `DEFAULT_RELATIONS`

**Per the version-compatibility contract in `CUSTOM-BUILD-GUIDE.md`: the
connected repo runs graphifyy 0.9.47 while the suites were verified against
0.9.53. Re-run the suites after any upstream upgrade.**

---

## Sequencing

1 (blocking) → 2 → 3 → 6 is a coherent shippable increment.
4 and 5 are independent and can be deferred without blocking anything.

| phase | risk | why |
|---|---|---|
| 1 | **HIGH** | unverified resolver live in a real repo |
| 2 | low | test-only |
| 3 | medium | touches shared traversal used by triage |
| 4 | low | new file, additive |
| 5 | low | read-only measurement |
