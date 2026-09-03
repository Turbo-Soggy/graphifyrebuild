"""Validate the corpus's ground truth with a positive AND a negative control.

Why both controls
-----------------
A positive-only check ("every ground-truth symbol contains a changed line")
passes trivially for a builder that attributes every change to every symbol in
the file. The negative control ("no symbol OUTSIDE the ground truth contains a
changed line") is what makes the pair meaningful: together they pin the
attribution exactly, in both directions.

This lives in the repo rather than in a scratch file because it is the evidence
for every recall number the benchmark reports. A ground truth nobody re-checks
is a ground truth that quietly rots — this exact check, re-run after a refactor,
is what caught `def_line` being used as a containment bound, which had
mis-attributed 13 hunk lines in the frozen flask corpus.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluate import repo_for  # noqa: E402
from tasks import changed_old_lines, symbol_table  # noqa: E402

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(HERE / "corpus.json"))
    args = ap.parse_args()

    corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
    per = defaultdict(lambda: [0, 0, 0, 0])   # pos_ok, pos_fail, neg_ok, neg_fail
    failures: list[str] = []

    for task in corpus:
        repo = repo_for(task)
        mods, ins = changed_old_lines(repo, task["commit"],
                                      tuple(task["by_file"].keys()))
        row = per[task["repo"]]
        for path, syms in task["by_file"].items():
            src = subprocess.run(["git", "show", f"{task['parent']}:{path}"],
                                 cwd=repo, capture_output=True).stdout
            table = {s["name"]: s for s in symbol_table(src, path)}
            touched = set(mods.get(path, [])) | set(ins.get(path, []))
            gt = {s["name"] for s in syms}

            for s in syms:
                got = table.get(s["name"])
                if got and any(got["extent_start"] <= ln <= got["end"]
                               for ln in touched):
                    row[0] += 1
                else:
                    row[1] += 1
                    failures.append(
                        f"POS {task['repo']}/{task['commit'][:8]} {path} {s['name']}")

            for name, s in table.items():
                if name in gt:
                    continue
                # A class legitimately spans a changed method; not a violation.
                if any(m.startswith(name + ".") for m in gt):
                    continue
                if any(s["extent_start"] <= ln <= s["end"] for ln in touched):
                    row[3] += 1
                    failures.append(
                        f"NEG {task['repo']}/{task['commit'][:8]} {path} {name}")
                else:
                    row[2] += 1

    print(f"{'repo':<12}{'pos ok':>8}{'pos fail':>10}{'neg ok':>9}{'neg fail':>10}")
    tot = [0, 0, 0, 0]
    for repo, row in sorted(per.items()):
        print(f"{repo:<12}{row[0]:>8}{row[1]:>10}{row[2]:>9}{row[3]:>10}")
        tot = [a + b for a, b in zip(tot, row)]
    print(f"{'TOTAL':<12}{tot[0]:>8}{tot[1]:>10}{tot[2]:>9}{tot[3]:>10}")

    if failures:
        print(f"\n{len(failures)} violation(s):")
        for f in failures[:40]:
            print("  " + f)
        return 1
    print("\nboth controls clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
