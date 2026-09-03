"""Blast-radius BFS + override discovery (Requirement 2, cases 2 & 3).

``blast_radius`` returns a SCOPED SUBGRAPH (nodes + edges + meta), not hit
lines and not the full graph — the subgraph is what gets fed into a coding
agent's context, so it must stay token-bounded (``max_nodes`` cap, explicit
``truncated`` flag).

Directionality: for appsec impact the default is "up" (reverse traversal —
who reaches/depends on the vulnerable node), matching upstream ``affected``.
"down" walks callees/dependencies; "both" unions the two.
"""
from __future__ import annotations

import json
from collections import deque

from graphify_ext import graphio

# Superset of upstream affected.DEFAULT_AFFECTED_RELATIONS (kept in sync by a
# test against the vendored source): structural relations that transmit the
# impact of a behavior/signature change.
DEFAULT_RELATIONS = (
    "calls",
    "indirect_call",
    "references",
    "imports",
    "imports_from",
    "dynamic_import",
    "re_exports",
    "inherits",
    "extends",
    "implements",
    "uses",
    "mixes_in",
    "embeds",
    "requires",
)

# Relations injected by edge_inject that mark taint reachability (case 4).
TAINT_RELATIONS = ("taints", "reaches_sink")

# Containment / membership. Public because `blast-radius --include-containment`
# exposes them, but deliberately NOT part of DEFAULT_RELATIONS: `contains` is
# typically the single most common relation in a graph (measured: 4,256 of
# 6,944 edges, 61%, on the connected repo), so following it by default floods
# every radius. Upstream CRG reaches the same conclusion for the same reason —
# its IMPACT_EDGE_DIRECTIONS marks CONTAINS as IMPACT_DIRECTION_NONE, noting a
# changed file already seeds every node in it.
MEMBER_RELATIONS = ("method", "contains")
_MEMBER_RELATIONS = MEMBER_RELATIONS  # back-compat alias


def _member_seeds(data: dict, seed: str) -> list[str]:
    """Upstream #1669 parity: when the seed is a class, its method nodes are
    where callers actually bind — seed the walk with one outward
    method/contains hop (seeds only, never reported as hits)."""
    out = []
    for e in graphio.edges(data):
        if str(e.get("source")) == seed and str(e.get("relation", "")) in _MEMBER_RELATIONS:
            out.append(str(e.get("target")))
    return out


def blast_radius(
    data: dict,
    seed: str,
    *,
    depth: int = 2,
    relations: tuple[str, ...] = DEFAULT_RELATIONS,
    direction: str = "up",
    max_nodes: int = 500,
) -> dict:
    """BFS from ``seed`` over relation-filtered edges, capped at ``depth`` hops.

    Returns ``{"seed", "depth", "direction", "truncated", "nodes": [...],
    "edges": [...]}`` where nodes carry their depth and edges are the traversed
    subset (plus all edges BETWEEN included nodes, so the subgraph is closed
    and self-describing for the agent).
    """
    if direction not in ("up", "down", "both"):
        raise ValueError(f"direction must be up|down|both, got {direction!r}")
    relation_set = set(relations)
    idx = graphio.node_index(data)
    if seed not in idx:
        raise KeyError(f"seed node {seed!r} not in graph")

    # Adjacency: reverse (target -> sources) and forward (source -> targets).
    rev: dict[str, list[dict]] = {}
    fwd: dict[str, list[dict]] = {}
    for e in graphio.edges(data):
        if str(e.get("relation", "")) not in relation_set:
            continue
        s, t = str(e.get("source")), str(e.get("target"))
        rev.setdefault(t, []).append(e)
        fwd.setdefault(s, []).append(e)

    depths: dict[str, int] = {seed: 0}
    kept_edges: list[dict] = []
    truncated = False

    queue: deque[tuple[str, int]] = deque([(seed, 0)])
    for member in _member_seeds(data, seed):
        if member in idx and member not in depths:
            depths[member] = 0
            queue.append((member, 0))

    while queue:
        current, d = queue.popleft()
        if d >= depth:
            continue
        neighbors: list[tuple[str, dict]] = []
        if direction in ("up", "both"):
            neighbors += [(str(e.get("source")), e) for e in rev.get(current, [])]
        if direction in ("down", "both"):
            neighbors += [(str(e.get("target")), e) for e in fwd.get(current, [])]
        for other, e in neighbors:
            if other not in idx:
                continue
            kept_edges.append(e)
            if other in depths:
                continue
            if len(depths) >= max_nodes:
                truncated = True
                continue
            depths[other] = d + 1
            queue.append((other, d + 1))

    # Close the subgraph: include every edge between included nodes, of ANY
    # relation — deliberately ignoring `relations`.
    #
    # This is a policy decision, not an oversight. `relations` controls which
    # edges the walk FOLLOWS (which nodes are reachable); closure controls what
    # is REPORTED about the nodes already selected. Filtering closure too would
    # hide exactly the edges the caller most needs: injected `taints` /
    # `reaches_sink` / `tests` / `reads_config` edges are never in the
    # structural traversal set, so a narrowed walk would silently drop the
    # security context it was narrowed to find.
    included = set(depths)
    edge_set: list[dict] = []
    seen_edge_ids = set()
    for e in graphio.edges(data):
        s, t = str(e.get("source")), str(e.get("target"))
        if s in included and t in included:
            key = (s, t, str(e.get("relation", "")))
            if key in seen_edge_ids:
                continue
            seen_edge_ids.add(key)
            edge_set.append(e)

    sub_nodes = []
    for nid, d in sorted(depths.items(), key=lambda kv: (kv[1], kv[0])):
        n = dict(idx[nid])
        n["blast_depth"] = d
        # Strip clustering noise from agent-facing output.
        n.pop("community", None)
        sub_nodes.append(n)

    result = {
        "seed": seed,
        "depth": depth,
        "direction": direction,
        "relations": list(relations),
        "truncated": truncated,
        "node_count": len(sub_nodes),
        "edge_count": len(edge_set),
        "nodes": sub_nodes,
        "edges": edge_set,
    }
    # Rough token cost of handing this subgraph to an agent (~4 chars/token).
    # `truncated` only reports the max_nodes cap, which on a real repo does not
    # fire: measured on a hot node, `--direction both --depth 2` returned 50
    # nodes — well under the 500 cap — but ~14.7k tokens, while the same walk
    # narrowed to `calls` cost ~4.2k. A node count is a poor proxy for context
    # budget, so report the budget directly rather than implying it is bounded.
    result["estimated_tokens"] = len(json.dumps(result, default=str)) // 4
    return result


def taint_exposed(radius: dict) -> dict:
    """Case 4 filter: the appsec-relevant subset of a blast radius — nodes
    touched by injected taint edges, plus those edges."""
    taint_edges = [e for e in radius["edges"]
                   if str(e.get("relation", "")) in TAINT_RELATIONS]
    touched = {str(e.get("source")) for e in taint_edges} | \
              {str(e.get("target")) for e in taint_edges}
    return {
        "nodes": [n for n in radius["nodes"] if str(n.get("id")) in touched],
        "edges": taint_edges,
    }


_INHERIT_RELATIONS = ("inherits", "extends", "implements", "mixes_in")


def overrides_of(data: dict, seed: str) -> list[dict]:
    """Case 3: overriding/implementing definitions of a (possibly inherited)
    method — a fix in the base method does not propagate to overrides.

    Walk: seed method -> owning class (reverse method/contains edge) ->
    subclasses (reverse inherits/extends/implements, transitive) -> the
    subclass's own member node with the same bare name, if any.
    """
    idx = graphio.node_index(data)
    if seed not in idx:
        raise KeyError(f"seed node {seed!r} not in graph")
    seed_bare = graphio._bare(str(idx[seed].get("label", "")))

    owners = [str(e.get("source")) for e in graphio.edges(data)
              if str(e.get("target")) == seed
              and str(e.get("relation", "")) in _MEMBER_RELATIONS]
    if not owners:
        # Seed may itself be a class: check overrides of ALL its methods? No —
        # without a method name there is nothing to match. Return empty.
        return []

    # Transitive subclasses of any owner.
    children: dict[str, list[str]] = {}
    for e in graphio.edges(data):
        if str(e.get("relation", "")) in _INHERIT_RELATIONS:
            children.setdefault(str(e.get("target")), []).append(str(e.get("source")))
    subclasses: set[str] = set()
    stack = list(owners)
    while stack:
        cls = stack.pop()
        for sub in children.get(cls, []):
            if sub not in subclasses:
                subclasses.add(sub)
                stack.append(sub)

    members: dict[str, list[str]] = {}
    for e in graphio.edges(data):
        if str(e.get("relation", "")) in _MEMBER_RELATIONS:
            members.setdefault(str(e.get("source")), []).append(str(e.get("target")))

    out: list[dict] = []
    for cls in sorted(subclasses):
        for member in members.get(cls, []):
            m = idx.get(member)
            if m is None or member == seed:
                continue
            if graphio._bare(str(m.get("label", ""))) == seed_bare:
                entry = dict(m)
                entry["override_of"] = seed
                entry["owning_class"] = cls
                entry.pop("community", None)
                out.append(entry)
    return out
