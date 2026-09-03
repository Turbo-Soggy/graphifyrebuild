# Upstream report — **FILED PUBLICLY 2026-09-03**

> **Public issue: https://github.com/Graphify-Labs/graphify/issues/3302** — this is the live report.
>
> **Routing history, and a correction.** It was first sent through GitHub private vulnerability
> reporting (`GHSA-7hhr-924m-gwrf`) because SECURITY.md forbids public issues for
> security-relevant reports. That was the wrong call: this is a **correctness defect, not a
> vulnerability in graphify**, and the private channel is slower and invisible to other users.
> Refiled publicly at the user's direction. The public issue asks the maintainers to close the
> private report as a duplicate — **the reporter cannot close it via the API** (403: requires
> administrative/security management rights), so it can only be withdrawn from the web UI.
>
> The submitted text says plainly that this is a *correctness* defect rather than a
> vulnerability in graphify, explains why it was still escalated (security tooling built on the
> graph silently under-counts attack surface), states that no embargo is needed, and invites the
> maintainer to convert it to a public issue if they consider it out of channel — which may be
> the faster route to a fix.
>
> Their policy commits to acknowledging within 48 hours.

---

## Original draft (as submitted)

**Target:** https://github.com/Graphify-Labs/graphify/issues (canonical: `safishamsi/graphify`
301-redirects to the same repo id `1200597263`; issues are open, last push 4 days ago)
**Label:** `bug` · **Repo has no ISSUE_TEMPLATE and no CONTRIBUTING**, so this follows the
house style observed in the tracker: `<Area/language>: <symptom> — <consequence>`, body
leading with `**Version:**`, then `## Summary` / `## Reproduction` / `## Root cause`.

It contains no code from any private repository; the reproducer is synthetic.

---

## Title

`Python: methods differing only by a leading underscore collide on node id — the public one is dropped from the graph`

---

## Body

**Version:** 0.9.53 (also reproduced on 0.9.47)
**Platform:** Windows 11, Python 3.14.7
**Command:** `graphify extract . --code-only --no-cluster`

### Summary

When a class defines both `_foo` and `foo`, only one node reaches `graph.json`. Node ids appear
to be slugified in a way that strips the leading underscore, so `_get_connection` and
`get_connection` both become `<module>_<class>_get_connection` and the second definition
silently overwrites the first. No warning is emitted and the file is not reported as
partially extracted.

The consequence is that a public API method can be **entirely absent** from the graph while its
private sibling is present under the public name — so `affected`, `explain`, `query` and `path`
cannot see it, and any tool built on the graph inherits the blind spot.

### Reproduction

`mod.py`:

```python
class Adapter:
    def _get_connection(self, url):     # L2
        return url

    def get_connection(self, url):      # L5
        return self._get_connection(url)

    def unrelated(self, x):             # L8   <- control
        return x
```

```console
$ graphify extract . --code-only --no-cluster
[graphify extract] wrote graphify-out/graph.json — 4 nodes, 3 edges (no clustering)
```

Nodes for `mod.py`:

```
mod                        | mod.py            | L1
mod_adapter                | Adapter           | L1
mod_adapter_get_connection | ._get_connection() | L2
mod_adapter_unrelated      | .unrelated()      | L8
```

**Expected:** three method nodes (`_get_connection`, `get_connection`, `unrelated`).
**Actual:** two. `Adapter.get_connection` (L5) is absent; the id that would name it is held by
`_get_connection` (L2).

The `unrelated` control extracts correctly, so this is specific to the underscore-differing
pair rather than to methods generally.

### Real-world impact

Found while benchmarking context retrieval against `psf/requests` at commit `aa1461b6^`.
`src/requests/adapters.py` defines both `HTTPAdapter._get_connection` (L377) and
`HTTPAdapter.get_connection` (L406). The graph contains one node,
`src_requests_adapters_httpadapter_get_connection`, pointing at **L377**. The public
`get_connection` at L406 has no node at all — only a stray docstring node at L407.

In a 25-symbol ground-truth set drawn from that repo's own fix commits, this defect accounted
for a measurable share of symbols that no graph query could reach.

### Root cause (hypothesis)

Id generation appears to normalise identifiers by stripping non-alphanumeric characters,
including the leading `_`, without disambiguating collisions. Two candidate fixes:

1. preserve the underscore in the id (e.g. `mod_adapter__get_connection`); or
2. detect an id collision at insert time and suffix/disambiguate rather than overwrite.

(2) is the more robust of the two, since the same collapse presumably affects other
character-stripping cases (`foo_bar` vs `fooBar` vs `foo-bar`, dunder names, and so on) —
worth a test that asserts *node count equals definition count* for a file, which would catch
the whole class.

### Notes

- **This is not #387.** That was an all-`.mjs` failure caused by a missing `_DISPATCH` entry,
  fixed in v0.4.16. Here the language is Python, the dispatch path is plainly working (siblings
  in the same class extract fine), and the loss is per-symbol rather than per-file.
- Possibly related in mechanism to #2281 (JS/MJS node ids minted from absolute paths) — both are
  id-shape defects that silently change which node exists.
- Searched the tracker for `mjs`, `"0 nodes"`, `"no nodes"`, `manifest nodes`, `javascript
  extraction`, `silently skipped file`, `"partial extraction"` before filing; no open issue
  appeared to describe this. With ~1,222 open issues and inexact substring search, that is
  "no match found" rather than "not reported".

---

## Second, weaker candidate — hold unless asked

A separate observation from the same work: in a private TypeScript repository,
`codemod-runner.mjs` appears in graphify's manifest but yields **0 nodes**, while sibling `.mjs`
files in the same directory yield 5–6 each.

**Do not file this yet.** Unlike the Python bug above it has no minimal public reproducer, the
file cannot be shared, and the root cause is unidentified — it could be a grammar rejection
(cf. open #2922, where a bare `&` in JSX makes tree-sitter reject an otherwise valid file), a
size truncation (#2225), an object-literal-only module (#2419), or a failed chunk that still
wrote a manifest entry (#3093). A report that cannot be reproduced by the maintainer and does
not name a cause is likely to be closed. Reduce it to a synthetic reproducer first, or drop it.
