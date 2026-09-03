# Build B (`crg/`) — FROZEN 2026-09-03

This directory is **no longer maintained**. It is kept because the evidence it produced is
still valid and still reproducible; it is not kept because anything depends on it.

## What it was

A second, independent implementation of the same per-branch caching + fix-context ideas, built
on **`code-review-graph` 2.3.8** instead of stock graphify. It exists because a superseding
spec (`code-review-graph-customization-spec.md` + `claude-code-implementation-brief.md`) asked
for CRG as the substrate, and the honest way to evaluate that claim was to build it.

## What it proved — re-run at the moment of freezing, not cited from old logs

All four verification scripts were executed on **2026-09-03, immediately before this file was
written and before anything else touched Build B**, so the freeze is a tested checkpoint rather
than an assumption. Log: `bench/agentctx/crg-freeze-check.log`.

| suite | result at freeze | exit |
|---|---|---|
| `verify_req1.py` | **14 / 14 checks passed** | 0 |
| `verify_req2.py` | **11 / 11 checks passed** | 0 |
| `verify_taint.py` | **16 / 16 checks passed** | 0 |
| `verify_config.py` | **25 / 25 checks passed** | 0 |

They match the counts recorded when the work was originally done, so nothing has silently
rotted between then and the freeze.

The substantive finding: **CRG's SQLite schema stores real symbol extents**
(`nodes(qualified_name, kind, file_path, line_start, line_end)`), where graphify's `graph.json`
stores only a start line. That difference is the whole reason `graphify_ext/symbols.py` has to
re-parse files with tree-sitter to recover extents — work CRG would not have needed.

That is worth remembering rather than deleting.

## Why it is frozen and not merged

- **It was never packaged.** `pyproject.toml` contains no `crg` entry and never has; nothing
  installs it, and no `graphify_ext` module imports from it.
- **It was never deployed.** Only Build A was.
- Maintaining two substrates doubles the work of every upstream version bump for a build that
  has no consumer.

Merging "the best of both" would produce a third build, not a consolidation, so that was
explicitly not done.

## What is deliberately preserved

`verify_req1.py`, `verify_req2.py`, `verify_taint.py`, `verify_config.py` and their supporting
modules (`crg_graphdb.py`, `taint_inject.py`, `config_link.py`, `swap_or_build.py`) stay
runnable. A frozen build whose evidence can no longer be reproduced is worth less than one
whose evidence can, so the scripts are not to be moved, renamed, or trimmed.

## When to revive it

If symbol extents, or any other per-symbol structure graphify does not record, become central
rather than recoverable. `AGENT-CONTEXT-COMPARISON.md` §5 shows extents are recoverable from
graphify's graph with tree-sitter, which is why that trigger has not fired. If graphify's
extraction defects (§8 of the same document) prove unfixable upstream, the substrate choice is
worth reopening — and this directory is the head start.

See also: [[crg-implementation-state]] in memory, `plans/01-close-research-gaps.md`.
