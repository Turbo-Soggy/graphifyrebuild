# Code graphs for agentic fixing — research notes + evaluation of `graphifyrebuild`

Scope: what the current research/industry consensus says a code graph should look
like when the consumer is an autonomous (or semi-autonomous) coding agent doing
localization + fix + verification, and how that maps onto the two builds in
`C:\projects\graphifyrebuild` (`graphify_ext` on stock `graphifyy`, and `crg` on
stock `code-review-graph`).

---

## 1. What the research says a "code graph for an agent" should be

### 1.1 Granularity: hierarchical, not flat

The strongest converging signal across sources is that a **flat call graph is
not enough**. LocAgent (ACL 2025) builds a heterogeneous graph with four node
types — **directory → file → class → function** — connected by four edge
types: `contain` (hierarchy), `import` (file-level), `invoke` (call/instantiate),
`inherit` (class inheritance). The stated reason: "issues typically manifest
through call relationships rather than directory structure," so the agent needs
both axes available and needs to be able to jump between them in one traversal
step rather than re-deriving hierarchy from file paths.

RepoGraph (ICLR 2025) goes one level finer and stores **line-level** definition/
reference tags alongside the function-level graph, specifically so an agent
action (`search_repo`) can answer "what touches this line" without falling back
to grep.

**Where this lands on your build — corrected:** graphify's node set
(function/class/file, per `ARCHITECTURE.md`) matches LocAgent's middle two
tiers, and stock graphify already emits `contains` as a first-class relation —
verified against the real graph: 4,256 of 6,944 edges (61%), plus 45 `method`
edges, making `contains` the single most common relation in the graph, not a
missing one. LocAgent's four-relation design is fully present at the schema
level.

What's actually true, verified against `graphify_ext/blast_radius.py`:
`_MEMBER_RELATIONS = ("method", "contains")` is defined and used only to seed
class-member lookups (`overrides`), then explicitly excluded from
`DEFAULT_RELATIONS` — the constant `blast-radius` and `triage`'s `neighbors`
actually traverse. The docstring says so directly: "method/contains hop
(seeds only, never reported as hits)." So the gap isn't a missing edge type,
it's that the one traversal tool an agent would use for "what's in this
class/file" deliberately can't answer that question — `contains` exists in
the graph but is invisible to `blast-radius --direction both`.

The second half of the original claim holds: `graphio.py`'s own schema
docstring documents only `source_location` (`"L<n>"`, a single line, no end)
— confirmed, there is no end-line/span field anywhere in the schema. That's
the real RepoGraph-shaped gap; see the boundary-mis-attribution finding below.

### 1.2 Query surface: tools, not a query language

CodexGraph and the CPG-for-LLMs literature (arXiv 2603.24837, "Bridging Code
Property Graphs and Language Models") converge on the same finding from the
opposite direction: giving an LLM a raw graph query language (Cypher, CPGQL)
reliably produces hallucinated syntax and invalid queries. The fix in every
system reviewed is a **thin tool layer over the graph** — a handful of named,
parameterized operations (`neighbors`, `traverse`, `slice`, `blast_radius`,
`taint_path`) instead of query-language access. LocAgent's three tools
(`SearchEntity`, `TraverseGraph`, `RetrieveEntity`) and CodexGraph's
"semantic primitives" (program slicing, taint tracking abstracted into callable
operations) are both instances of this pattern.

**Where this lands on your build:** this is the one area where both `graphify_ext`
and `crg` are already aligned with best practice, more so than most public
projects — `blast-radius`, `overrides`, `triage`, `test-link`, `config-scan`
are exactly this tool layer, and CRG additionally exposes it as live MCP tools
(`test_triage.py`), which is the more agent-native transport (LocAgent and
CodexGraph both assume the agent calls tools, not that it reads a file).
`graphify_ext`'s equivalent commands are CLI-only; there's no MCP server in
Build A, so an agent has to shell out rather than call a tool. That's a real
asymmetry between your two builds worth resolving in whichever one you keep —
CRG's live-MCP triage client is the more agent-native shape.

### 1.3 Confidence and provenance on every edge

Both graphify's own schema (`EXTRACTED` / `INFERRED` / `AMBIGUOUS`) and the CPG
literature agree edges need a trust label, not just a relation label — an
agent deciding whether to act on "function X calls sink Y" needs to know
whether that's a parsed call or a heuristic guess before it treats it as
grounds for a fix. This is not universal in public tools (RepoGraph's def/ref
edges, for instance, don't appear to carry a confidence field at all).

**Where this lands on your build:** your `EXTERNAL`/`origin: graphify-ext`
addition for injected findings is the correct extension of this pattern — it
keeps analyzer-sourced edges (Semgrep taint, coverage, config) distinguishable
from AST-extracted ones without a second vocabulary or a join layer. This is
one of the stronger design choices in the rebuild, not just an adequate one.

### 1.4 Taint/data-flow as a first-class edge type, sourced externally

The CPG literature treats control-flow + data-flow (program dependence graph)
as one of the three merged layers a code graph needs for vulnerability work —
"taint flows from untrusted sources to sensitive sinks" is called out
explicitly as the reason CFG+PDG get merged with the AST layer rather than kept
separate. Nothing in the papers reviewed suggests an LLM-facing graph should
*compute* taint itself, though — CodexGraph's abstraction argument (pre-built
analyses > agent-synthesized traversals) implies the opposite: taint detection
is a job for a dedicated static analyzer (Semgrep, CodeQL, a real dataflow
engine), and the graph's job is to make the *result* queryable alongside
structural edges.

**Where this lands on your build:** this is exactly the design both builds
made — `graphify-ext inject --semgrep` and `crg/taint_inject.py` both treat
taint as an external finding mapped onto the graph, not a capability the graph
computes. Your honest-limitations section already states this correctly
("Neither build detects taint itself"). This matches the literature's implied
division of labor better than a design that tried to reimplement taint
analysis inside the graph builder would have.

### 1.5 Token-bounded, scoped context — not "hand the agent the graph"

LocAgent's own results (92.7% file-level Acc@5 on SWE-bench Lite) come from
returning a **tree-shaped, hop-and-relation-scoped subgraph**, not a full-graph
dump — and the paper specifically credits "structured, hierarchical output"
over adjacency-matrix or flat-JSON dumps for LLM reasoning quality, independent
of size. graphify's own `dev.to` writeup flags the corresponding failure mode
on the stock tool: BFS traversals return ~1,500 tokens *regardless of
specificity*, i.e. the graph doesn't get more targeted just because the query
was.

**Where this lands on your build:** `blast-radius --depth/--max-nodes` with a
`truncated` flag is the right primitive (scoped, bounded, and honest about
truncation) but it's breadth/depth-bounded rather than *relation-aware* the way
LocAgent's `TraverseGraph` is (agent picks direction *and* entity/relation type
per hop). Whether that gap matters depends on how noisy your call graph's fan-
out is in practice — worth checking whether `--depth 2` around a hot node in
your real repo returns something an agent can act on or something that's
already hit the token budget on structural noise before reaching anything
useful.

### 1.6 Post-fix verification as a graph-diff, not just a test run

This is the one area none of the academic papers reviewed (LocAgent, RepoGraph,
the CPG-bridging paper) address at all — they're localization/generation
benchmarks, not closed-loop pipelines with a "did the fix change what it should
have and nothing else" check. The closer analogue is the "blast radius" framing
from infra/remediation tooling (zof.ai's "Scoping the Blast Radius" post) and
your own build's `verify-fix` command, which is a genuinely custom contribution
rather than an adaptation of a published pattern.

**Where this lands on your build:** `verify-fix`'s zero-tolerance, node-scoped,
field-projected edge-diff (ignoring `source_location`/clustering, catching
added/removed edges and confidence-tier changes) is more rigorous than
anything in the literature reviewed for this specific step, and the documented
failure mode (renames always trip it) is exactly the kind of edge case that
matters for CI gating and that a less careful design would have missed
silently.

---

## 2. Net assessment of the `graphifyrebuild` adaptation

**Is it a good adaptation?** Structurally, yes — more rigorous than most. The
things the published research treats as load-bearing for an agent-facing code
graph are present and, in a few places (confidence/provenance separation,
post-fix diffing), implemented more carefully than the reference designs. The
two builds also did something the papers don't have to: they verified against
a real upstream tool's actual behavior (private-API coupling table, hook
template composition, manifest portability) rather than designing in the
abstract, which is the harder and more valuable half of this kind of work.

> **STATUS (updated after implementation).** All five gaps below have been
> acted on; see `plans/01-close-research-gaps.md` for the phased record and
> `TEST-RESULTS.md` for the suites. Summary of what changed, and what the
> implementation work discovered that this analysis did not:
>
> | gap | outcome |
> |---|---|
> | 1. containment not traversable | **Closed.** `blast-radius --include-containment` (opt-in) plus `--relation`; `contains` stays out of the defaults, matching upstream CRG's own reasoning |
> | 2. no extents → mis-attribution | **Closed.** Three guards in `resolve_by_location`; boundary cases went from **2 of 4 mis-attributed to 6/6 correct**, now pinned in `corpus/ground_truth.json` (M8) and mutation-tested |
> | 3. depth-bounded, not relation-typed | **Closed.** The core already accepted `relations`; it was simply unexposed. Now on the CLI and threaded through `triage` |
> | 4. Build A CLI-only | **Closed.** `graphify-ext-mcp` exposes five tools over stdio/HTTP, copying crg's server pattern; fastmcp kept an optional extra |
> | 5. at-scale validation | **Done, and it found a real defect** — see below |
>
> **Three findings that correct or extend this analysis:**
>
> 1. **§1.5's fan-out concern was right, but about the wrong bound.** On a hot
>    node in the connected repo, `--direction both --depth 2` returns 50 nodes
>    and **~14,700 tokens** while `--max-nodes 500` never fires — so `truncated`
>    stayed `false` while the context cost was large. A node count is a poor
>    proxy for token budget. The radius now reports `estimated_tokens`, and
>    relation filtering cuts the same walk to ~4,200 (**3.5x**).
> 2. **The at-scale run exposed a silent adapter defect the toy corpus could
>    not.** Real Semgrep taint findings on this repo carried **no
>    `dataflow_trace` in 9 of 9 cases** (semgrep emits one only for multi-step
>    paths). `from_semgrep` required a trace and therefore produced **zero
>    edges from nine legitimate findings, silently**. Fixed — and deliberately
>    not "fixed" the naive way: semgrep's JSON carries no indication of whether
>    a rule ran in taint mode, so trace-less findings are mapped only when the
>    caller declares the rule, and are otherwise reported as *skipped*.
> 3. **Unresolved findings were traced to a stock-graphify extraction gap, not
>    to the mapper.** `codemod-runner.mjs` is in graphify's manifest and holds a
>    `main()` at L131 but has **0 nodes in the graph**, while sibling `.mjs`
>    files have 5–6 each. The mapper correctly reported those findings as
>    unresolved rather than attaching them to a neighbour.

> **STATUS 2 (2026-09-03) — measured against a real GitHub repo, and it revises
> the block above.** `AGENT-CONTEXT-COMPARISON.md` benchmarks this build against
> stock graphify *and* against plain ripgrep, on 14 tasks whose ground truth is
> `psf/requests`' own fix commits. Three of the "closed" rows above did not
> survive contact with measurement:
>
> | item | revised status |
> |---|---|
> | **Symbol-level context (new)** | **RESOLVED.** `graphify_ext/symbols.py` + `context.py` + `graphify-ext context` + `context_tool`. Recovers true extents, signatures and decorators via tree-sitter — data graphify does not record at all (`extract.py` writes only `f"L{start}"`). Took follow-up file reads from **80 to 0** across the task set. This, not traversal, is the build's differentiator. |
> | **Guess-refusal is now a typed signal** | **RESOLVED.** `symbols.py` had five distinct failure modes all returning bare `None`, and `context.py` *inferred* a reason — reporting an unreadable file or a parser crash as "probably a docstring node". Now returns a typed `Unresolved(code, detail)`; packs carry `reason_code`, `is_seed`, `seed_resolved`, and the MCP tool returns `status: "partial"` when the seed itself could not be sliced. |
> | Gap 1 (containment opt-in) | **Decision reversed on evidence.** Keeping `contains` out of the defaults costs recall: enabling it lifts recall **0.351 → 0.494** *and* improves precision, because co-changed symbols are frequently siblings or members. `context_tool` now defaults it **on**; `blast-radius`' default is unchanged. |
> | Gap 3 (relation-typed traversal) | **Closed but of little measured value.** `--direction both` returned identical recall to `up` on **14/14** tasks at 4× the tokens. Treat it as a failed feature, not a differentiator. |
> | Gap 5 / extraction ceiling | **IN MOTION upstream.** 12% of ground-truth symbols had **no graph node at all**. Root-caused to a node-id collision (`_foo` and `foo` collapse; the public one is dropped), reproduced minimally with a control, re-verified on **0.9.53**. Filed 2026-09-03 via GitHub private vulnerability reporting: **`GHSA-7hhr-924m-gwrf`** (state `triage`). The older `.mjs` 0-node observation remains **unfiled** — no public reproducer, cause unidentified. |
> | **Relevance ranking (new)** | **SCOPED, NOT STARTED — needs a requirements conversation.** Recall is flat at 0.351 across depth 1→4 under an 8k budget while cost rises 5×; with the budget removed it reaches **0.619 at depth 6**. The right symbols are reachable and are being crowded out by irrelevant neighbours, so **ranking, not traversal depth, is the binding constraint** and the largest remaining lever. Deliberately not begun: the ranking inputs must be agreed first — blast-radius weight, taint-reachability, recency, and test-coverage status — and each needs a defined source and a way to be measured. This is logged so it is not mistaken for forgotten. |
>
> Also worth carrying forward: **stock is not behind on traversal.** `graphify
> explain` beat every `blast-radius` configuration on default-setting recall
> (0.387 vs 0.351), and this build's relation set is byte-identical to stock's
> `DEFAULT_AFFECTED_RELATIONS`. Its one verified traversal win is narrow: stock's
> `affected` seeds a class's members but does not *report* them, so stock returns
> nothing on a class seed where this build returns the members (0.00 vs 1.00).
> And **plain ripgrep had the best recall of any arm at default settings (0.530)** —
> this build only passes it with containment on at depth 3 (0.565), though it
> delivers code and so needs no follow-up reads at all.

**Genuine gaps, ranked by how much the literature says they matter:**

1. **`contains` is emitted but deliberately excluded from traversal, not
   missing.** Corrected after checking the real graph: `contains` is 4,256 of
   6,944 edges (61%) — the single most common relation — plus 45 `method`
   edges. `blast_radius.py` defines them as `_MEMBER_RELATIONS`, uses them only
   to seed `overrides`' class-member walk, and excludes them from
   `DEFAULT_RELATIONS` by design ("seeds only, never reported as hits"). So the
   actual gap is narrower than LocAgent's model implies: an agent asking
   "what's the blast radius of deleting this file/class" via `blast-radius`
   can't traverse containment to get there, even though the graph has the
   edges to answer it. If that query shape matters for the fix workflow, it's
   a traversal-relation change (add `contains`/`method` to an opt-in relation
   set), not a graph-schema change.

2. **No stored function/class extents (start–end line spans) — confirmed, and
   now measured as a live mis-attribution risk.** `graphio.py`'s schema
   docstring documents only `source_location` (a single `"L<n>"`), no end
   line, confirming the nearest-preceding-definition fallback is the only
   resolution path for `file:line → node`. Measured against boundary cases:
   **2 of 4 mis-attributed** — a module-level finding (e.g. a hardcoded
   secret, or a taint source sitting in config/module-level code rather than
   inside a function body) gets attributed to whichever function happens to
   precede it in the file, which does not actually contain it. This is the
   same failure class as the two bugs the taint corpus already caught
   (confidently wrong security context, not a crash or an empty result) —
   the corpus's existing ground truth is expressed as interior-line
   function/call pairs, so it doesn't exercise this path. Worth extending
   `corpus/ground_truth.json` with boundary-line and module-level cases
   specifically, since this is the kind of error a triage pipeline would
   hand an agent as fact rather than surface as uncertain.

3. **Depth/breadth-bounded traversal, not relation-typed traversal.**
   `blast-radius` bounds by hop count and node count; LocAgent's
   `TraverseGraph` additionally lets the caller filter *which* relation and
   entity types to follow per hop. If your real repo's call graphs are dense
   enough that `--direction both --depth 2` routinely hits `--max-nodes`
   before surfacing the taint/test/config edges that actually matter for
   triage, this is where that shows up — the fix is filtering the walk by
   relation set (which `triage`'s `DEFAULT_RELATIONS` constant already
   suggests you're halfway toward) rather than only by distance.

4. **CLI-only surface on Build A vs. live-MCP on Build B.** Not a graph-schema
   issue, but every reference design that specifies an agent-facing query
   surface (LocAgent's tools, CodexGraph's semantic primitives, CRG's own MCP
   server) assumes the agent calls a tool in-loop, not that it shells out to a
   CLI and re-parses stdout. If Build A is the one that ends up connected to a
   real agent platform, wrapping its commands as MCP tools (mirroring what
   `crg/test_triage.py` already does against CRG) closes that gap without
   touching the graph schema itself.

5. **Taint/config linkage still needs at-scale validation**, which your own
   checklist already marks open (`[ ]` in the README) — the corpus-level
   validation (24/24) proves mapping *fidelity*, not recall against a real,
   large, previously-unseen codebase. This is the standard "toy corpus vs.
   real repo" gap and applies to essentially every graph-for-agents system
   reviewed here, not something specific to your build.

Gap #2 is the one worth acting on before this connects to a real agent
platform — it's a security-relevant, silent-failure-mode gap in the same
family as the two bugs your own test suites already caught, not a structural
shortcoming of the overall design. Gaps #1, #3–5 are refinements, not defects.

None of these are "wrong design" findings — they're the difference between a
graph built for a human reading `GRAPH_REPORT.md` (which is what stock graphify
was designed for) and a graph built for an agent doing multi-hop, relation-
filtered, token-budgeted reasoning (which is what your `graphify_ext`/`crg`
layers are pushing it toward, and mostly succeed at).

---

## 3. If you have to pick one build to take forward

Your own `CUSTOM-BUILD-GUIDE.md` already says this plainly and the research
agrees with the reasoning: CRG's native SQLite schema, built-in blast-radius/
test-coverage/change-risk tooling, and live MCP transport are structurally
closer to what LocAgent/CodexGraph assume an agent-facing graph looks like than
graphify's JSON-file-plus-CLI shape is. Build B (`crg`) is described in your
own docs as "the more complete implementation," and nothing in this research
contradicts that — if the goal is a platform-connected agent doing fixes
against a live graph, the MCP-native, DB-backed, git-anchored (via
`git_head_sha` in CRG's own metadata table, which your build correctly
discovered and used instead of re-inventing a sidecar stamp) design is the
smaller number of steps from where you are to "agent calls a tool and gets
scoped, provenance-tagged, token-bounded context back."

---

## Sources

- [LocAgent: Graph-Guided LLM Agents for Code Localization (ACL 2025)](https://aclanthology.org/2025.acl-long.426.pdf)
- [RepoGraph: Enhancing AI Software Engineering with Repository-level Code Graph (ICLR 2025)](https://github.com/ozyyshr/RepoGraph) / [paper](https://arxiv.org/html/2410.14684v2)
- [Bridging Code Property Graphs and Language Models for Program Analysis](https://arxiv.org/html/2603.24837v1)
- [CodexGraph: LLM-Driven Code Graphs](https://www.emergentmind.com/topics/codexgraph)
- [Awesome-Repo-Level-Code-Generation (survey list)](https://github.com/YerbaPage/Awesome-Repo-Level-Code-Generation)
- [Graphify + code-review-graph: Build a Self-Updating Knowledge Graph for AI Coding Agents](https://dev.to/mir_mursalin_ankur/graphify-code-review-graph-build-a-self-updating-knowledge-graph-for-claude-code-and-other-ai-j1m)
- [code-review-graph (tirth8205)](https://github.com/tirth8205/code-review-graph)
- [Scoping the Blast Radius: Using the System Graph to Contain Every Remediation](https://zof.ai/blog/scoping-the-blast-radius-using-the-system-graph-to-contain-every-remediation)
- Project docs read in place: `graphifyrebuild/README.md`, `CUSTOM-BUILD-GUIDE.md`, `TEST-RESULTS.md`, `crg/README.md`, `graphify_ext/triage.py`, `graphify-upstream/ARCHITECTURE.md`, `graphify-upstream/AGENTS.md`
