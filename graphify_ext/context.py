"""Assemble a fix-ready context pack: the seed symbol's code plus its neighbourhood.

The measured problem this solves
--------------------------------
Across a 14-task benchmark on real fix commits, every arm — ripgrep, ``graphify
affected``, ``graphify explain``, ``blast-radius`` — returned only *names and
line numbers*. The agent still had to open between 1.4 and 6.1 files per task to
see any code. A graph that can name the right symbol but cannot show it has moved
the agent one step, not all the way.

This module closes that: given a seed node, it returns the actual source of the
seed and of its graph neighbours, ordered so that a token budget is spent on the
most relevant symbols first.

Ordering
--------
A single multiplicative score, ``relation_weight x decay ** depth``, NOT a
lexicographic key.

The previous key was ``(depth, relation_class, label)``, which made depth
dominate absolutely: a depth-3 ``calls`` edge always lost to a depth-1
``imports`` edge, which is backwards for an agent changing code. Measured at the
truncation boundary, 3 of 12 boundaries were being decided by the final
alphabetical tie-break -- i.e. arbitrarily.

Swapping to a relation-major lexicographic order would just move the problem: a
depth-8 ``calls`` edge would then beat a depth-1 ``imports`` edge. A product
lets the two trade off. With ``decay = 0.5``:

    depth-3 calls   1.0 x 0.125  = 0.125   >  depth-1 imports  0.2 x 0.5 = 0.100
    depth-8 calls   1.0 x 0.0039 = 0.004   <  depth-1 imports  0.2 x 0.5 = 0.100

Weights and decay are PARAMETERS, tuned and reported against the benchmark
corpus -- not constants to assert. Ties break on node degree ascending (prefer a
specific symbol over a hub) then node id, so ordering is deterministic and
disclosed rather than silently alphabetical.

Truncation is explicit
----------------------
A symbol larger than ``per_symbol_cap`` is emitted as signature + head with a
``… truncated`` marker and an exact line range, never silently cut. Symbols whose
extent cannot be resolved are listed in ``unresolved`` with their reason rather
than dropped, because a missing symbol the agent does not know is missing is the
failure mode that produces confidently wrong fixes.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import blast_radius as _br
from . import graphio, symbols

# Relation weights: how much an agent editing a symbol needs each kind of
# neighbour. Provisional and benchmark-tunable, not settled fact.
_REL_WEIGHT = {
    # a tainted path to the seed outranks everything except a direct call;
    # provisional pending Phase 2, which makes taint a first-class input.
    "taints": 0.95, "reaches_sink": 0.95,
    "calls": 1.0, "indirect_call": 1.0,
    "method": 0.7, "contains": 0.7,
    "extends": 0.6, "inherits": 0.6, "implements": 0.6,
    "mixes_in": 0.6, "embeds": 0.6,
    "tests": 0.5, "reads_config": 0.5,
    "references": 0.4, "uses": 0.4,
    "imports": 0.2, "imports_from": 0.2, "dynamic_import": 0.2,
    "re_exports": 0.2, "requires": 0.2,
}
_DEFAULT_WEIGHT = 0.3      # an unknown relation sits between references and imports
DEFAULT_DECAY = 0.5

# A dropped symbol is "high rank" when it scored at least as well as the WEAKEST
# symbol that was kept -- i.e. the budget produced an inversion, and the agent is
# looking at a gap that widening the budget would actually close. Scoring this
# against a fixed quantile instead was degenerate on small candidate sets (with
# two candidates, a top-quartile cut makes the floor the maximum, so nothing but
# the single best symbol could ever be flagged).


def score_node(relation: str | None, depth: int, decay: float = DEFAULT_DECAY) -> float:
    """Relevance score for a candidate symbol. Higher is more relevant."""
    return _REL_WEIGHT.get(str(relation or ""), _DEFAULT_WEIGHT) * (decay ** max(0, depth))


def _count(text: str) -> int:
    """Token count. Uses a real tokenizer when one is available, else chars/4.

    The method used is reported in the result so a budget number is never
    mistaken for an exact count when it is an approximation.
    """
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        return max(1, len(text) // 4)


def _token_method() -> str:
    try:
        import tiktoken  # noqa: F401
        return "tiktoken/cl100k_base"
    except Exception:
        return "approx-chars/4"


def _render(sym: symbols.Symbol, role: str, depth: int, relation: str | None,
            cap: int, confidence: str | None = None) -> tuple[str, bool]:
    lines = sym.source.splitlines()
    truncated = len(lines) > cap
    body = "\n".join(lines[:cap]) if truncated else sym.source
    # Show the confidence label for anything not directly extracted from the
    # AST, so an INFERRED `tests` edge never reads like a measured one.
    mark = (f" [{confidence}]"
            if confidence and confidence not in ("EXTRACTED", "") else "")
    via = f" via {relation}{mark}" if relation else ""
    header = (f"=== {role} (depth {depth}{via}) "
              f"{sym.file}:{sym.start}-{sym.end}  {sym.name} ===")
    tail = (f"\n… truncated at {cap} of {len(lines)} lines "
            f"({sym.file}:{sym.start + cap}-{sym.end} omitted)") if truncated else ""
    return f"{header}\n{body}{tail}\n", truncated


def _render_class_summary(sym: symbols.Symbol, role: str, depth: int,
                          relation: str | None, root: Path,
                          confidence: str | None = None) -> str:
    """A RELATED class as signature + member index, not as a body.

    A class body is the sum of its methods, each of which is its own node and
    competes for the same budget on its own merits. Emitting the body too
    spends the budget twice on the same lines -- flask's ``Flask`` class alone
    is ~1,500 lines -- and measured on the corpus it was the single largest
    consumer of body-tier tokens on the tasks that scored zero.
    """
    mark = (f" [{confidence}]"
            if confidence and confidence not in ("EXTRACTED", "") else "")
    via = f" via {relation}{mark}" if relation else ""
    header = (f"=== {role} (depth {depth}{via}) "
              f"{sym.file}:{sym.start}-{sym.end}  {sym.name} ===")
    lines = [header, sym.signature or sym.source.splitlines()[0]]
    defs = symbols.definitions_in(root, sym.file) or []
    prefix_matches = [d for d in defs
                      if d.start >= sym.start and d.end <= sym.end
                      and d.name != sym.name
                      and d.name.count(".") == sym.name.count(".") + 1]
    if prefix_matches:
        lines.append(f"    # {len(prefix_matches)} member(s); bodies are separate symbols:")
        for d in prefix_matches[:40]:
            lines.append(f"    L{d.def_line}  {d.signature[:100]}")
        if len(prefix_matches) > 40:
            lines.append(f"    ... and {len(prefix_matches) - 40} more")
    return "\n".join(lines) + "\n"


def build_context(
    data: dict,
    seed: str,
    root: Path,
    *,
    depth: int = 2,
    direction: str = "both",
    relations: tuple[str, ...] | None = None,
    budget: int = 6000,
    per_symbol_cap: int = 80,
    max_nodes: int = 200,
    decay: float = DEFAULT_DECAY,
    order: str = "mention-first",
    manifest: dict | None = None,
    index_budget: int = 0,
    index_extra_depth: int = 1,
    index_dynamic: bool = True,
) -> dict:
    """Seed symbol + neighbourhood, as source, within ``budget`` tokens.

    Two tiers share ``budget``. **Bodies**: full source for the best-ranked
    symbols within ``depth`` hops, until ``budget - index_budget`` is spent.
    **Index**: one line each (``file:line  signature``) for everything else the
    walk reached -- body-tier symbols that did not fit, plus symbols
    ``index_extra_depth`` hops further out -- until ``index_budget`` is spent.
    Measured motivation (70-task corpus, depth 2 / 6k): of 175 ground-truth
    symbols, 37 were reachable one hop beyond the walk and 17 were reached but
    dropped for budget; nothing was unreachable. An index line costs ~25 tokens
    against ~300 for a body, so the tail of the ranking is far cheaper to
    *name* than to *show*, and naming it is enough for an agent to open it.
    ``index_budget=0`` disables the tier and reproduces the single-tier pack.

    With ``index_dynamic`` (the default) ``index_budget`` is a RESERVE, not a
    fixed share: bodies may spend up to ``budget - index_budget``, and the index
    then gets everything bodies left unspent -- measured mean body usage is
    ~4.4k of 6k, so a fixed 1,200 split wasted ~400 tokens per pack while
    still costing one task its bodies. The sweep behind this is in
    ``plans/04-correctness-roadmap.md``.

    ``manifest`` is graphify's own ``manifest.json`` (``{path: {ast_hash,...}}``,
    MD5 of file content at extraction time). When given, every file the pack
    slices is re-hashed and any mismatch is reported in ``stale_files``: the
    line numbers the graph holds for that file were true of a different
    version of it. Omit it and the check is skipped, not faked -- ``stale_check``
    says which happened.
    """
    rel = relations or _br.DEFAULT_RELATIONS
    walk_depth = depth + (index_extra_depth if index_budget > 0 else 0)
    radius = _br.blast_radius(data, seed, depth=walk_depth, relations=rel,
                              direction=direction, max_nodes=max_nodes)

    by_id = {str(n["id"]): n for n in graphio.nodes(data)}

    # Which relation first reached each node, for ordering and for telling the
    # agent *why* a symbol is in the pack.
    via: dict[str, str] = {}
    via_conf: dict[str, str] = {}
    for e in radius["edges"]:
        s, t = str(e.get("source")), str(e.get("target"))
        r = str(e.get("relation", ""))
        # Carry the edge's confidence label alongside its relation. A `tests`
        # edge may be coverage-measured (EXTRACTED) or a bare name match
        # (INFERRED); an agent told only "via tests" cannot tell a fact from a
        # guess, which is the wrong way round for deciding a fix is covered.
        conf = str(e.get("confidence") or "")
        for endpoint in (t, s):
            if endpoint not in via:
                via[endpoint] = r
                if conf:
                    via_conf[endpoint] = conf

    # Degree over the WHOLE graph, for the tie-break: among equally scoring
    # candidates prefer the specific symbol over the hub, since a hub is both
    # less informative and more likely to be reachable by another route.
    degree: dict[str, int] = {}
    for e in graphio.edges(data):
        for endpoint in (str(e.get("source")), str(e.get("target"))):
            degree[endpoint] = degree.get(endpoint, 0) + 1

    def _score(n: dict) -> float:
        return score_node(via.get(str(n["id"])), int(n.get("blast_depth", 0)), decay)

    scores = {str(n["id"]): _score(n) for n in radius["nodes"]}

    # Depth stays the primary key. A weighted score (relation_weight x
    # decay**depth) was tried as the primary key and MEASURED WORSE: identical
    # recall at depths 1-3 and any budget, and -0.071 at depth 4 / budget 12k
    # (0.494 vs 0.565). Sweeping decay 0.5 -> 0.95 never recovered it, which
    # rules out the depth penalty as the cause and leaves the relation weights
    # themselves: ranking `contains` below `calls` loses on a corpus whose
    # ground truth is reached THROUGH containment chains. See
    # bench/agentctx/decay-sweep.log and plans/04-correctness-roadmap.md.
    #
    # What survives from that attempt, because neither depends on the scoring
    # hypothesis: the score orders symbols WITHIN a depth (strictly better than
    # the alphabetical tie-break it replaces), and the tie-break below is
    # deterministic and disclosed rather than arbitrary.
    # `order="legacy"` reproduces the pre-2026-09-03 key exactly, so any
    # ordering change can be A/B'd against it under matched conditions instead
    # of against a reconstructed baseline. Reconstructing one is how three
    # separate config mismatches got stacked into a single bogus comparison.
    if order == "legacy":
        _LEGACY_RANK = {"calls": 0, "indirect_call": 0, "method": 1, "contains": 1,
                        "extends": 2, "inherits": 2, "implements": 2,
                        "mixes_in": 2, "embeds": 2, "references": 3, "uses": 3,
                        "imports": 4, "imports_from": 4, "dynamic_import": 4,
                        "re_exports": 4, "requires": 4}
        ranked = sorted(
            radius["nodes"],
            key=lambda n: (
                0 if str(n["id"]) == seed else 1,
                int(n.get("blast_depth", 0)),
                _LEGACY_RANK.get(via.get(str(n["id"]), ""), 5),
                str(n.get("label", "")),
            ),
        )
    elif order in ("mention", "mention-first"):
        # A candidate whose bare name occurs as an identifier in the SEED'S OWN
        # SOURCE is a symbol the seed visibly touches, even where the extractor
        # emitted no edge for it (dynamic dispatch, attribute access, string
        # references). "mention" applies that within a depth; "mention-first"
        # lets it beat depth and is the DEFAULT: on the 70-task corpus at
        # d2/6k/reserve-300 it moved bodies recall 0.629 -> 0.636 and named
        # recall 0.723 -> 0.749, one task improved, none regressed, tokens
        # unchanged (bench/agentctx/baseline-dyn300-mention-first.json vs
        # baseline-supplement-index-dyn300.json). order="current" reproduces
        # the previous default for A/B.
        seed_node = by_id.get(seed, {})
        got_seed = symbols.resolve_node_detail(root, seed_node) if seed_node else None
        seed_idents = (set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", got_seed.source))
                       if isinstance(got_seed, symbols.Symbol) else set())

        def _mentioned(n: dict) -> int:
            leaf = str(n.get("label", "")).strip().lstrip(".").rstrip("()")
            return 0 if leaf and leaf in seed_idents else 1

        if order == "mention-first":
            key = lambda n: (0 if str(n["id"]) == seed else 1, _mentioned(n),
                             int(n.get("blast_depth", 0)), -scores[str(n["id"])],
                             0 if str(n.get("label", "")).startswith(".") else 1,
                             str(n["id"]))
        else:
            key = lambda n: (0 if str(n["id"]) == seed else 1,
                             int(n.get("blast_depth", 0)), _mentioned(n),
                             -scores[str(n["id"])],
                             0 if str(n.get("label", "")).startswith(".") else 1,
                             str(n["id"]))
        ranked = sorted(radius["nodes"], key=key)
    else:
        ranked = sorted(
            radius["nodes"],
            key=lambda n: (
                0 if str(n["id"]) == seed else 1,  # the seed is never displaced
                int(n.get("blast_depth", 0)),      # depth first — measured best
                -scores[str(n["id"])],             # relation weight within a depth
                # Members before non-members. This is the ONLY part of the sort
                # that moved recall: degree-ascending scored 0.494 and
                # degree-descending 0.530 against 0.565 here, at d3/b12k.
                # The original alphabetical tie-break hit 0.565 by accident —
                # graphify labels methods ".name()", and "." sorts before every
                # letter, so sorting by label WAS a members-first rule. Making
                # it explicit keeps the measured behaviour and states the reason.
                0 if str(n.get("label", "")).startswith(".") else 1,
                str(n["id"]),                      # then stable, never arbitrary
            ),
        )



    chunks: list[str] = []
    included: list[dict] = []
    unresolved: list[dict] = []
    omitted: list[dict] = []
    pending_omitted: list[dict] = []
    index_candidates: list[tuple[dict, symbols.Symbol, dict, str]] = []
    index_unsliceable = 0
    used = 0
    body_budget = max(0, budget - index_budget)

    for n in ranked:
        nid = str(n["id"])
        node = by_id.get(nid, n)
        node_depth = int(n.get("blast_depth", 0))
        # A containment walk reaches the FILE node, which has no body to slice.
        # It is not a failure, so it does not get a failure's reason code.
        loc = str(node.get("source_location") or "")
        if graphio.is_file_node(node):
            if node_depth > depth and nid != seed:
                index_unsliceable += 1
                continue
            unresolved.append({
                "id": nid, "label": node.get("label"),
                "file": node.get("source_file"), "location": loc,
                "reason_code": "file_node",
                "reason": "this is the file itself, not a definition; its "
                          "members are reached via contains edges",
                "is_seed": nid == seed,
            })
            continue
        got = symbols.resolve_node_detail(root, node)
        if isinstance(got, symbols.Unresolved):
            if node_depth > depth and nid != seed:
                # Reached only by the index tier's extra hop. It would never
                # have been shown as a body, so it is not a failure of this
                # pack; listing every rationale/module node three hops out as
                # "unresolved" tripled that list and buried the real ones.
                index_unsliceable += 1
                continue
            # The reason is the one symbols.py actually hit, never inferred
            # here: an unreadable file and a docstring node are different
            # failures, and telling the agent the wrong one is a guess wearing
            # a refusal's clothing.
            unresolved.append({
                "id": nid,
                "label": node.get("label"),
                "file": node.get("source_file"),
                "location": node.get("source_location"),
                "reason_code": got.code,
                "reason": got.detail,
                "is_seed": nid == seed,
            })
            continue
        sym = got

        entry_meta = {
            "id": nid, "label": node.get("label"),
            "file": sym.file, "lines": [sym.start, sym.end],
            "def_line": sym.def_line, "signature": sym.signature,
            "depth": node_depth, "via": via.get(nid),
            "score": round(scores.get(nid, 0.0), 5),
        }
        # Beyond the body depth: index tier only, never a body.
        if node_depth > depth and nid != seed:
            index_candidates.append((entry_meta, sym, node, "depth"))
            continue

        role = "SEED" if nid == seed else "RELATED"
        if sym.kind == "class" and nid != seed:
            text = _render_class_summary(sym, role, node_depth, via.get(nid),
                                         root, via_conf.get(nid))
            truncated = False
        else:
            text, truncated = _render(sym, role, node_depth, via.get(nid),
                                      per_symbol_cap, via_conf.get(nid))
        cost = _count(text)
        if used + cost > body_budget and nid != seed:
            index_candidates.append((entry_meta, sym, node, "budget"))
            continue
        chunks.append(text)
        used += cost
        included.append({
            "id": nid, "name": sym.name, "kind": sym.kind, "file": sym.file,
            # `lines` spans decorators; `def_line` is the definition keyword's
            # own line — the one graphify stores, and the only stable join key
            # back to graph nodes or to a tree-sitter symbol table.
            "lines": [sym.start, sym.end], "def_line": sym.def_line,
            "depth": node_depth,
            "via": via.get(nid), "via_confidence": via_conf.get(nid),
            "score": round(scores.get(nid, 0.0), 5),
            "signature": sym.signature,
            "truncated": truncated,
            # Provenance of the NODE, distinct from the edge's confidence: an
            # extractor node and one materialised by `supplement` from source
            # are equally real, but an agent should be able to tell which
            # layer it is trusting.
            "origin": node.get("origin") or node.get("_origin") or "ast",
            "qualified_name": node.get("qualified_name"),
        })

    # ---- index tier ----------------------------------------------------
    # Ranked order is preserved: body-tier overflow first (it outranked the
    # deeper nodes), then the extra hop. Each line is name + location +
    # signature -- enough to open the symbol, cheap enough to list dozens.
    index: list[dict] = []
    index_lines: list[str] = []
    index_used = 0
    index_cap = (max(index_budget, budget - used) if (index_dynamic and index_budget > 0)
                 else index_budget)
    for meta, sym, node, why in index_candidates:
        line = f"  {sym.file}:L{sym.def_line}  {sym.signature[:120]}"
        cost = _count(line + "\n")
        if index_budget <= 0 or index_used + cost > index_cap:
            pending_omitted.append({**meta, "reason": why if why == "budget"
                                    else "index_budget"})
            continue
        index_used += cost
        index_lines.append(line)
        index.append({**meta, "kind": sym.kind, "tier_reason": why,
                      "origin": node.get("origin") or node.get("_origin") or "ast",
                      "qualified_name": node.get("qualified_name")})
    if index_lines:
        head = (f"--- index: {len(index_lines)} more symbol(s) reached "
                f"(depth <= {walk_depth}); open by file:line ---")
        chunks.append(head + "\n" + "\n".join(index_lines) + "\n")
        used += _count(head + "\n") + index_used

    # An empty pack has two very different causes — the seed resolved but has no
    # neighbours, or the seed itself could not be sliced. Collapsing them into
    # "0 symbols" would hand the agent the same missing-vs-absent ambiguity this
    # module exists to remove, so say which it was.
    # ---- disclose what the GRAPH does not contain -----------------------
    # The gaps above (`unresolved`, `omitted`) are things the graph knows about
    # and this pack chose not to emit. This is the other kind: symbols that are
    # really there in the source and have no node at all, so nothing downstream
    # could ever mention them. Measured on 14 checkouts of psf/requests: 610 of
    # 610 functions nested inside another function have no node (a graphify
    # design choice), and every remaining non-dunder miss was an id collision.
    # An agent told "here is the context" while a closure inside the very
    # function it is editing is invisible has been misled by omission.
    graph_syms: dict[str, set[tuple[int, str]]] = {}
    for n in graphio.nodes(data):
        loc = str(n.get("source_location") or "")
        if loc.startswith("L") and loc[1:].isdigit() and n.get("source_file"):
            leaf = str(n.get("label") or "").strip().lstrip(".").rstrip("()")
            graph_syms.setdefault(str(n["source_file"]), set()).add(
                (int(loc[1:]), leaf))

    unmodelled: list[dict] = []
    shown: dict[str, list[tuple[int, int]]] = {}
    for i in included:
        shown.setdefault(str(i["file"]), []).append((i["lines"][0], i["lines"][1]))
    for f, ranges in shown.items():
        defs = symbols.definitions_in(root, f)
        if defs is None:
            continue
        known = graph_syms.get(f, set())
        known_lines = {ln for ln, _ in known}
        kinds = {s.name: s.kind for s in defs}
        extents = {s.name: (s.start, s.end) for s in defs}
        for sym in defs:
            leaf = sym.name.split(".")[-1]
            # Suppress on (line AND name), not line alone. graphify emits
            # doc/rationale nodes that share a line with real code, so a
            # line-only check let an unrelated node hide a genuine gap.
            if (sym.def_line, leaf) in known:
                continue
            if sym.def_line in known_lines and leaf in {
                    lf for ln, lf in known if ln == sym.def_line}:
                continue
            inside = any(lo <= sym.def_line <= hi for lo, hi in ranges)
            nested = symbols.is_nested_in_function(sym.name, kinds,
                                                   sym.def_line, extents)
            unmodelled.append({
                "name": sym.name, "kind": sym.kind, "file": sym.file,
                "lines": [sym.start, sym.end], "def_line": sym.def_line,
                "signature": sym.signature,
                # Whether the gap sits in code the agent was shown, or elsewhere
                # in a file it was shown. Restricting to the former dropped the
                # case this feature was built for: a sibling top-level
                # `res.json = function(){}` next to an included `res.send`.
                "within_shown_code": inside,
                "reason": ("nested inside another function — graphify emits no "
                           "node for these" if nested
                           else "present in source but absent from the graph"),
            })

    # ---- tests that touch this context --------------------------------
    # An autonomous fix ends with "run the tests that exercise this". The
    # graph already knows which test files import/call/reference the symbols
    # shown (`tests` edges when coverage or the heuristic was injected, and the
    # extractor's own edges from files under tests/ otherwise). Collect them
    # per shown symbol, with the relation and its confidence, so the agent can
    # tell a coverage-measured link from an import.
    from . import test_link as _tl
    shown_ids = {i["id"] for i in included} | {i["id"] for i in index}
    related_tests: list[dict] = []
    seen_tests: set[tuple[str, str, str]] = set()
    _NOT_A_TEST_LINK = {"contains", "method", "rationale_for", "embeds"}
    for e in graphio.edges(data):
        s_id, t_id = str(e.get("source")), str(e.get("target"))
        rel_name = str(e.get("relation", ""))
        if rel_name in _NOT_A_TEST_LINK:
            continue          # structure/doc edges say nothing about exercising code
        for test_end, code_end in ((s_id, t_id), (t_id, s_id)):
            # A test that the walk itself pulled in (it calls the seed) is
            # still a test to run afterwards, so being shown does not exclude
            # it -- only being the very symbol it touches does.
            if code_end not in shown_ids or test_end == code_end:
                continue
            tn = by_id.get(test_end)
            if tn is None or not _tl.is_test_path(str(tn.get("source_file") or "")):
                continue
            key = (test_end, code_end, rel_name)
            if key in seen_tests:
                continue
            seen_tests.add(key)
            related_tests.append({
                "test_id": test_end, "test_label": tn.get("label"),
                "test_file": tn.get("source_file"),
                "test_location": tn.get("source_location"),
                "relation": rel_name,
                "confidence": e.get("confidence") or ("EXTRACTED" if rel_name != "tests" else None),
                "detail": e.get("detail"),
                "touches": code_end,
                "touches_label": by_id.get(code_end, {}).get("label"),
            })
    related_tests.sort(key=lambda r: (0 if r["touches"] == seed else 1,
                                      0 if r["relation"] == "tests" else 1,
                                      str(r["test_file"]), str(r["test_label"])))

    # ---- review checklist: what a fix to the seed must not leave behind ------
    # Measured in the end-to-end fix eval (bench/fixeval): on the task the
    # graph arm lost, the pack SHOWED the call site (`Session.send` line 197
    # was in the RELATED bodies) and the agent still declared itself done
    # without touching it; on another it fixed the named member and not its two
    # sibling decorators. Showing is not enough. This restates, as a list the
    # agent can tick, every call site of the seed and every sibling member of
    # its owner -- no new retrieval, just the two things "stopped one symbol
    # short" means.
    _CALL_RELS = ("calls", "indirect_call")
    _MEMBER_RELS = ("method", "contains")
    call_sites: list[dict] = []
    owners: list[str] = []
    for e in graphio.edges(data):
        rel_name = str(e.get("relation", ""))
        s_id, t_id = str(e.get("source")), str(e.get("target"))
        if t_id == seed and rel_name in _CALL_RELS and s_id in by_id:
            cn = by_id[s_id]
            call_sites.append({
                "id": s_id, "label": cn.get("label"), "file": cn.get("source_file"),
                "location": cn.get("source_location"),
                # the line the extractor saw the call on, when it recorded one
                "call_line": e.get("source_location"),
                "relation": rel_name, "confidence": e.get("confidence"),
                "shown": s_id in {i["id"] for i in included},
            })
        elif t_id == seed and rel_name in _MEMBER_RELS and s_id in by_id:
            owners.append(s_id)
    siblings: list[dict] = []
    for e in graphio.edges(data):
        if (str(e.get("source")) in owners and str(e.get("relation", "")) in _MEMBER_RELS
                and str(e.get("target")) != seed and str(e.get("target")) in by_id):
            sn = by_id[str(e.get("target"))]
            if not graphio.is_file_node(sn) and (sn.get("_callable") or str(sn.get("label", "")).endswith("()")):
                siblings.append({"id": str(e.get("target")), "label": sn.get("label"),
                                 "file": sn.get("source_file"), "location": sn.get("source_location"),
                                 "owner": by_id.get(str(e.get("source")), {}).get("label"),
                                 "shown": str(e.get("target")) in {i["id"] for i in included}})
    seen_ids: set[str] = set()
    call_sites = [c for c in call_sites if not (c["id"] in seen_ids or seen_ids.add(c["id"]))]
    seen_ids = set()
    siblings = [c for c in siblings if not (c["id"] in seen_ids or seen_ids.add(c["id"]))]
    review_checklist = {
        "call_sites_of_seed": call_sites,
        "sibling_members": siblings,
        "note": ("Before finishing a change to the seed: visit every call site listed "
                 "(update it or confirm it still holds), and decide for each sibling "
                 "member whether the same change applies. Items marked shown=false are "
                 "in the graph but not in this pack; open them."),
    }

    # ---- disclose files the graph is STALE for ---------------------------
    # graphify's manifest records an MD5 of each file at extraction. A file
    # whose current hash differs has been edited since: every line number the
    # graph holds for it is suspect, and a slice keyed on one may already have
    # been refused above as `definition_mismatch`. Report the file itself, so
    # the agent knows to re-extract rather than to distrust one symbol at a
    # time. Files the pack did not touch are not checked -- this is a
    # statement about what was shown, not about the repository.
    stale_files: list[dict] = []
    touched = set(shown) | {str(u["file"]) for u in unresolved if u.get("file")}
    if manifest is not None:
        import hashlib
        for f in sorted(touched):
            entry = manifest.get(f) if isinstance(manifest, dict) else None
            if not isinstance(entry, dict):
                stale_files.append({"file": f, "reason": "not in manifest",
                                    "manifest_hash": None, "current_hash": None})
                continue
            recorded = str(entry.get("ast_hash") or entry.get("hash") or "")
            try:
                h = hashlib.md5(usedforsecurity=False)
                h.update((Path(root) / f).read_bytes())
                current = h.hexdigest()
            except OSError:
                stale_files.append({"file": f, "reason": "file unreadable",
                                    "manifest_hash": recorded or None,
                                    "current_hash": None})
                continue
            if recorded and recorded != current:
                stale_files.append({"file": f, "reason": "edited since extraction",
                                    "manifest_hash": recorded,
                                    "current_hash": current})

    # Severity is only knowable once everything that fitted is known, so it is
    # assigned here rather than at drop time.
    kept = [i["score"] for i in included if i["id"] != seed]
    floor = min(kept) if kept else None
    for o in pending_omitted:
        # Nothing but the seed survived: the budget is too small to rank
        # anything, so every drop is potentially the one that mattered.
        o["severity"] = ("truncated_high_rank"
                         if floor is None or o["score"] >= floor
                         else "truncated_low_rank")
    omitted.extend(pending_omitted)

    seed_failure = next((u for u in unresolved if u["is_seed"]), None)

    return {
        "seed": seed,
        "seed_resolved": seed_failure is None,
        "seed_unresolved_reason": seed_failure["reason_code"] if seed_failure else None,
        "depth": depth,
        "index_depth": walk_depth,
        "index_budget": index_budget,
        "index_cap": index_cap,
        "direction": direction,
        "budget": budget,
        "decay": decay,
        "ranking": ("mentioned-in-seed first, then depth, then relation_weight * "
                    "decay**depth" if order == "mention-first" else
                    "depth, then relation_weight * decay**depth within depth"),
        "high_rank_floor": floor,
        "tokens_used": used,
        "token_method": _token_method(),
        "included": included,
        "index": index,
        "index_unsliceable": index_unsliceable,
        "related_tests": related_tests,
        "review_checklist": review_checklist,
        "unmodelled": unmodelled,
        "unresolved": unresolved,
        "omitted": omitted,
        "stale_check": "manifest" if manifest is not None else "skipped (no manifest given)",
        "stale_files": stale_files,
        "text": "\n".join(chunks),
    }
