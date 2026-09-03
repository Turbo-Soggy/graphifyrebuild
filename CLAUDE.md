## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

## claude-mem

This project also has claude-mem's cross-session memory. Prefer it over re-reading files or re-deriving history for anything about *past work* (what was tried, decided, or found in an earlier session):
- Use the `mem-search` skill (or `smart_search`/`smart_outline`/`smart_unfold`) to recall prior findings, decisions, and gotchas before re-investigating something already explored.
- Use `smart-explore` for token-optimized structural search (tree-sitter based) when graphify's graph doesn't cover the question.

Rule of thumb: **graphify** for "what does the code look like / how is it structured" (current-state, symbol/file-level); **claude-mem** for "what did we already learn or decide about it" (historical, session-level). Reach for both before falling back to raw `Read`/`Grep` over the whole tree — that's the expensive path.
