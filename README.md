# graphify-ext

Customization layer on top of stock [graphify](https://github.com/Graphify-Labs/graphify)
(v8 branch, vendored read-only under `graphify-upstream/`), implementing the two
requirements in `C:\projects\graphify-customization-spec.md`:

1. **Per-branch incremental graph caching** — one cached graph state per branch,
   reused on switch-back, reconciled with the existing incremental primitive
   instead of a full rebuild on every branch switch.
2. **AppSec fix-context layer** — blast radius, override discovery, external
   taint/test/config edge injection, an end-to-end vuln triage pipeline, and
   post-fix edge-diff verification for a coding agent.

Nothing in `graphify-upstream/` is modified; everything lives in the
`graphify_ext` package.

## Install

```
pip install -e .            # into the SAME environment as graphifyy
graphify-ext hook install   # replaces the stock hook bodies in .git/hooks
```

For `uv tool` installs of graphifyy: `uv tool install graphifyy --with graphify-ext`.
The hook installer verifies importability under the pinned interpreter and warns
if graphify_ext is missing there.

---

## Requirement 1: per-branch caching

### Layout

```
.graphify-cache/
├── main/                      # slot: graph.json, manifest.json, reports, meta
├── feature-x-<digest>/        # slash-y branch names get a digest suffix (no collisions)
├── @detached/                 # scratch slot for detached HEAD (always rebuilt)
graphify-out/                  # link (or copy) of the ACTIVE branch's slot
```

`graphify-out/` stays the stable path, so `graphify query`/`explain`/`path` and
every skill/platform install file need zero changes. Upstream explicitly
supports this: its atomic writer resolves symlinks and writes *through* the
link (`graphify.paths._atomic_replace`).

### Link mode vs copy mode

`activate()` tries, in order: symlink → Windows directory junction →
**copy mode**. After creating a link it *functionally verifies* traversal by
creating a directory through it — on this machine an EDR/filter driver breaks
`mkdir` through junctions under the user profile (`WinError 183` for paths that
don't exist) while junctions under `C:\projects` work fine, so a bare
"creation succeeded" check is not enough. In copy mode the slot is copied over
a real `graphify-out/` and mirrored back before every swap (owner recorded in
`graphify-out/.graphify_ext_owner`; previous branch recovered via git's `@{-1}`
when the owner file is missing).

### Verified upstream facts the design rests on (source, not README)

* **`manifest.json` is portable** (`graphify/detect.py::save_manifest`): keys
  are forward-slash repo-relative when saved with `root=` (the rebuild path
  does), values are `{mtime, seen, ast_hash, semantic_hash}` — mtime is only a
  fast path; MD5 content hashes are ground truth (`detect_incremental`). So
  cross-branch reuse is valid: checkout-driven mtime churn on identical files
  costs one hash check, not a re-extract. The v4-era "mtime-based, always
  gitignored" description is obsolete.
* **`graphify update` is NOT manifest-gated** — `_rebuild_code(root)` with no
  `changed_paths` re-extracts the *full* corpus. The spec's plan to run a plain
  `update` after swap would have bought nothing. The swap-back reconcile here
  computes the changed set itself with `detect_incremental(kind="ast")` against
  the slot's manifest and passes it as `changed_paths`.
* **No schema-version field exists in graph.json**, so the version stamp lives
  in a slot-local sidecar `graphify_ext_meta.json` (`{branch, base_commit,
  graphify_version, stamped_at}`). It is deliberately NOT inside
  `manifest.json`: a non-file key would surface as a phantom "deleted file" in
  `detect_incremental`'s corpus sweep. The meta never enters `graphify-out/`
  (copy-mode mirrors would smear one branch's anchor onto another slot).
* **The nohup-on-Git-for-Windows failure the spec warns about is already fixed
  upstream** (#1161): hooks use a Python-native detached launcher. The
  customized hooks inherit it unchanged.

### Hook strategy (spec step 5)

`graphify_ext/hooks_ext.py` composes hook scripts from upstream's own exported
building blocks (`_PYTHON_DETECT` probe, `_WORKTREE_GUARD`, the detached
launcher, rebase/merge guards) and swaps only the rebuild *bodies*:

* `post-checkout` → `branch_cache.swap_or_build()` instead of the stock
  unconditional full `_rebuild_code(Path('.'))`.
* `post-commit` → `branch_cache.post_commit_update(changed)` — the same
  incremental `_rebuild_code(changed_paths=...)` as stock, plus slot stamping,
  external-edge re-application, and the memory/LESSONS refresh.

The stock markers are reused so exactly one graphify block ever exists per
hook: `graphify-ext hook install` replaces a stock block in place; re-running
stock `graphify hook install` reverts (re-run ours to re-apply).
`uv tool upgrade graphifyy` cannot clobber the customization — the hook scripts
live in `.git/hooks`, not site-packages. The composition raises loudly in
`_compose_scripts()` if upstream's template layout drifts.

Spec step 6 (repoint the commit hook's manifest path) is a **no-op by design**:
with `graphify-out` linked to the active slot, `graphify-out/manifest.json`
*is* the slot's manifest.

### Fallback-to-full-rebuild triggers (all implemented, all tested)

| trigger | mechanism |
|---|---|
| no cache slot for target branch | `has_cache()` false → full build |
| history rewrite (rebase/force-push) | `base_commit` no longer `merge-base --is-ancestor` of HEAD → full build |
| detached HEAD | `@detached` scratch slot, cleared + fully rebuilt each time |
| graphify version change | meta `graphify_version` ≠ installed version → full build |

A slot with no meta stamp is trusted (spec: "let update sort it out" — sound,
because reconciliation is content-hash-based).

`git checkout -b` fires no rebuild (HEAD unchanged, upstream #2421); the first
commit on the new branch seeds its slot from the currently active slot before
updating, so the new branch never starts from an empty graph.

### Verification checklist (spec) — status

- [x] Manifest format/portability confirmed in source (see above)
- [x] graph.json schema-version: confirmed absent; version check implemented via slot meta
- [x] checkout A → edit → commit → checkout B → checkout A: pre-switch edit present via
      incremental update, no full rebuild (`tests/test_e2e_branch_cache.py`, steps 3–4,
      run against the real graphifyy package in BOTH link and copy modes)
- [x] history rewrite → full rebuild fires (e2e step 5)
- [x] detached HEAD → full rebuild fires (e2e step 6)
- [x] symlink approach vs skill/install assumptions: upstream writes through symlinks
      (`paths._atomic_replace` docstring + code); in copy mode `graphify-out` is a real
      dir, so consumers are trivially unaffected

---

## Requirement 2: AppSec fix-context layer

### Architecture decision (spec step 2, decided up front): **merge into graph.json**

The agent queries ONE graph; blast-radius, triage, and stock `graphify query`
all see injected edges with no join layer. The cost — rebuilds rewrite
graph.json and drop injected edges — is handled by persisting findings to
`graphify-out/external-findings.json` (slot-local ⇒ per-branch) and
re-injecting after every rebuild (`edge_inject.reapply`, called by both
customized hook bodies). Injected edges carry `confidence: "EXTERNAL"` and
`origin: "graphify-ext"` (stock vocabulary is EXTRACTED/INFERRED/AMBIGUOUS),
so injection is idempotent and provenance is unambiguous.

### Case coverage

| # | case | implementation |
|---|---|---|
| 1 | direct callers/callees | `triage` `neighbors` (1-hop, relation-filtered) |
| 2 | transitive blast radius | `graphify-ext blast-radius "<node>" --depth N [--direction up\|down\|both] [--max-nodes N] --json` — scoped closed subgraph, depth-tagged nodes, explicit `truncated` flag for token budgeting. Relation set is a verified superset of upstream `affected`'s (parity test parses the upstream constant) |
| 3 | overrides | `graphify-ext overrides "<node>"` — method → owning class → transitive subclasses → same-bare-name member |
| 4 | taint reachability | `graphify-ext inject --semgrep out.json` maps Semgrep taint `dataflow_trace` source→sink onto graph nodes as `taints`/`reaches_sink` edges; `triage` filters the radius to the taint-touched subset |
| 5 | test coverage | `graphify-ext test-link --coverage cov.json` (coverage.py JSON with `--cov-context=test` — ground truth) or `--heuristic` (conservative name match; ambiguous names emit nothing rather than falsely claiming coverage) |
| 6 | config/schema linkage | **verified first** (spec checklist): stock extractor emits NO env-var edges — grep of `extract.py` shows `os.environ` only for graphify's own flags. `graphify-ext config-scan` links env-var read sites (py/js/ts/rb/go/java) to defining config files (.env*, compose, Dockerfile, tf, CI yaml) as `reads_config` edges |
| 7 | duplicate patterns | out-of-band by design — `triage` output says to run a pattern search (Semgrep) seeded by the vuln signature; not a graph-connectivity problem |
| 8 | post-fix verification | `graphify-ext verify-fix snapshot --node X` → apply fix + update → `verify-fix check` (exit 2 on unexpected edge delta). Fingerprint excludes community/cluster attrs, so clustering churn is never a false positive |
| 9 | cross-repo | `triage` notes point at `graphify global add` / `merge-graphs` pre-registration (upstream verbs) |

Location→node resolution (`file:line` from a SAST report) uses
nearest-preceding-definition containment (function extents aren't stored in
graph.json), preferring callables; name resolution ports upstream
`affected.resolve_seed`'s ladder so ext and stock commands agree on what a
name means.

### Pipeline

```
graphify-ext inject --semgrep semgrep.json      # taint edges (case 4)
graphify-ext test-link --coverage coverage.json # tests edges (case 5)
graphify-ext config-scan                        # reads_config edges (case 6)
graphify-ext triage vulns.json --out ctx.json   # per-vuln agent context (1,2,3,4,5,6 + notes for 7,9)
# ... agent applies fix ...
graphify-ext verify-fix check                   # case 8
```

`vulns.json`: `[{"id", "description", "file", "line", "function"?}, ...]`.

### Requirement-2 checklist (spec) — status

- [x] Env-var/config edges confirmed absent upstream before building the pass
- [x] Merge-vs-separate-index decided (merge; rationale above) before injector code
- [x] Blast-radius token-bounding: `--depth`/`--max-nodes` caps + `truncated` flag
      (benchmark on your real repo to pick the default depth for your agent's budget)
- [ ] Taint-edge injection validated against a known-vulnerable corpus — adapter +
      resolution are unit-tested; run against your SISA pipeline/corpus to validate
      node-ID landing at scale
- [x] Post-fix edge-diff ignores clustering churn (tested)

---

## Development

```
python -m venv .venv
.venv/Scripts/pip install -e . pytest
.venv/Scripts/pip install -e ./graphify-upstream   # for the e2e tests
.venv/Scripts/python -m pytest tests -q            # 70 tests; e2e runs link+copy modes
```

`tests/test_e2e_branch_cache.py` intentionally runs the full lifecycle twice:
once in the system temp dir (copy mode on this machine — filter driver breaks
junction traversal there) and once next to the project (link mode).
