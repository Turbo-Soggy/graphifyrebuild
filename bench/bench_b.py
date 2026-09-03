#!/usr/bin/env python3
"""Section B — per-branch caching, timed against stock. The missing headline number.

B1  cold full build: stock vs ext (should be EQUAL — same builder)
B2  branch switch A->B->A under stock (full rebuild every time)
B3  branch switch A->B->A under ext (first visit full, revisit incremental)
B5  commit-triggered incremental: stock vs ext (should be near-identical)
B6  disk footprint after visiting N branches
B7  history rewrite -> full rebuild, and the ancestry check is what fires
B8  detached HEAD -> full rebuild
B9  long-diverged branches -> no false "cache invalid"
B10 corrupted cache slot -> graceful fallback, no crash, no malformed graph

Run under both link and copy mode via --mode (B11 / critique #4).

Usage:
  python bench_b.py [--sandbox PATH] [--mode link|copy] [--iterations N]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402

R = H.Results("Section B")
TIMINGS: dict[str, list[float]] = {}


def record(label: str, seconds: float) -> None:
    TIMINGS.setdefault(label, []).append(seconds)


def best(label: str) -> float:
    """Minimum observed time — the measurement least polluted by AV scans,
    background work, and scheduler noise. Reported alongside the median."""
    return min(TIMINGS.get(label, [float("inf")]))


def med(label: str) -> float:
    vals = TIMINGS.get(label, [])
    return statistics.median(vals) if vals else float("inf")


def fmt(label: str) -> str:
    vals = TIMINGS.get(label, [])
    if not vals:
        return "n/a"
    return (f"min {min(vals):.1f}s / med {statistics.median(vals):.1f}s"
            + (f" (n={len(vals)})" if len(vals) > 1 else ""))


def make_branch_diff(repo: Path, n_files: int = 3) -> list[str]:
    """Realistic branch difference: modify a few existing source files."""
    changed = []
    src = H.source_files(repo, n_files)
    for i, f in enumerate(src):
        f.write_text(f.read_text(encoding="utf-8")
                     + f"\n\ndef _bench_added_{i}():\n    return {i}\n",
                     encoding="utf-8")
        changed.append(H.norm_rel(repo, f))
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sandbox", default=str(H.BENCH / "sandbox-a"))
    ap.add_argument("--mode", choices=["link", "copy"], default="link")
    ap.add_argument("--iterations", type=int, default=3)
    args = ap.parse_args()

    repo = Path(args.sandbox).resolve()
    N = args.iterations
    mode_env = {"GRAPHIFY_EXT_LINK_MODE": args.mode}
    os.environ["GRAPHIFY_EXT_LINK_MODE"] = args.mode

    print(f"=== Section B — mode={args.mode}, iterations={N}, repo={repo.name} ===")
    base = H.base_commit(repo)   # pinned tag, never run-time HEAD
    MAIN = H.default_branch(repo)
    H.ensure_excludes(repo)
    H.uninstall_hooks(repo)
    H.reset_repo(repo, base)
    H.assert_pristine(repo)
    print(f"baseline pinned at {base[:8]}")

    # ------------------------------------------------------------------ B1
    print("\n== B1: cold full build, stock vs ext ==")
    # Like-for-like matters here. `graphify . --code-only` is the extract CLI;
    # ext's first visit calls _rebuild_code(force=True) — which is what stock's
    # OWN post-checkout hook runs. Comparing ext against the extract CLI would
    # charge ext for work the two paths don't share, so the pass/fail check
    # uses the same-code-path baseline and the CLI number is context only.
    for i in range(N):
        H.reset_graph_state(repo)
        t, _ = H.timed(H.stock_full_build, repo)
        if i or N == 1:  # discard the first (cold FS cache)
            record("B1 stock extract CLI", t)
        print(f"    stock `graphify . --code-only` #{i + 1}: {t:.1f}s")

    for i in range(N):
        H.reset_graph_state(repo)
        H.stock_full_build(repo)  # seed a graph for _rebuild_code to rebuild
        t, _ = H.timed(H.stock_rebuild_full, repo)
        if i or N == 1:
            record("B1 stock _rebuild_code full", t)
        print(f"    stock _rebuild_code full (hook path) #{i + 1}: {t:.1f}s")

    for i in range(N):
        H.reset_graph_state(repo)
        t, r = H.timed(H.ext_swap, repo)
        out = (r.stdout or "") + (r.stderr or "")
        if i or N == 1:
            record("B1 ext first-visit (full)", t)
        print(f"    ext first-visit #{i + 1}: {t:.1f}s "
              f"({'full rebuild' if 'full rebuild' in out else '?'})")
    R.info("B1", "stock extract CLI (context, different entry point)",
           fmt("B1 stock extract CLI"))
    R.info("B1", "stock _rebuild_code full — the hook path", fmt("B1 stock _rebuild_code full"))
    R.info("B1", "ext first visit (full build)", fmt("B1 ext first-visit (full)"))
    ratio = best("B1 ext first-visit (full)") / max(best("B1 stock _rebuild_code full"), 1e-9)
    R.check("B1", "ext cold build within 1.5x of the same-code-path stock baseline",
            ratio < 1.5, f"{ratio:.2f}x vs _rebuild_code full")

    # Build the two-branch scenario used by B2/B3.
    H.reset_repo(repo, base)
    H.git(repo, "checkout", "-q", "-b", "feature")
    changed = make_branch_diff(repo)
    H.git(repo, "add", "-A")
    H.git(repo, "commit", "-q", "-m", "feature work")
    H.git(repo, "checkout", "-q", MAIN)

    # ------------------------------------------------------------------ B2
    print("\n== B2: branch switch main->feature->main, STOCK (full rebuild each) ==")
    H.reset_graph_state(repo)
    H.stock_full_build(repo)
    for i in range(N):
        H.git(repo, "checkout", "-q", "feature")
        t, _ = H.timed(H.stock_rebuild_full, repo)
        record("B2 stock switch->feature", t)
        print(f"    stock switch to feature #{i + 1}: {t:.1f}s")
        H.git(repo, "checkout", "-q", MAIN)
        t, _ = H.timed(H.stock_rebuild_full, repo)
        record("B2 stock switch->main", t)
        print(f"    stock switch back to main #{i + 1}: {t:.1f}s")
    R.info("B2", "stock switch to feature", fmt("B2 stock switch->feature"))
    R.info("B2", "stock switch back to main", fmt("B2 stock switch->main"))
    stock_switch = min(best("B2 stock switch->feature"), best("B2 stock switch->main"))
    # Stock has no branch cache, so a REVISIT must cost about the same as the
    # first visit. Compare min-to-min: on a machine whose wall-clock timings
    # vary ~2x from antivirus and scheduler noise, comparing spread or medians
    # measures the noise, not the algorithm. Minimum is the least-polluted
    # sample of the same underlying work.
    revisits = TIMINGS.get("B2 stock switch->main", [])
    first, rest = (revisits[0], revisits[1:]) if len(revisits) > 1 else (None, [])
    no_speedup = (min(rest) > first * 0.5) if (first and rest) else True
    R.check("B2", "stock shows no revisit speedup (confirms it has no cache)",
            no_speedup,
            f"first {first:.1f}s vs best revisit {min(rest):.1f}s" if rest else "n/a")

    # ------------------------------------------------------------------ B3
    print("\n== B3: branch switch main->feature->main, EXT ==")
    H.reset_repo(repo, base, keep_branches=("feature",))
    H.git(repo, "checkout", "-q", MAIN)
    t, r = H.timed(H.ext_swap, repo)
    record("B3 ext first visit main", t)
    print(f"    ext first visit main: {t:.1f}s")

    H.git(repo, "checkout", "-q", "feature")
    t, r = H.timed(H.ext_swap, repo)
    out_first = (r.stdout or "") + (r.stderr or "")
    record("B3 ext first visit feature", t)
    print(f"    ext first visit feature: {t:.1f}s")
    R.check("B3", "first visit to a branch is a full build",
            "full rebuild" in out_first or "no cache" in out_first,
            out_first.strip().splitlines()[0][:80] if out_first.strip() else "")

    for i in range(N):
        H.git(repo, "checkout", "-q", MAIN)
        t, r = H.timed(H.ext_swap, repo)
        out = (r.stdout or "") + (r.stderr or "")
        record("B3 ext revisit main", t)
        cached = "no cache" not in out and "full rebuild" not in out
        print(f"    ext revisit main #{i + 1}: {t:.1f}s "
              f"({'CACHE HIT' if cached else 'FULL REBUILD'})")
        H.git(repo, "checkout", "-q", "feature")
        t, r = H.timed(H.ext_swap, repo)
        out = (r.stdout or "") + (r.stderr or "")
        record("B3 ext revisit feature", t)
        print(f"    ext revisit feature #{i + 1}: {t:.1f}s "
              f"({'CACHE HIT' if 'no cache' not in out and 'full rebuild' not in out else 'FULL REBUILD'})")

    R.info("B3", "ext revisit main", fmt("B3 ext revisit main"))
    R.info("B3", "ext revisit feature", fmt("B3 ext revisit feature"))
    ext_revisit = max(best("B3 ext revisit main"), best("B3 ext revisit feature"))
    speedup = stock_switch / max(ext_revisit, 1e-9)
    R.check("B3", "HEADLINE: branch revisit is faster than a stock full rebuild",
            ext_revisit < stock_switch,
            f"{ext_revisit:.1f}s vs stock {stock_switch:.1f}s = {speedup:.1f}x faster")

    # Correctness of the swapped graph, not just its speed.
    H.git(repo, "checkout", "-q", MAIN)
    H.ext_swap(repo)
    g_main = H.load_graph(repo)
    H.git(repo, "checkout", "-q", "feature")
    H.ext_swap(repo)
    g_feat = H.load_graph(repo)
    main_has = any("_bench_added_0" in str(n.get("label", "")) for n in (g_main or {}).get("nodes", []))
    feat_has = any("_bench_added_0" in str(n.get("label", "")) for n in (g_feat or {}).get("nodes", []))
    R.check("B3", "swapped graphs are branch-correct (feature symbol only on feature)",
            feat_has and not main_has, f"main={main_has} feature={feat_has}")

    # ------------------------------------------------------------------ B5
    print("\n== B5: commit-triggered incremental, stock vs ext ==")
    H.git(repo, "checkout", "-q", MAIN)
    H.reset_graph_state(repo)
    H.stock_full_build(repo)
    import shutil
    pristine = repo / "graphify-out-pristine"
    H.rmtree(pristine)
    shutil.copytree(repo / H.OUT, pristine)

    probe = H.source_dir(repo) / "bench_probe.py"
    for i in range(N):
        probe.write_text(f"def bench_probe_{i}():\n    return {i}\n", encoding="utf-8")
        rel = H.norm_rel(repo, probe)
        H.rmtree(repo / H.OUT)
        shutil.copytree(pristine, repo / H.OUT)
        t, _ = H.timed(H.stock_rebuild_incremental, repo, [rel])
        record("B5 stock incremental", t)
        H.rmtree(repo / H.OUT)
        shutil.copytree(pristine, repo / H.OUT)
        t, _ = H.timed(H.run, [sys.executable, "-c",
                               "import sys;from pathlib import Path;"
                               "from graphify_ext.branch_cache import post_commit_update;"
                               f"sys.exit(0 if post_commit_update([Path({rel!r})]) else 1)"],
                       repo)
        record("B5 ext incremental", t)
        print(f"    #{i + 1}: stock {TIMINGS['B5 stock incremental'][-1]:.1f}s  "
              f"ext {TIMINGS['B5 ext incremental'][-1]:.1f}s")
    H.rmtree(pristine)
    probe.unlink(missing_ok=True)
    R.info("B5", "stock incremental", fmt("B5 stock incremental"))
    R.info("B5", "ext incremental", fmt("B5 ext incremental"))
    overhead = best("B5 ext incremental") - best("B5 stock incremental")
    R.check("B5", "ext adds no meaningful overhead to the common commit path",
            overhead < max(1.5, best("B5 stock incremental") * 0.5),
            f"+{overhead:.1f}s over stock {best('B5 stock incremental'):.1f}s")

    # ------------------------------------------------------------------ B6
    print("\n== B6: disk footprint ==")
    H.reset_repo(repo, base, keep_branches=("feature",))
    for br in (MAIN, "feature"):
        H.git(repo, "checkout", "-q", br)
        H.ext_swap(repo)
    cache_size = H.dir_size(repo / H.CACHE)
    slots = [p for p in (repo / H.CACHE).iterdir() if p.is_dir()] if (repo / H.CACHE).exists() else []
    per_slot = cache_size / max(len(slots), 1)
    H.reset_graph_state(repo)
    H.stock_full_build(repo)
    stock_size = H.dir_size(repo / H.OUT)
    R.info("B6", "stock graphify-out", f"{stock_size / 1e6:.1f} MB")
    R.info("B6", "ext cache", f"{cache_size / 1e6:.1f} MB across {len(slots)} slot(s) "
                              f"= {per_slot / 1e6:.1f} MB/branch")
    R.check("B6", "cache grows ~linearly per branch (no hidden blowup)",
            per_slot < stock_size * 2.5,
            f"{per_slot / max(stock_size, 1):.2f}x a single stock output per branch")

    # ------------------------------------------------------------------ B7
    print("\n== B7: history rewrite -> full rebuild, ancestry check is what fires ==")
    H.reset_repo(repo, base, keep_branches=("feature",))
    H.git(repo, "checkout", "-q", "feature")
    H.ext_swap(repo)
    slot = repo / H.CACHE / "feature"
    meta_before = json.loads((slot / "graphify_ext_meta.json").read_text(encoding="utf-8"))
    H.git(repo, "commit", "-q", "--amend", "-m", "rewritten")
    new_head = H.head(repo)
    # Assert the PRECONDITION the check keys on, so a pass can't come from
    # some unrelated cause coincidentally producing the right log line.
    anc = H.run(["git", "merge-base", "--is-ancestor",
                 meta_before["base_commit"], new_head], repo)
    R.check("B7", "precondition: cached base commit is NOT an ancestor of the rewritten HEAD",
            anc.returncode != 0,
            f"base {meta_before['base_commit'][:8]} vs HEAD {new_head[:8]}")
    H.git(repo, "checkout", "-q", MAIN)
    H.ext_swap(repo)
    H.git(repo, "checkout", "-q", "feature")
    r = H.ext_swap(repo)
    out = (r.stdout or "") + (r.stderr or "")
    R.check("B7", "rewritten branch falls back to a full rebuild",
            "cache not trustworthy" in out or "full rebuild" in out,
            [ln for ln in out.splitlines() if "graphify-ext" in ln][:1])
    R.check("B7", "graph is healthy after the fallback",
            bool(H.load_graph(repo)) and H.node_edge_counts(H.load_graph(repo))[0] > 100)

    # ------------------------------------------------------------------ B8
    print("\n== B8: detached HEAD ==")
    sha = H.head(repo)
    H.git(repo, "checkout", "-q", sha)
    r = H.ext_swap(repo)
    out = (r.stdout or "") + (r.stderr or "")
    R.check("B8", "detached HEAD triggers a full rebuild into a scratch slot",
            "detached HEAD" in out, [ln for ln in out.splitlines() if "detached" in ln][:1])
    R.check("B8", "scratch slot used, not a branch-named slot",
            (repo / H.CACHE / "@detached").exists())
    H.git(repo, "checkout", "-q", MAIN)

    # ------------------------------------------------------------------ B9
    print("\n== B9: long-diverged branches must NOT read as 'cache invalid' ==")
    H.reset_repo(repo, base, keep_branches=("feature",))
    # Advance BOTH branches so neither is an ancestor of the other — genuine
    # divergence, which must be distinguished from a history rewrite.
    H.git(repo, "checkout", "-q", MAIN)
    (H.source_dir(repo) / "div_main.py").write_text(
        "def div_main():\n    return 1\n", encoding="utf-8")
    H.git(repo, "add", "-A"); H.git(repo, "commit", "-q", "-m", "main advances")
    H.ext_swap(repo)
    H.git(repo, "checkout", "-q", "feature")
    (H.source_dir(repo) / "div_feat.py").write_text(
        "def div_feat():\n    return 1\n", encoding="utf-8")
    H.git(repo, "add", "-A"); H.git(repo, "commit", "-q", "-m", "feature advances")
    H.ext_swap(repo)
    main_sha, feat_sha = H.git(repo, "rev-parse", MAIN), H.git(repo, "rev-parse", "feature")
    a1 = H.run(["git", "merge-base", "--is-ancestor", main_sha, feat_sha], repo).returncode
    a2 = H.run(["git", "merge-base", "--is-ancestor", feat_sha, main_sha], repo).returncode
    R.check("B9", "precondition: branches are genuinely diverged (neither is an ancestor)",
            a1 != 0 and a2 != 0)
    invalid_seen = []
    for _ in range(2):
        for br in (MAIN, "feature"):
            H.git(repo, "checkout", "-q", br)
            r = H.ext_swap(repo)
            out = (r.stdout or "") + (r.stderr or "")
            if "not trustworthy" in out or "CACHE INVALID" in out:
                invalid_seen.append(br)
    R.check("B9", "divergence never produces a false 'cache invalid'",
            not invalid_seen, f"false invalidations: {invalid_seen or 'none'}")
    # Read the graph only AFTER swapping: a checkout alone leaves graphify-out
    # pointed at the previous branch's slot (in production the post-checkout
    # hook performs this swap).
    H.git(repo, "checkout", "-q", MAIN)
    H.ext_swap(repo)
    g = H.load_graph(repo)
    labels = {str(n.get("label", "")) for n in (g or {}).get("nodes", [])}
    R.check("B9", "each branch's cache stays correctly isolated",
            any("div_main" in x for x in labels) and not any("div_feat" in x for x in labels),
            f"div_main={any('div_main' in x for x in labels)} "
            f"div_feat={any('div_feat' in x for x in labels)}")

    # ------------------------------------------------------------------ B10
    print("\n== B10: corrupted cache slot -> graceful fallback ==")
    # Corrupt the graph with NO pending file changes, so the swap takes the
    # zero-changed fast path. That path returns without touching graph.json, so
    # it is the only one that can serve a corrupt graph — and a variant of this
    # test that happens to leave changes pending passes without exercising it.
    H.reset_repo(repo, base, keep_branches=("feature",))
    H.git(repo, "checkout", "-q", "feature")
    H.ext_swap(repo)
    H.git(repo, "checkout", "-q", MAIN)
    H.ext_swap(repo)
    (repo / H.CACHE / "feature" / "graph.json").write_text(
        "{ this is not valid json", encoding="utf-8")
    H.git(repo, "checkout", "-q", "feature")
    r = H.ext_swap(repo)
    served = (repo / H.OUT / "graph.json").read_text(encoding="utf-8")[:1]
    R.check("B10", "corrupt graph with NO pending changes is not served "
                   "(zero-changed fast path validates the cache)",
            r.returncode == 0 and served == "{" and len(served) > 0
            and H.load_graph(repo) is not None,
            f"rc={r.returncode}")

    for target in ("graph.json", "manifest.json"):
        H.git(repo, "checkout", "-q", "feature")
        H.ext_swap(repo)
        H.git(repo, "checkout", "-q", MAIN)
        H.ext_swap(repo)
        slot_file = repo / H.CACHE / "feature" / target
        if not slot_file.exists():
            R.check("B10", f"slot {target} exists to corrupt", False)
            continue
        slot_file.write_text("{ this is not valid json", encoding="utf-8")
        H.git(repo, "checkout", "-q", "feature")
        r = H.ext_swap(repo)
        out = (r.stdout or "") + (r.stderr or "")
        R.check("B10", f"corrupt {target}: swap does not crash",
                r.returncode == 0, f"rc={r.returncode}")
        g = H.load_graph(repo)
        n, e = H.node_edge_counts(g)
        R.check("B10", f"corrupt {target}: graph recovers to a healthy state",
                bool(g) and n > 100, f"{n} nodes / {e} edges")

    # ------------------------------------------------------------------ done
    H.reset_repo(repo, base)
    H.uninstall_hooks(repo)

    print(f"\n=== timings ({args.mode} mode) ===")
    for label in TIMINGS:
        print(f"  {label:32} {fmt(label)}")
    payload = {"mode": args.mode, "repo": repo.name,
               "timings": {k: v for k, v in TIMINGS.items()}}
    (H.BENCH / f"timings-{args.mode}-{repo.name}.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")
    return R.summary()


if __name__ == "__main__":
    sys.exit(main())
