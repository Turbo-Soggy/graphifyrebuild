#!/usr/bin/env python3
"""Section A — regression: confirm graphify-ext leaves stock behavior untouched.

Run FIRST. A caching layer on top of a broken base extraction just caches the
wrong answer faster.

A1  commit-triggered incremental produces an identical graph under stock and ext
A2  `git checkout -b` (HEAD unchanged) fires no rebuild under either
A3  `git checkout -- <path>` (file checkout) fires no rebuild under either
A4  query / explain / path give identical output before any ext command is run
A5  `graphify update .` with no ext hooks behaves exactly as upstream documents

Usage: python regression_a.py [sandbox_path]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402

SANDBOX = Path(sys.argv[1]) if len(sys.argv) > 1 else H.BENCH / "sandbox-a"
R = H.Results("Section A (regression)")

PROBE = "src/flask/regr_probe.py"
PROBE_SRC = "def regr_probe_fn():\n    return 1\n"


def main() -> int:
    base = H.base_commit(SANDBOX)   # pinned tag, not run-time HEAD
    H.ensure_excludes(SANDBOX)
    H.uninstall_hooks(SANDBOX)
    H.reset_repo(SANDBOX, base)
    H.assert_pristine(SANDBOX)

    # ---------------------------------------------------------------- A1
    print("\n== A1: commit-triggered incremental, stock vs ext ==")
    # Shared starting point: one full build, snapshotted so both runs start
    # from a byte-identical graph.
    H.stock_full_build(SANDBOX)
    pristine = SANDBOX / "graphify-out-pristine"
    H.rmtree(pristine)
    import shutil
    shutil.copytree(SANDBOX / H.OUT, pristine)

    (SANDBOX / PROBE).write_text(PROBE_SRC, encoding="utf-8")
    H.git(SANDBOX, "add", PROBE)
    H.git(SANDBOX, "commit", "-q", "-m", "regr probe")

    stock_r = H.stock_rebuild_incremental(SANDBOX, [PROBE])
    stock_graph = H.load_graph(SANDBOX)
    R.check("A1", "stock incremental rebuild succeeded", stock_r.returncode == 0)

    # Restore the pre-commit graph, then run the SAME incremental through ext.
    H.rmtree(SANDBOX / H.OUT)
    shutil.copytree(pristine, SANDBOX / H.OUT)
    ext_r = H.run([sys.executable, "-c",
                   "import sys;from pathlib import Path;"
                   "from graphify_ext.branch_cache import post_commit_update;"
                   f"sys.exit(0 if post_commit_update([Path({PROBE!r})]) else 1)"],
                  SANDBOX)
    ext_graph = H.load_graph(SANDBOX)
    R.check("A1", "ext incremental rebuild succeeded", ext_r.returncode == 0,
            (ext_r.stdout or ext_r.stderr or "").strip().splitlines()[-1][:90]
            if ext_r.returncode else "")

    if stock_graph and ext_graph:
        same, detail = H.graphs_identical(stock_graph, ext_graph)
        sn, se = H.node_edge_counts(stock_graph)
        en, ee = H.node_edge_counts(ext_graph)
        R.check("A1", "extracted graph identical to stock (ext edges excluded)",
                same, detail if not same else f"{sn} nodes / {se} edges both")
        R.check("A1", "probe symbol present in both",
                any(n.get("label", "").startswith("regr_probe_fn")
                    for n in stock_graph["nodes"])
                and any(n.get("label", "").startswith("regr_probe_fn")
                        for n in ext_graph["nodes"]))
    else:
        R.check("A1", "both graphs produced", False, "one side missing graph.json")

    H.rmtree(pristine)
    H.reset_repo(SANDBOX, base)

    # ---------------------------------------------------------------- A2/A3
    # Guards live in the hook SCRIPTS, so compare stock and ext hooks directly.
    # Each hook echoes a launch line before spawning; stdout is therefore a
    # synchronous signal of whether the guards let it through.
    for label, installer in (("stock", H.install_stock_hooks),
                             ("ext", H.install_ext_hooks)):
        print(f"\n== A2/A3: {label} hook guards ==")
        H.uninstall_hooks(SANDBOX)
        installer(SANDBOX)
        # Need graphify-out present, or the checkout hook exits on its own guard
        # for an unrelated reason and the test would pass vacuously.
        if not (SANDBOX / H.OUT).exists():
            H.stock_full_build(SANDBOX)
        R.check("A2", f"{label}: post-checkout hook installed",
                H.hook_path(SANDBOX, "post-checkout").exists())

        sha = H.head(SANDBOX)
        # A2: branch switch flag=1 but PREV == NEW (what `checkout -b` reports)
        res = H.invoke_hook(SANDBOX, "post-checkout", sha, sha, "1")
        R.check("A2", f"{label}: no rebuild on `checkout -b` (HEAD unchanged)",
                not H.hook_fired(res),
                (res.stdout or "").strip()[:80])

        # A3: file checkout — third arg 0
        res = H.invoke_hook(SANDBOX, "post-checkout", sha, sha, "0")
        R.check("A3", f"{label}: no rebuild on file checkout (`checkout -- path`)",
                not H.hook_fired(res),
                (res.stdout or "").strip()[:80])

        # Positive control: without it, A2/A3 would pass vacuously if the hook
        # never fired at all. The guards compare PREV vs NEW as strings, so a
        # distinct SHA exercises them exactly as git does. GRAPHIFY_OUT is
        # redirected so the rebuild this genuinely spawns writes to a throwaway
        # directory instead of the graph the later cases depend on.
        throwaway = "graphify-out-poscontrol"
        res = H.invoke_hook(SANDBOX, "post-checkout", "0" * 40, sha, "1",
                            env={"GRAPHIFY_OUT": throwaway})
        fired = H.hook_fired(res)
        R.check("A2", f"{label}: positive control — real branch switch DOES fire",
                fired, (res.stdout or "").strip()[:80])
        if fired:
            H.wait_for_rebuild(SANDBOX / throwaway)
        H.rmtree(SANDBOX / throwaway)

    H.uninstall_hooks(SANDBOX)
    H.reset_repo(SANDBOX, base)

    # ---------------------------------------------------------------- A4
    print("\n== A4: read commands identical before any ext command runs ==")
    H.stock_full_build(SANDBOX)
    baseline = {}
    for name, args in (("query", ["query", "how does routing work"]),
                       ("explain", ["explain", "Flask"]),
                       ("god-nodes", ["god-nodes", "--top", "5"])):
        r = H.run([str(H.GRAPHIFY), *args], SANDBOX)
        baseline[name] = (r.returncode, (r.stdout or "").strip())

    # Install ext + run a read-only ext command; stock reads must not change.
    H.install_ext_hooks(SANDBOX)
    H.run([str(H.GRAPHIFY_EXT), "blast-radius", "Flask", "--depth", "1"], SANDBOX)
    for name, args in (("query", ["query", "how does routing work"]),
                       ("explain", ["explain", "Flask"]),
                       ("god-nodes", ["god-nodes", "--top", "5"])):
        r = H.run([str(H.GRAPHIFY), *args], SANDBOX)
        rc, out = baseline[name]
        R.check("A4", f"stock `{name}` output unchanged",
                r.returncode == rc and (r.stdout or "").strip() == out)

    R.check("A4", "graphify-out is still a real directory (slot machinery inert "
                  "until swap is invoked)",
            (SANDBOX / H.OUT).exists() and not H.is_link(SANDBOX / H.OUT))

    H.uninstall_hooks(SANDBOX)

    # ---------------------------------------------------------------- A5
    print("\n== A5: `graphify update .` with no ext hooks installed ==")
    (SANDBOX / PROBE).write_text(PROBE_SRC.replace("1", "2"), encoding="utf-8")
    r = H.run([str(H.GRAPHIFY), "update", "."], SANDBOX)
    out = (r.stdout or "") + (r.stderr or "")
    R.check("A5", "update exits 0", r.returncode == 0,
            out.strip().splitlines()[-1][:90] if out.strip() else "")
    R.check("A5", "update reports the documented success message",
            "Code graph updated" in out or "updated" in out.lower())
    R.check("A5", "graphify-out still a real directory (no slot side effects)",
            not H.is_link(SANDBOX / H.OUT))

    H.reset_repo(SANDBOX, base)
    return R.summary()


if __name__ == "__main__":
    sys.exit(main())
