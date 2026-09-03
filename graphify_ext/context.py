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
    order: str = "current",
) -> dict:
    """Seed symbol + neighbourhood, as source, within ``budget`` tokens."""
    rel = relations or _br.DEFAULT_RELATIONS
    radius = _br.blast_radius(data, seed, depth=depth, relations=rel,
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
    used = 0

    for n in ranked:
        nid = str(n["id"])
        node = by_id.get(nid, n)
        got = symbols.resolve_node_detail(root, node)
        if isinstance(got, symbols.Unresolved):
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

        role = "SEED" if nid == seed else "RELATED"
        text, truncated = _render(sym, role, int(n.get("blast_depth", 0)),
                                  via.get(nid), per_symbol_cap,
                                  via_conf.get(nid))
        cost = _count(text)
        if used + cost > budget and nid != seed:
            sc = scores.get(nid, 0.0)
            pending_omitted.append({
                "id": nid, "label": node.get("label"),
                "file": sym.file, "lines": [sym.start, sym.end],
                "reason": "budget",
                "score": round(sc, 5),
            })
            continue
        chunks.append(text)
        used += cost
        included.append({
            "id": nid, "name": sym.name, "kind": sym.kind, "file": sym.file,
            # `lines` spans decorators; `def_line` is the definition keyword's
            # own line — the one graphify stores, and the only stable join key
            # back to graph nodes or to a tree-sitter symbol table.
            "lines": [sym.start, sym.end], "def_line": sym.def_line,
            "depth": int(n.get("blast_depth", 0)),
            "via": via.get(nid), "via_confidence": via_conf.get(nid),
            "score": round(scores.get(nid, 0.0), 5),
            "signature": sym.signature,
            "truncated": truncated,
        })

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
    graph_lines: dict[str, set[int]] = {}
    for n in graphio.nodes(data):
        loc = str(n.get("source_location") or "")
        if loc.startswith("L") and loc[1:].isdigit() and n.get("source_file"):
            graph_lines.setdefault(str(n["source_file"]), set()).add(int(loc[1:]))

    unmodelled: list[dict] = []
    spans: dict[str, list[tuple[int, int]]] = {}
    for i in included:
        spans.setdefault(str(i["file"]), []).append((i["lines"][0], i["lines"][1]))
    for f, ranges in spans.items():
        defs = symbols.definitions_in(root, f)
        if defs is None:
            continue
        known = graph_lines.get(f, set())
        for sym in defs:
            if sym.def_line in known:
                continue
            if not any(lo <= sym.def_line <= hi for lo, hi in ranges):
                continue                      # outside what the agent was shown
            unmodelled.append({
                "name": sym.name, "kind": sym.kind, "file": sym.file,
                "lines": [sym.start, sym.end], "def_line": sym.def_line,
                "signature": sym.signature,
                "reason": ("nested inside another definition — graphify emits no "
                           "node for these" if sym.name.count(".") >= 1
                           else "present in source but absent from the graph"),
            })

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
        "direction": direction,
        "budget": budget,
        "decay": decay,
        "ranking": "depth, then relation_weight * decay**depth within depth",
        "high_rank_floor": floor,
        "tokens_used": used,
        "token_method": _token_method(),
        "included": included,
        "unmodelled": unmodelled,
        "unresolved": unresolved,
        "omitted": omitted,
        "text": "\n".join(chunks),
    }
