"""Re-run the benchmark and diff it against a stored baseline, per task.

Why this exists
---------------
Every wrong number in this project's history came from comparing against a
figure whose conditions were not re-derived: a budget that differed, a depth
that differed, a `max_nodes` that differed, or a baseline reconstructed from
memory that had never existed in the code. Three of those were stacked into a
single reported regression that had to be retracted twice.

So the baseline is a file, produced by this script, carrying its own
configuration. A comparison either runs against a baseline captured by the same
code path or it does not run.

Aggregates are printed last and per-task diffs first, deliberately: a mean that
moved 0.000 while four tasks moved +0.25 and four moved -0.25 is not "no
change", and the containment result showed a lift concentrated in 3 of 14 tasks
that read as uniform in the aggregate.

Usage
-----
    python regress.py --update          # capture a baseline
    python regress.py                   # compare; non-zero exit on regression
    python regress.py --config d3-12k   # a named config from CONFIGS
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evaluate import load_graph_json, prepare, resolve_entry  # noqa: E402
from graphify_ext import blast_radius as br  # noqa: E402
from graphify_ext import context as ctxmod  # noqa: E402
from graphify_ext import graphio, supplement  # noqa: E402

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus.json"

_CONTAINMENT = tuple(dict.fromkeys(br.DEFAULT_RELATIONS + br.MEMBER_RELATIONS))

#: Named configurations. Every field that changed a number in this project's
#: history is explicit here, because "same config" has to be checkable rather
#: than remembered.
CONFIGS = {
    "default": dict(depth=2, budget=6000, max_nodes=800, direction="both",
                    relations=_CONTAINMENT, order="current", supplement=False),
    "d3-12k": dict(depth=3, budget=12000, max_nodes=800, direction="both",
                   relations=_CONTAINMENT, order="current", supplement=False),
    "no-containment": dict(depth=2, budget=6000, max_nodes=800, direction="both",
                           relations=br.DEFAULT_RELATIONS, order="current",
                           supplement=False),
    "legacy-order": dict(depth=2, budget=6000, max_nodes=800, direction="both",
                         relations=_CONTAINMENT, order="legacy", supplement=False),
    # Same as `default` but with `graphify-ext supplement` applied to the task
    # graph first. The ONLY variable is the supplement, so a per-task diff
    # against `default` isolates what materialising missing definitions buys
    # (tasks whose entry symbol had no node become scoreable) and costs
    # (crowding on tasks that already scored).
    "supplement": dict(depth=2, budget=6000, max_nodes=800, direction="both",
                       relations=_CONTAINMENT, order="current", supplement=True),
    "supplement-d3-12k": dict(depth=3, budget=12000, max_nodes=800, direction="both",
                              relations=_CONTAINMENT, order="current", supplement=True),
    # Same TOTAL budget as `supplement` (6,000), but 1,200 of it carved out for
    # the index tier (one line per symbol: file:line + signature), which also
    # walks one hop further. Budget-matched by construction, so the per-task
    # diff against `supplement` isolates the tiering alone.
    "supplement-index": dict(depth=2, budget=6000, max_nodes=800, direction="both",
                             relations=_CONTAINMENT, order="current", supplement=True,
                             index_budget=1200),
}
# Budget-matched sweep of the index share (rule 8: a parameter is fitted, not
# asserted). Total stays 6,000; only the split moves.
for _ib in (600, 2000, 3000):
    CONFIGS[f"supplement-index-{_ib}"] = dict(
        depth=2, budget=6000, max_nodes=800, direction="both",
        relations=_CONTAINMENT, order="current", supplement=True, index_budget=_ib,
        index_dynamic=False)
CONFIGS["supplement-index"]["index_dynamic"] = False
# Dynamic variant: `index_budget` is only a reserve; the index also gets what
# the bodies left unspent. Same total budget.
# `dyn300-mention-first` IS the shipped default (context.build_context defaults:
# order="mention-first", index reserve 300 dynamic, containment on).
for _ord in ("mention", "mention-first"):
    CONFIGS[f"dyn300-{_ord}"] = dict(
        depth=2, budget=6000, max_nodes=800, direction="both",
        relations=_CONTAINMENT, order=_ord, supplement=True, index_budget=300,
        index_dynamic=True)
for _ib in (300, 600, 1200):
    CONFIGS[f"supplement-index-dyn{_ib}"] = dict(
        depth=2, budget=6000, max_nodes=800, direction="both",
        relations=_CONTAINMENT, order="current", supplement=True, index_budget=_ib,
        index_dynamic=True)


def _graph_for(wt: Path, want_supplement: bool) -> dict:
    """The task graph, with or without the supplement -- never a mix.

    The supplemented graph lives in a sibling file so the stock graph the other
    configs score against is never rewritten in place; both are derived from
    the same extraction.
    """
    stock = wt / "graphify-out" / "graph.json"
    if not want_supplement:
        return load_graph_json(wt)
    sup = wt / "graphify-out" / "graph.supplemented.json"
    if not sup.exists() or sup.stat().st_mtime < stock.stat().st_mtime:
        sup.write_text(stock.read_text(encoding="utf-8"), encoding="utf-8")
        supplement.apply(sup, root=wt)
    return graphio.load(sup)


def targets(task: dict) -> set[tuple[str, int]]:
    out = set()
    for g in task["discover"]:
        path, name = g.split("::", 1)
        sym = next(s for s in task["by_file"][path] if s["name"] == name)
        out.add((path, sym["def_line"]))
    return out


def score_task(task: dict, cfg: dict) -> dict | None:
    wt, _ = prepare(task)
    data = _graph_for(wt, bool(cfg.get("supplement")))
    seed = resolve_entry(data, task)
    if seed is None:
        return {"recall": None, "reason": "entry symbol has no graph node"}
    tgt = targets(task)
    pack = ctxmod.build_context(
        data, seed, wt, depth=cfg["depth"], direction=cfg["direction"],
        budget=cfg["budget"], relations=cfg["relations"],
        max_nodes=cfg["max_nodes"], order=cfg["order"],
        index_budget=int(cfg.get("index_budget", 0)),
        index_dynamic=bool(cfg.get("index_dynamic", False)),
    )
    found = {(str(i["file"]), int(i["def_line"])) for i in pack["included"]
             if str(i["id"]) != seed}
    indexed = {(str(i["file"]), int(i["def_line"])) for i in pack["index"]}
    hit = found & tgt
    hit_idx = (found | indexed) & tgt
    return {
        # `recall` is BODIES only -- source the agent can read without opening
        # a file. `recall_index` adds symbols the index tier NAMED (file:line +
        # signature); the agent still has to open those, so the two are
        # reported side by side and never summed into one number.
        "recall": round(len(hit) / len(tgt), 4) if tgt else None,
        "recall_index": round(len(hit_idx) / len(tgt), 4) if tgt else None,
        "precision": round(len(hit) / len(found), 4) if found else 0.0,
        "returned": len(found),
        "indexed": len(indexed),
        "tokens_used": pack["tokens_used"],
        "omitted": len(pack["omitted"]),
        "omitted_high_rank": sum(1 for o in pack["omitted"]
                                 if o.get("severity") == "truncated_high_rank"),
        "unmodelled": len(pack["unmodelled"]),
        "unresolved": len(pack["unresolved"]),
    }


def run(corpus: list[dict], cfg_name: str) -> dict:
    cfg = CONFIGS[cfg_name]
    rows = {}
    for t in corpus:
        key = f"{t.get('repo', 'requests')}/{t['commit'][:8]}"
        rows[key] = score_task(t, cfg)
        print(f"  {key:<26} recall={rows[key].get('recall')}", flush=True)
    return {
        "config": cfg_name,
        # the numbers that have historically differed between "identical" runs
        "config_detail": {k: (list(v) if isinstance(v, tuple) else v)
                          for k, v in cfg.items()},
        "corpus_size": len(corpus),
        "repos": sorted({t.get("repo", "requests") for t in corpus}),
        "tasks": rows,
    }


def aggregate(rows: dict) -> dict:
    rs = [r["recall"] for r in rows.values() if r.get("recall") is not None]
    ps = [r["precision"] for r in rows.values() if r.get("precision") is not None]
    return {
        "n_scored": len(rs),
        "mean_recall": round(st.mean(rs), 4) if rs else None,
        "median_recall": round(st.median(rs), 4) if rs else None,
        "mean_precision": round(st.mean(ps), 4) if ps else None,
        "zeros": sum(1 for x in rs if x == 0.0),
        "perfect": sum(1 for x in rs if x == 1.0),
    }


def compare(base: dict, now: dict) -> int:
    base["config_detail"].setdefault("supplement", False)
    for d in (base["config_detail"], now["config_detail"]):
        d.setdefault("index_budget", 0)
        d.setdefault("index_dynamic", False)
    if base["config_detail"] != now["config_detail"]:
        print("REFUSING TO COMPARE — configuration differs from the baseline:")
        for k in sorted(set(base["config_detail"]) | set(now["config_detail"])):
            b, n = base["config_detail"].get(k), now["config_detail"].get(k)
            if b != n:
                print(f"   {k}: baseline={b!r}  now={n!r}")
        print("Re-capture with --update, or run the config the baseline used.")
        return 2

    ups, downs, newt, gone = [], [], [], []
    for key, now_row in now["tasks"].items():
        if key not in base["tasks"]:
            newt.append(key)
            continue
        b, n = base["tasks"][key].get("recall"), now_row.get("recall")
        if b is None or n is None:
            if b != n:
                downs.append((key, b, n))
            continue
        if n > b + 1e-9:
            ups.append((key, b, n))
        elif n < b - 1e-9:
            downs.append((key, b, n))
    gone = [k for k in base["tasks"] if k not in now["tasks"]]

    print("\n--- per-task diff (this is the part that matters) ---")
    for key, b, n in downs:
        print(f"  REGRESSED  {key:<26} {b} -> {n}")
    for key, b, n in ups:
        print(f"  improved   {key:<26} {b} -> {n}")
    for key in newt:
        print(f"  new task   {key}")
    for key in gone:
        print(f"  MISSING    {key}  (in baseline, absent now)")
    if not (ups or downs or newt or gone):
        print("  no per-task change")

    ba, na = aggregate(base["tasks"]), aggregate(now["tasks"])
    print("\n--- aggregate (read AFTER the per-task diff) ---")
    for k in ba:
        mark = "" if ba[k] == na[k] else "   <-- changed"
        print(f"  {k:<16} {ba[k]}  ->  {na[k]}{mark}")

    if downs or gone:
        print(f"\nFAIL: {len(downs)} task(s) regressed, {len(gone)} missing")
        return 1
    print("\nOK: no task regressed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--config", default="default", choices=sorted(CONFIGS))
    ap.add_argument("--update", action="store_true", help="write a new baseline")
    ap.add_argument("--baseline", default=None)
    args = ap.parse_args()

    corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
    base_path = Path(args.baseline or HERE / f"baseline-{args.config}.json")

    print(f"corpus: {len(corpus)} tasks · config: {args.config}")
    now = run(corpus, args.config)

    if args.update or not base_path.exists():
        base_path.write_text(json.dumps(now, indent=2), encoding="utf-8")
        agg = aggregate(now["tasks"])
        print(f"\nwrote baseline {base_path.name}")
        for k, v in agg.items():
            print(f"  {k:<16} {v}")
        return 0

    base = json.loads(base_path.read_text(encoding="utf-8"))
    return compare(base, now)


if __name__ == "__main__":
    raise SystemExit(main())
