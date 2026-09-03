"""Score each arm on the frozen task set: can an agent reach fix-ready context?

Arms
----
``grep``            no graph at all — what an agent does with ripgrep. Without this
                    arm the exercise cannot say whether *any* graph beats search.
``stock-affected``  ``graphify affected`` — stock's reverse impact analysis.
``stock-explain``   ``graphify explain`` — stock's bidirectional depth-1 neighbourhood.
                    Included so stock is represented at its strongest, not its weakest.
``ext-up``          ``graphify-ext blast-radius`` at its default ``--direction up``.
``ext-both``        the same with ``--direction both``, the custom build's own addition.

Method notes that matter for reading the numbers
------------------------------------------------
* **Symbols are matched on (file, definition line), never on labels.** graphify
  labels a method ``.json()`` with no class qualifier, so labels are ambiguous
  within a file; ``source_location`` is exactly tree-sitter's start line.
* **Stock's printed line is the call *site*, not the definition line** (upstream
  labels it ``[via_relation]`` precisely so it is not mistaken for one). So stock's
  hits are resolved through its own ``affected_nodes`` API to get node ids, while
  its CLI text is what gets tokenised. Parsing its text for locations would have
  scored stock against lines it never claimed were definitions.
* **Tokens are counted with tiktoken cl100k_base** on the exact bytes each arm
  prints — not with the ``estimated_tokens`` chars/4 approximation, which is a
  product feature under test here and would be circular as a measuring tool.
* Recall is over ``G_discover`` (ground truth minus the entry symbol the agent was
  handed). Precision is over everything the arm returned.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tasks import symbol_table, enclosing  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE / "repo"
WT = HERE / "wt"
RAW = HERE / "raw"

STOCK = "C:/Users/SISABenjaminDavid/AppData/Local/Programs/Python/Python312/Scripts/graphify.EXE"
EXT = "C:/projects/graphifyrebuild/.venv/Scripts/graphify-ext.exe"

ENC = tiktoken.get_encoding("cl100k_base")
DEPTH = 2


def toks(text: str) -> int:
    return len(ENC.encode(text))


def run(cmd: list[str], cwd: Path) -> str:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True)
    return p.stdout.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# environment: one worktree + one graph per task, at the PARENT commit
# --------------------------------------------------------------------------

def prepare(task: dict, rebuild: bool = False) -> tuple[Path, float]:
    wt = WT / task["commit"][:8]
    if not wt.exists():
        subprocess.run(
            ["git", "worktree", "add", "-f", str(wt), task["parent"]],
            cwd=REPO, capture_output=True, check=True,
        )
    graph = wt / "graphify-out" / "graph.json"
    build_s = 0.0
    if rebuild or not graph.exists():
        import time
        t0 = time.perf_counter()
        subprocess.run([STOCK, "extract", ".", "--code-only", "--no-cluster"],
                       cwd=wt, capture_output=True)
        build_s = time.perf_counter() - t0
    return wt, build_s


def load_graph_json(wt: Path) -> dict:
    return json.loads((wt / "graphify-out" / "graph.json").read_text(encoding="utf-8"))


def def_line(node: dict) -> int | None:
    loc = str(node.get("source_location") or "")
    if loc.startswith("L") and loc[1:].isdigit():
        return int(loc[1:])
    return None


def node_key(node: dict) -> tuple[str, int] | None:
    f, l = node.get("source_file"), def_line(node)
    return (str(f), l) if f and l else None


def resolve_entry(data: dict, task: dict) -> str | None:
    """Graph node id for the entry symbol, matched on (file, definition line)."""
    want = None
    for path, syms in task["by_file"].items():
        for s in syms:
            if s["name"] == task["entry"]:
                want = (path, s["start"])
    if want is None:
        return None
    cands = [n for n in data["nodes"] if node_key(n) == want]
    if not cands:
        return None
    # A definition line can carry both the symbol and a doc/rationale node;
    # prefer the callable, which is the one an agent means.
    cands.sort(key=lambda n: (not n.get("_callable"), len(str(n.get("label", "")))))
    return str(cands[0]["id"])


# --------------------------------------------------------------------------
# arms
# --------------------------------------------------------------------------

def arm_grep(wt: Path, task: dict) -> tuple[set[tuple[str, int]], str]:
    """Agent with no graph: ripgrep the entry symbol's leaf name across the tree."""
    leaf = task["entry"].split(".")[-1]
    text = run(["rg", "-n", "--no-heading", "-g", "*.py", "-g", "!graphify-out",
                r"\b" + leaf + r"\b", "."], wt)
    found: set[tuple[str, int]] = set()
    tables: dict[str, list[dict]] = {}
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        rel = parts[0].replace("\\", "/").lstrip("./")
        try:
            if rel not in tables:
                tables[rel] = symbol_table((wt / rel).read_bytes())
        except OSError:
            continue
        nm = enclosing(tables[rel], int(parts[1]))
        if nm:
            sym = next((s for s in tables[rel] if s["name"] == nm), None)
            if sym:
                found.add((rel, sym["start"]))
    return found, text


def arm_stock_affected(wt: Path, data: dict, seed: str, task: dict):
    from graphify.affected import (DEFAULT_AFFECTED_RELATIONS, affected_nodes,
                                   load_graph, resolve_seed)
    gp = wt / "graphify-out" / "graph.json"
    g = load_graph(gp)
    s = resolve_seed(g, seed, gp.parent.parent)
    text = run([STOCK, "affected", seed, "--depth", str(DEPTH)], wt)
    if s is None:
        return set(), text
    hits = affected_nodes(g, s, relations=DEFAULT_AFFECTED_RELATIONS, depth=DEPTH)
    by_id = {str(n["id"]): n for n in data["nodes"]}
    found = {k for h in hits if (k := node_key(by_id.get(h.node_id, {})))}
    return found, text


def arm_stock_explain(wt: Path, data: dict, seed: str):
    """Parse explain's neighbour list; its ids are recoverable by label+file pair."""
    text = run([STOCK, "explain", seed], wt)
    by_id = {str(n["id"]): n for n in data["nodes"]}
    edges = data.get("links") or data.get("edges") or []
    found: set[tuple[str, int]] = set()
    for e in edges:
        s, t = str(e.get("source")), str(e.get("target"))
        other = t if s == seed else (s if t == seed else None)
        if other and (k := node_key(by_id.get(other, {}))):
            found.add(k)
    return found, text


def arm_ext(wt: Path, data: dict, seed: str, direction: str):
    from graphify_ext import blast_radius as br
    res = br.blast_radius(data, seed, depth=DEPTH, direction=direction)
    by_id = {str(n["id"]): n for n in data["nodes"]}
    found = {k for n in res["nodes"]
             if str(n["id"]) != seed and (k := node_key(by_id.get(str(n["id"]), {})))}
    cmd = [EXT, "blast-radius", seed, "--depth", str(DEPTH)]
    if direction != "up":
        cmd += ["--direction", direction]
    return found, run(cmd, wt)


# --------------------------------------------------------------------------

def arm_ext_context(wt: Path, data: dict, seed: str, budget: int):
    """The custom build's context pack — the only arm that returns source code."""
    from graphify_ext import context as ctxmod
    pack = ctxmod.build_context(data, seed, wt, depth=DEPTH, direction="both",
                                budget=budget)
    found = {(str(i["file"]), int(i["def_line"])) for i in pack["included"]
             if str(i["id"]) != seed}
    return found, pack["text"], pack


def score(found: set, target: set, tokens: int, text: str) -> dict:
    hit = found & target
    return {
        "returned": len(found),
        "recall": round(len(hit) / len(target), 3) if target else None,
        "hits": len(hit),
        "precision": round(len(hit) / len(found), 3) if found else 0.0,
        "tokens": tokens,
        "files_to_open": len({f for f, _ in found}),
        "chars": len(text),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=str(HERE / "tasks.json"))
    ap.add_argument("--out", default=str(HERE / "results.json"))
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    RAW.mkdir(exist_ok=True)
    tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    rows = []

    for i, task in enumerate(tasks, 1):
        sha = task["commit"][:8]
        wt, build_s = prepare(task, args.rebuild)
        data = load_graph_json(wt)
        seed = resolve_entry(data, task)
        target = {(g.split("::", 1)[0],
                   next(s["start"] for s in task["by_file"][g.split("::", 1)[0]]
                        if s["name"] == g.split("::", 1)[1]))
                  for g in task["discover"]}

        row = {"commit": sha, "subject": task["subject"], "entry": task["entry"],
               "seed_node": seed, "n_discover": len(target),
               "graph_nodes": len(data["nodes"]), "build_s": round(build_s, 2),
               "arms": {}}

        f, t = arm_grep(wt, task)
        row["arms"]["grep"] = score(f, target, toks(t), t)
        (RAW / f"{sha}.grep.txt").write_text(t, encoding="utf-8")

        if seed is None:
            row["error"] = "entry symbol has no graph node"
            rows.append(row)
            print(f"[{i}/{len(tasks)}] {sha} SEED-MISSING {task['entry']}")
            continue

        for name, fn in (
            ("stock-affected", lambda: arm_stock_affected(wt, data, seed, task)),
            ("stock-explain", lambda: arm_stock_explain(wt, data, seed)),
            ("ext-up", lambda: arm_ext(wt, data, seed, "up")),
            ("ext-both", lambda: arm_ext(wt, data, seed, "both")),
            ("ext-context", lambda: arm_ext_context(wt, data, seed, 6000)[:2]),
        ):
            try:
                f, t = fn()
            except Exception as exc:  # a crash is a result, not a reason to stop
                row["arms"][name] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            row["arms"][name] = score(f, target, toks(t), t)
            # Only the context arm hands the agent source; every other arm hands
            # it names and line numbers it must still go and open.
            row["arms"][name]["delivers_code"] = (name == "ext-context")
            if name == "ext-context":
                row["arms"][name]["files_to_open"] = 0
            (RAW / f"{sha}.{name}.txt").write_text(t, encoding="utf-8")

        rows.append(row)
        r = {k: v.get("recall") for k, v in row["arms"].items()}
        print(f"[{i}/{len(tasks)}] {sha} |D|={len(target)} recall={r}")

    Path(args.out).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
