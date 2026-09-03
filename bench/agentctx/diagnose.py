"""Why did a task score what it scored? Per ground-truth symbol, one verdict.

For every symbol the agent had to discover, classify the failure (or success)
mechanically, so "recall 0.0" decomposes into causes that point at different
fixes:

  included            in the pack
  omitted_for_budget  reached by the walk, ranked, dropped for budget
  unresolved          reached, but no slice could be emitted (reason code)
  reachable_deeper    a path exists in the graph at depth > cfg depth
  unreachable         a node exists but no path of any depth over the config's relations
  unreachable_any     a node exists but no path over ANY relation (graph disconnected)
  no_node             the graph has no node for it at all

    python diagnose.py [--config default] [--repo flask] [--only-zero]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evaluate import resolve_entry  # noqa: E402
from regress import CONFIGS, _graph_for, prepare, targets  # noqa: E402
from graphify_ext import context as ctxmod  # noqa: E402
from graphify_ext import graphio  # noqa: E402

HERE = Path(__file__).resolve().parent


def _adj(data, relations):
    adj: dict[str, set[str]] = {}
    for e in graphio.edges(data):
        if relations is not None and str(e.get("relation")) not in relations:
            continue
        s, t = str(e["source"]), str(e["target"])
        adj.setdefault(s, set()).add(t)
        adj.setdefault(t, set()).add(s)
    return adj


def _depth(adj, seed, target, limit=8):
    seen = {seed: 0}
    q = deque([seed])
    while q:
        cur = q.popleft()
        if cur == target:
            return seen[cur]
        if seen[cur] >= limit:
            continue
        for nxt in adj.get(cur, ()):
            if nxt not in seen:
                seen[nxt] = seen[cur] + 1
                q.append(nxt)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="default", choices=sorted(CONFIGS))
    ap.add_argument("--repo", default=None)
    ap.add_argument("--only-zero", action="store_true")
    ap.add_argument("--corpus", default=str(HERE / "corpus.json"))
    args = ap.parse_args()
    cfg = CONFIGS[args.config]
    corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
    verdicts = Counter()
    via_of_missed = Counter()
    rows = []
    for t in corpus:
        if args.repo and t.get("repo") != args.repo:
            continue
        key = f"{t.get('repo')}/{t['commit'][:8]}"
        wt, _ = prepare(t)
        data = _graph_for(wt, bool(cfg.get("supplement")))
        seed = resolve_entry(data, t)
        if seed is None:
            print(f"{key}: SEED MISSING")
            continue
        pack = ctxmod.build_context(data, seed, wt, depth=cfg["depth"], direction=cfg["direction"],
                                    budget=cfg["budget"], relations=cfg["relations"],
                                    max_nodes=cfg["max_nodes"], order=cfg["order"])
        inc = {(str(i["file"]), int(i["def_line"])): i for i in pack["included"]}
        om = {(str(o["file"]), int(o["lines"][0])): o for o in pack["omitted"]}
        # omitted entries carry construct start, not def_line; also index by node id
        om_ids = {o["id"] for o in pack["omitted"]}
        un_ids = {u["id"]: u for u in pack["unresolved"]}
        by_key = {}
        for n in graphio.nodes(data):
            loc = str(n.get("source_location") or "")
            if n.get("source_file") and loc.startswith("L") and loc[1:].isdigit():
                by_key.setdefault((str(n["source_file"]), int(loc[1:])), []).append(n)
        adj_cfg = _adj(data, set(cfg["relations"]))
        adj_all = _adj(data, None)
        tgt = targets(t)
        hit = sum(1 for k in tgt if k in inc)
        recall = hit / len(tgt) if tgt else None
        if args.only_zero and recall not in (0, 0.0):
            continue
        for k in sorted(tgt):
            if k in inc:
                v = "included"
                detail = f"via {inc[k]['via']} d{inc[k]['depth']}"
            else:
                nodes = [n for n in by_key.get(k, []) if n.get("_callable") or not str(n.get("label", "")).endswith(".py")]
                nodes = [n for n in nodes if n.get("file_type") != "rationale"] or by_key.get(k, [])
                if not nodes:
                    v, detail = "no_node", ""
                else:
                    nid = str(nodes[0]["id"])
                    if nid in om_ids:
                        o = next(o for o in pack["omitted"] if o["id"] == nid)
                        v, detail = "omitted_for_budget", f"score {o['score']} {o['severity']}"
                    elif nid in un_ids:
                        v, detail = "unresolved", un_ids[nid]["reason_code"]
                    else:
                        d = _depth(adj_cfg, seed, nid)
                        if d is not None:
                            v, detail = "reachable_deeper", f"depth {d}"
                        else:
                            d2 = _depth(adj_all, seed, nid)
                            v = "unreachable" if d2 is not None else "unreachable_any"
                            detail = f"any-relation depth {d2}" if d2 is not None else ""
            verdicts[v] += 1
            rows.append((key, k, v, detail))
            print(f"  {key:<20} {v:<20} {k[0]}:{k[1]:<5} {detail}")
    print("\n=== verdicts over ground-truth symbols ===")
    for v, n in verdicts.most_common():
        print(f"  {v:<20} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
