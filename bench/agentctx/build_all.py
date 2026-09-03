"""Prepare every corpus task's worktree + graph up front (parallel).

``regress.py`` builds lazily, one task at a time; on a fresh machine that
serialises ~70 stock extractions behind the scoring loop. This does the same
``prepare()`` for every task in a pool so the graphs exist before scoring
starts, and prints one line per task so a stuck build is visible.
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluate import prepare  # noqa: E402

HERE = Path(__file__).resolve().parent


def main() -> int:
    corpus = json.loads((HERE / "corpus.json").read_text(encoding="utf-8"))
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    t0 = time.perf_counter()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(prepare, t): t for t in corpus}
        for fut in as_completed(futs):
            t = futs[fut]
            key = f"{t.get('repo', 'requests')}/{t['commit'][:8]}"
            try:
                wt, build_s = fut.result()
                n = len(json.loads((wt / "graphify-out" / "graph.json")
                                   .read_text(encoding="utf-8"))["nodes"])
                done += 1
                print(f"[{done}/{len(corpus)}] {key:<24} {n:>6} nodes  build {build_s:5.1f}s",
                      flush=True)
            except Exception as exc:  # report, keep going
                print(f"[FAIL] {key}: {type(exc).__name__}: {exc}", flush=True)
    print(f"done in {time.perf_counter() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
