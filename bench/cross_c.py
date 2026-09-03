#!/usr/bin/env python3
"""Section C — cross-cutting checks.

C1  graphify-out always resolves to the correct branch slot, never a stale one
C2  concurrent swap while a rebuild is in flight — no corruption, no wrong slot
C3  GRAPHIFY_SKIP_HOOK=1 genuinely suppresses the hook (benchmark isolation)
C4  PYTHONHASHSEED=0 actually stabilises clustering (prove the pin does work)

Usage: python cross_c.py [--sandbox PATH] [--mode link|copy]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402

R = H.Results("Section C")


def active_slot_name(repo: Path) -> str:
    out = repo / H.OUT
    if H.is_link(out):
        return Path(os.path.realpath(out)).name
    owner = out / ".graphify_ext_owner"
    if owner.exists():
        return Path(owner.read_text(encoding="utf-8-sig").strip()).name
    return "(real dir, no owner)"


def communities(repo: Path) -> dict[str, int]:
    g = H.load_graph(repo)
    return {str(n.get("id")): n.get("community")
            for n in (g or {}).get("nodes", []) if n.get("community") is not None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sandbox", default=str(H.BENCH / "sandbox-a"))
    ap.add_argument("--mode", choices=["link", "copy"], default="link")
    args = ap.parse_args()
    repo = Path(args.sandbox).resolve()
    os.environ["GRAPHIFY_EXT_LINK_MODE"] = args.mode

    print(f"=== Section C — mode={args.mode} ===")
    base = H.base_commit(repo)
    MAIN = H.default_branch(repo)
    H.ensure_excludes(repo)
    H.uninstall_hooks(repo)
    H.reset_repo(repo, base)
    H.assert_pristine(repo)

    # Two branches with a real difference.
    H.git(repo, "checkout", "-q", "-b", "cfeature")
    f = H.source_dir(repo) / "c_probe.py"
    f.write_text("def c_probe():\n    return 1\n", encoding="utf-8")
    H.git(repo, "add", "-A")
    H.git(repo, "commit", "-q", "-m", "c probe")
    H.git(repo, "checkout", "-q", MAIN)

    # ------------------------------------------------------------------ C1
    print("\n== C1: graphify-out always resolves to the active branch's slot ==")
    expected_slot = {MAIN: MAIN, "cfeature": "cfeature"}
    ok_all = True
    for step in range(3):
        for br in (MAIN, "cfeature"):
            H.git(repo, "checkout", "-q", br)
            H.ext_swap(repo)
            got = active_slot_name(repo)
            match = got == expected_slot[br]
            ok_all &= match
            if not match:
                print(f"    step {step} {br}: resolved to {got!r}")
    R.check("C1", "output path tracks the active branch across repeated switches",
            ok_all, f"final={active_slot_name(repo)}")

    # A deleted slot must not leave a dangling pointer being served.
    H.git(repo, "checkout", "-q", "cfeature")
    H.ext_swap(repo)
    H.git(repo, "checkout", "-q", MAIN)
    H.ext_swap(repo)
    stale = repo / H.CACHE / "cfeature"
    H.rmtree(stale)
    H.git(repo, "checkout", "-q", "cfeature")
    r = H.ext_swap(repo)
    g = H.load_graph(repo)
    n, _ = H.node_edge_counts(g)
    R.check("C1", "deleted slot is rebuilt, not served as a dangling pointer",
            r.returncode == 0 and n > 100, f"rc={r.returncode}, {n} nodes")

    # ------------------------------------------------------------------ C2
    print("\n== C2: concurrent swap while a rebuild is in flight ==")
    H.git(repo, "checkout", "-q", MAIN)
    H.ext_swap(repo)
    results: list[subprocess.CompletedProcess] = []

    def fire():
        results.append(H.ext_swap(repo))

    # Force real work on both: touch a file so the reconcile is not a no-op.
    touched = H.source_files(repo, 1)[0]
    touched.write_text(touched.read_text(encoding="utf-8")
                       + "\n\ndef _c2_probe():\n    return 1\n", encoding="utf-8")
    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rcs = [r.returncode for r in results]
    combined = "\n".join((r.stdout or "") + (r.stderr or "") for r in results)
    # Both must SUCCEED, not merely survive: a swap that loses the per-repo
    # lock has to wait for it, because a nonzero exit is indistinguishable from
    # a corrupt slot and would trigger the clear-and-full-rebuild fallback.
    R.check("C2", "both concurrent swaps succeed (lock is waited on, not failed on)",
            all(rc == 0 for rc in rcs), f"exit codes {rcs}")
    g = H.load_graph(repo)
    n, e = H.node_edge_counts(g)
    R.check("C2", "graph is well-formed after concurrent access", bool(g) and n > 100,
            f"{n} nodes / {e} edges")
    R.check("C2", "output still points at the current branch's slot",
            active_slot_name(repo) == MAIN, active_slot_name(repo))
    if "already in progress" in combined or "queued" in combined:
        R.info("C2", "contention handling", "one run deferred to the other (lock held)")
    else:
        R.info("C2", "contention handling",
               "both ran to completion (per-repo lock serialised them)")
    H.git(repo, "checkout", "-q", "--", H.norm_rel(repo, touched))

    # ------------------------------------------------------------------ C3
    print("\n== C3: GRAPHIFY_SKIP_HOOK=1 suppresses the hook ==")
    H.install_ext_hooks(repo)
    sha = H.head(repo)
    other = "0" * 40
    fired_without = H.hook_fired(
        H.invoke_hook(repo, "post-checkout", other, sha, "1",
                      env={"GRAPHIFY_OUT": "graphify-out-c3"}))
    if fired_without:
        H.wait_for_rebuild(repo / "graphify-out-c3")
    H.rmtree(repo / "graphify-out-c3")
    fired_with = H.hook_fired(
        H.invoke_hook(repo, "post-checkout", other, sha, "1",
                      env={"GRAPHIFY_SKIP_HOOK": "1"}))
    R.check("C3", "control: hook fires when the flag is unset", fired_without)
    R.check("C3", "GRAPHIFY_SKIP_HOOK=1 prevents the rebuild", not fired_with)

    fired_commit_with = None
    if H.hook_path(repo, "post-commit").exists():
        fired_commit_with = H.hook_fired(
            H.invoke_hook(repo, "post-commit", env={"GRAPHIFY_SKIP_HOOK": "1"}))
        R.check("C3", "GRAPHIFY_SKIP_HOOK=1 also suppresses post-commit",
                not fired_commit_with)
    H.uninstall_hooks(repo)

    # ------------------------------------------------------------------ C4
    print("\n== C4: PYTHONHASHSEED pin actually stabilises clustering ==")
    H.git(repo, "checkout", "-q", MAIN)

    def build_with(seed: str | None) -> dict[str, int]:
        H.reset_graph_state(repo)
        env = {"PYTHONHASHSEED": seed} if seed is not None else {}
        e = dict(os.environ)
        e.pop("PYTHONHASHSEED", None)
        e.update(env)
        e["GRAPHIFY_MAX_WORKERS"] = "1"
        subprocess.run([str(H.GRAPHIFY), ".", "--code-only"], cwd=str(repo),
                       capture_output=True, text=True, env=e)
        return communities(repo)

    pinned = [build_with("0") for _ in range(2)]
    R.check("C4", "PYTHONHASHSEED=0 gives identical community assignments",
            pinned[0] == pinned[1] and bool(pinned[0]),
            f"{len(pinned[0])} clustered nodes")

    unpinned = [build_with(None) for _ in range(3)]
    differ = any(unpinned[0] != u for u in unpinned[1:])
    if differ:
        R.check("C4", "unpinned runs DO vary — the pin is load-bearing", True,
                "community assignments differed between unpinned runs")
    else:
        R.info("C4", "unpinned runs did not vary in this sample",
               "the pin is harmless but unproven on this repo; keep it — "
               "networkx community order is hash-sensitive by construction")

    H.reset_repo(repo, base)
    H.uninstall_hooks(repo)
    return R.summary()


if __name__ == "__main__":
    sys.exit(main())
