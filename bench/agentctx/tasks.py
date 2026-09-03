"""Build a frozen, ground-truth task set from a repo's own fix history.

Why this exists
---------------
Scoring a code-graph tool against tasks *I* invent grades my own homework. So the
ground truth here comes from the repository's real commits: for a bug-fix commit
``C`` with parent ``P``, the symbols whose bodies ``C`` modified are exactly the
context an agent would have had to find in order to write that fix.

Everything is evaluated against the tree at ``P`` — the pre-fix state. Building
at ``C`` would leak the answer.

Definitions
-----------
``G``  the ground-truth symbol set: enclosing functions/classes of every changed
       hunk, resolved by tree-sitter against each file **as it existed at P**
       (old-side line numbers, not new-side).
``E``  the entry point: a symbol named in the commit message / PR title. Real bug
       reports name the failing function; the agent starts there. ``E`` is picked
       from message text only, never by looking at which choice scores best.
``G_discover = G - {E}``  what the agent must actually *discover*. Recall is
       measured over this set, because crediting a tool for returning the symbol
       it was handed would inflate every arm equally and measure nothing.

A task is kept only if ``|G| >= 2`` (so ``G_discover`` is non-empty) and an ``E``
can be found in the message. Those filters are objective and are applied *before*
any tool is run.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import tree_sitter_python as tsp
from tree_sitter import Language, Parser

HERE = Path(__file__).resolve().parent
REPO = HERE / "repo"

# requests moved to a src/ layout partway through its history; accept both so the
# task set is not silently restricted to one era of the project.
PKG_PREFIXES = ("src/requests/", "requests/")

_PY_LANG = Language(tsp.language())


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True,
    )
    return out.stdout.decode("utf-8", "replace")


def git_bytes(repo: Path, *args: str) -> bytes:
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)
    return out.stdout


def is_pkg_py(path: str) -> bool:
    """A production source file of the package (not tests, not setup/docs)."""
    if not path.endswith(".py"):
        return False
    if "/tests/" in path or path.startswith("tests/"):
        return False
    return any(path.startswith(p) for p in PKG_PREFIXES)


# --------------------------------------------------------------------------
# symbol extents
# --------------------------------------------------------------------------

def symbol_table(source: bytes) -> list[dict]:
    """Every function/class in ``source`` with its true extent.

    Uses tree-sitter's ``start_point``/``end_point`` — the same parser graphify
    itself depends on. Names are qualified by their enclosing definitions, so a
    method comes back as ``Class.method`` rather than a bare ``method``.
    """
    parser = Parser(_PY_LANG)
    tree = parser.parse(source)
    out: list[dict] = []

    def name_of(node) -> str | None:
        ident = node.child_by_field_name("name")
        return ident.text.decode("utf-8", "replace") if ident is not None else None

    def walk(node, prefix: tuple[str, ...]) -> None:
        for child in node.children:
            if child.type in ("function_definition", "class_definition"):
                nm = name_of(child)
                if nm is None:
                    walk(child, prefix)
                    continue
                qual = (*prefix, nm)
                out.append({
                    "name": ".".join(qual),
                    "kind": "class" if child.type == "class_definition" else "function",
                    # tree-sitter rows are 0-based; git/editor lines are 1-based.
                    "start": child.start_point[0] + 1,
                    "end": child.end_point[0] + 1,
                })
                walk(child, qual)
            else:
                walk(child, prefix)

    walk(tree.root_node, ())
    return out


def enclosing(symbols: list[dict], line: int) -> str | None:
    """Innermost symbol containing ``line``; None if the line is module-level.

    Innermost wins so a one-line change inside a method is attributed to the
    method, not to its class — attributing to the class would make the ground
    truth coarser than the fix actually was.
    """
    best: dict | None = None
    for s in symbols:
        if s["start"] <= line <= s["end"]:
            if best is None or (s["end"] - s["start"]) < (best["end"] - best["start"]):
                best = s
    return best["name"] if best else None


# --------------------------------------------------------------------------
# diff -> changed lines on the OLD side
# --------------------------------------------------------------------------

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def changed_old_lines(repo: Path, commit: str) -> dict[str, list[int]]:
    """Map file -> old-side line numbers the commit touched.

    ``-U0`` keeps hunks tight so the ground truth is the symbols actually edited
    rather than everything within three lines of an edit. Pure insertions have an
    old-side count of 0; the insertion point still identifies the enclosing
    symbol, so the anchor line is kept.
    """
    diff = git(
        repo, "show", "--format=", "--unified=0", "--no-renames",
        "--diff-filter=M", commit,
    )
    per_file: dict[str, list[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("--- a/"):
            current = line[6:].strip()
            continue
        if line.startswith("--- "):
            current = None
            continue
        if line.startswith("@@") and current and is_pkg_py(current):
            m = HUNK.match(line)
            if not m:
                continue
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count == 0:
                per_file.setdefault(current, []).append(start)
            else:
                per_file.setdefault(current, []).extend(range(start, start + count))
    return per_file


def ground_truth(repo: Path, commit: str) -> tuple[dict[str, list[dict]], list[str]]:
    """(file -> changed symbols with extents, flat qualified list), resolved at the parent.

    Extents are carried through because graph nodes are matched to ground truth
    on ``(source_file, start line)``: graphify labels a method ``.json()`` with
    no class qualifier, so joining on names would be ambiguous, while its
    ``source_location`` is exactly tree-sitter's start line.
    """
    parent = git(repo, "rev-parse", f"{commit}^").strip()
    per_file = changed_old_lines(repo, commit)
    by_file: dict[str, list[dict]] = {}
    flat: list[str] = []
    for path, lines in per_file.items():
        try:
            src = git_bytes(repo, "show", f"{parent}:{path}")
        except subprocess.CalledProcessError:
            continue  # file did not exist at P (add), nothing to resolve against
        syms = symbol_table(src)
        by_name = {s["name"]: s for s in syms}
        names: list[str] = []
        for ln in lines:
            nm = enclosing(syms, ln)
            if nm and nm not in names:
                names.append(nm)
        if names:
            by_file[path] = [dict(by_name[n]) for n in names]
            flat.extend(f"{path}::{n}" for n in names)
    return by_file, flat


# --------------------------------------------------------------------------
# entry point, from the commit message only
# --------------------------------------------------------------------------

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
STOPWORDS = {
    "the", "a", "an", "and", "or", "not", "is", "in", "to", "of", "for", "on",
    "fix", "fixes", "fixed", "bug", "issue", "add", "added", "use", "when",
    "if", "we", "it", "this", "that", "be", "by", "with", "from", "as", "py",
    "python", "requests", "test", "tests", "merge", "pull", "request", "branch",
    "release", "version", "update", "updated", "docs", "doc", "http", "https",
}


def pick_entry(message: str, gt_flat: list[str], repo: Path, parent: str) -> str | None:
    """Symbol named in the commit message, preferring one that is in ``G``.

    Preferring a ``G`` member is not score-fitting: a real report names the
    function that misbehaved, and that function is by definition one the fix
    touched. What must never happen is picking ``E`` to make a tool look good —
    so the choice is a deterministic function of message word order alone.
    """
    gt_names = {g.split("::", 1)[1] for g in gt_flat}
    gt_leaf = {n.split(".")[-1]: n for n in gt_names}

    for tok in IDENT.findall(message):
        if tok.lower() in STOPWORDS or len(tok) < 4:
            continue
        if tok in gt_names:
            return tok
        leaf = tok.split(".")[-1]
        if leaf in gt_leaf:
            return gt_leaf[leaf]
    return None


# --------------------------------------------------------------------------

def build(repo: Path, limit: int, scan: int) -> list[dict]:
    log = git(
        repo, "log", "--no-merges", f"-n{scan}", "--format=%H%x00%s%x00%b%x01",
        "--", *PKG_PREFIXES,
    )
    tasks: list[dict] = []
    skipped = {"no_gt": 0, "single_symbol": 0, "no_entry": 0,
               "revert": 0, "duplicate_entry": 0}
    seen_entries: set[str] = set()

    for record in log.split("\x01"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x00")
        if len(parts) < 3:
            continue
        sha, subject, body = parts[0].strip(), parts[1], parts[2]
        message = f"{subject}\n{body}"

        # A revert is the inverse of another commit and usually of another
        # candidate task; keeping both double-counts one change and lets a
        # single area dominate the aggregate.
        if subject.lower().startswith("revert"):
            skipped["revert"] += 1
            continue

        try:
            by_file, flat = ground_truth(repo, sha)
        except subprocess.CalledProcessError:
            continue
        if not flat:
            skipped["no_gt"] += 1
            continue
        if len(flat) < 2:
            skipped["single_symbol"] += 1
            continue

        parent = git(repo, "rev-parse", f"{sha}^").strip()
        entry = pick_entry(message, flat, repo, parent)
        if entry is None:
            skipped["no_entry"] += 1
            continue

        discover = [g for g in flat if g.split("::", 1)[1] != entry]
        if not discover:
            skipped["single_symbol"] += 1
            continue

        # One entry symbol per task set. The #4965 `Response.content` series
        # alone supplied three near-identical commits; scoring all of them
        # would weight one method as heavily as five unrelated areas and make
        # the aggregate a measure of that method rather than of the tools.
        if entry in seen_entries:
            skipped["duplicate_entry"] += 1
            continue
        seen_entries.add(entry)

        tasks.append({
            "commit": sha,
            "parent": parent,
            "subject": subject,
            "entry": entry,
            "ground_truth": flat,
            "discover": discover,
            "by_file": by_file,
        })
        if len(tasks) >= limit:
            break

    print(f"selected {len(tasks)} tasks; skipped {skipped}", file=sys.stderr)
    return tasks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(REPO))
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--scan", type=int, default=400)
    ap.add_argument("--out", default=str(HERE / "tasks.json"))
    args = ap.parse_args()

    tasks = build(Path(args.repo), args.limit, args.scan)
    Path(args.out).write_text(json.dumps(tasks, indent=2), encoding="utf-8")
    for t in tasks:
        print(f"{t['commit'][:10]}  E={t['entry']:<34} |G|={len(t['ground_truth'])} "
              f"|discover|={len(t['discover'])}  {t['subject'][:52]}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
