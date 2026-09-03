"""Per-task diff between two captured baselines of DIFFERENT configs.

``regress.py`` refuses to compare across configs, correctly: a regression
check must hold everything constant. This is the other tool -- an A/B where
exactly one thing differs and the reader is told what -- and it prints the
per-task movement before any aggregate, per the evidence rules in
plans/04-correctness-roadmap.md.

    python compare_configs.py baseline-default.json baseline-supplement.json
"""

from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path


def agg(rows: dict, keys=None) -> dict:
    keys = list(keys) if keys is not None else list(rows)
    rs = [rows[k]["recall"] for k in keys if rows[k].get("recall") is not None]
    ri = [rows[k].get("recall_index", rows[k]["recall"]) for k in keys
          if rows[k].get("recall") is not None]
    ps = [rows[k]["precision"] for k in keys if rows[k].get("recall") is not None]
    tk = [rows[k].get("tokens_used") for k in keys if rows[k].get("tokens_used") is not None]
    return {
        "n_scored": len(rs),
        "mean_recall": round(st.mean(rs), 4) if rs else None,
        "mean_recall_with_index": round(st.mean(ri), 4) if ri else None,
        "mean_tokens_used": round(st.mean(tk)) if tk else None,
        "median_recall": round(st.median(rs), 4) if rs else None,
        "mean_precision": round(st.mean(ps), 4) if ps else None,
        "zeros": sum(1 for x in rs if x == 0.0),
        "perfect": sum(1 for x in rs if x == 1.0),
    }


def main(a_path: str, b_path: str) -> int:
    a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (a_path, b_path))
    da, db = a["config_detail"], b["config_detail"]
    diff = {k: (da.get(k), db.get(k)) for k in sorted(set(da) | set(db)) if da.get(k) != db.get(k)}
    print(f"A = {a['config']}   B = {b['config']}")
    print("config fields that differ (everything else identical):")
    for k, (x, y) in diff.items():
        print(f"   {k}: A={x!r}  B={y!r}")
    if not diff:
        print("   (none -- use regress.py for a same-config regression check)")

    ra, rb = a["tasks"], b["tasks"]
    newly, lost, ups, downs, same, still = [], [], [], [], [], []
    for k in ra:
        x, y = ra[k].get("recall"), rb.get(k, {}).get("recall")
        if x is None and y is None:
            still.append((k, rb.get(k, {}).get("reason")))
        elif x is None:
            newly.append((k, y, rb[k].get("returned"), rb[k].get("precision")))
        elif y is None:
            lost.append((k, x, rb.get(k, {}).get("reason")))
        elif y > x + 1e-9:
            ups.append((k, x, y))
        elif y < x - 1e-9:
            downs.append((k, x, y))
        else:
            same.append(k)

    print(f"\n--- per-task (A -> B) ---")
    for k, x, y in downs:
        print(f"  REGRESSED       {k:<24} {x} -> {y}")
    for k, x, why in lost:
        print(f"  LOST (unscored) {k:<24} {x} -> None ({why})")
    for k, x, y in ups:
        print(f"  improved        {k:<24} {x} -> {y}")
    for k, y, ret, prec in newly:
        print(f"  newly scoreable {k:<24} None -> {y}  (returned {ret}, precision {prec})")
    for k, why in still:
        print(f"  still unscored  {k:<24} {why}")
    print(f"  unchanged       {len(same)} task(s)")

    print(f"\n--- aggregate (read AFTER the per-task list) ---")
    print(f"  {'':28} {'A':>34}  {'B':>34}")
    aa, ab = agg(ra), agg(rb)
    for k in aa:
        print(f"  {k:<28} {str(aa[k]):>34}  {str(ab[k]):>34}")
    common = [k for k in ra if ra[k].get("recall") is not None and rb.get(k, {}).get("recall") is not None]
    ac, bc = agg(ra, common), agg(rb, common)
    print(f"\n  on the {len(common)} tasks BOTH configs score (apples to apples):")
    for k in ac:
        print(f"  {k:<28} {str(ac[k]):>34}  {str(bc[k]):>34}")
    repos = sorted({k.split('/')[0] for k in ra})
    print("\n  per repo (A | B):")
    for r in repos:
        ks = [k for k in ra if k.startswith(r + "/")]
        x, y = agg({k: ra[k] for k in ks}), agg({k: rb[k] for k in ks})
        print(f"  {r:<10} n={x['n_scored']:>2}|{y['n_scored']:<2} recall={x['mean_recall']}|{y['mean_recall']} "
              f"precision={x['mean_precision']}|{y['mean_precision']} zeros={x['zeros']}|{y['zeros']}")
    for field in ("unresolved", "unmodelled", "omitted"):
        print(f"  total {field:<11} A={sum(ra[k].get(field, 0) for k in ra)}  "
              f"B={sum(rb[k].get(field, 0) for k in rb)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
