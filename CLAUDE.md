# Working in this repo with the code graph

This repo carries a graphify knowledge graph at `graphify-out/` and the
`graphify-ext` layer on top of it. Use the graph before grepping: it returns
source, not just names, and it tells you what it does not know.

## Fixing a bug: the loop

1. **Find the symbol.** `graphify-ext search "<name>"` lists every candidate
   with file, line and origin. Seeds also accept `path/to/file.py:123`
   (a stack-trace or finding location), a qualified name (`res.json`,
   `Widget.build`) or a node id.
2. **Get the context.** `graphify-ext context "<seed>"` (or the `context_tool`
   over MCP). It prints the seed's source, its neighbours' source ranked by
   depth then relation, an **index** of further symbols as `file:line signature`
   lines, and the **tests** that touch anything shown. Read the trailers before
   trusting the pack:
   - `!!! GRAPH STALE` — files were edited since extraction; line numbers may
     be wrong. Run `graphify-ext refresh` and re-query.
   - `absent from the graph` (`unmodelled`) — definitions in the code you were
     shown that have no node: usually functions nested inside functions.
     Nothing downstream can mention them; read the enclosing body yourself.
   - `unresolved` — a symbol the graph knows but could not slice, with a reason
     code. `definition_mismatch` means the graph is stale for that file.
   - `omitted for budget` — raise `--budget`, or open the index entries.
3. **Widen only as needed.** `--depth 3`, `--budget 12000`, or
   `graphify-ext blast-radius "<seed>" --relation calls --direction up` for
   callers only. `graphify-ext overrides "<method>"` before editing a base
   method: overrides need their own fix.
4. **Snapshot, edit, refresh, verify.**
   `graphify-ext edge-diff snapshot --node "<seed>"` → edit →
   `graphify-ext refresh` (incremental update of the edited files, re-applies
   the ext layer) → `graphify-ext edge-diff check` (exit 2 on an unexpected
   structural change) → run the tests the context listed.

## Rules

- Prefer `graphify-ext context` over `Read` for any symbol the graph knows. It
  is one call instead of several file opens, and it discloses its gaps.
- If a seed does not resolve, do not guess a file: `search` first. If the
  symbol is bound by assignment (JavaScript `obj.method = function`) or shadowed
  by an id collision, run `graphify-ext supplement` once; it materialises those
  definitions and stays on for this output slot.
- After editing code, run `graphify-ext refresh` (or commit; the post-commit
  hook does the same). Do not query a graph the pack has just called stale.
- Numbers about retrieval quality live in `README.md` and
  `plans/04-correctness-roadmap.md`, always with their config in the same
  sentence. Do not repeat one without it.
- For broad architecture questions use stock `graphify query "<question>"`,
  `graphify path "<A>" "<B>"` and `graphify-out/GRAPH_REPORT.md`.

## MCP

`graphify-ext-mcp` exposes `search_tool`, `context_tool`, `blast_radius_tool`,
`overrides_tool`, `triage_tool`, `edge_diff_tool`, `supplement_tool`,
`refresh_tool`, `list_relations_tool` over stdio (`--http` for
streamable-http). Errors come back as `{"status": "error", ...}`; an ambiguous
seed comes back with its candidate list.
