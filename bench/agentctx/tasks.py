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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from graphify_ext import symbols  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE / "repo"

# Directory names that mean "this is test code, not the thing under test".
TEST_SEGMENTS = frozenset({"tests", "test", "spec", "specs", "__tests__", "e2e"})


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True,
    )
    return out.stdout.decode("utf-8", "replace")


def git_bytes(repo: Path, *args: str) -> bytes:
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)
    return out.stdout


def is_pkg_source(path: str, prefixes: tuple[str, ...]) -> bool:
    """A production source file of the package (not tests, not setup/docs).

    Language is decided by ``symbols.language_for`` rather than a local
    extension list, so the corpus can only ever contain files the product can
    actually parse — a task built from a file the tooling cannot read would be
    unscoreable by construction.
    """
    if symbols.language_for(path) is None:
        return False
    parts = path.split("/")
    if any(seg in TEST_SEGMENTS for seg in parts[:-1]):
        return False
    return any(path.startswith(p) for p in prefixes)


# --------------------------------------------------------------------------
# symbol extents
# --------------------------------------------------------------------------

def symbol_table(source: bytes, path: str = "x.py") -> list[dict]:
    """Every definition in ``source``, with BOTH line numbers kept separate.

    Delegates to ``graphify_ext.symbols`` — the SAME walker the product uses to
    slice code — so the benchmark's notion of "a symbol" cannot drift away from
    the thing being measured. ``path`` selects the grammar.

    ``def_line`` is the JOIN KEY to a graph node (graphify records the ``def``
    line). ``extent_start``..``end`` is the CONTAINMENT range. These were one
    field, and that defect fired: a changed ``@decorator`` line sits above
    ``def_line``, so it fell outside its own symbol. Measured on the frozen
    flask corpus: 13 hunk lines mis-attributed, and one decorated module-level
    function whose change vanished from the ground truth entirely.
    """
    syms = symbols.definitions_from_source(source, path)
    if syms is None:
        return []
    return [{"name": s.name, "kind": s.kind, "def_line": s.def_line,
             "extent_start": s.start, "end": s.end} for s in syms]


def enclosing(syms: list[dict], line: int) -> str | None:
    """Innermost symbol containing ``line``; None if the line is module-level.

    Innermost wins so a one-line change inside a method is attributed to the
    method, not to its class — attributing to the class would make the ground
    truth coarser than the fix actually was.
    """
    best: dict | None = None
    for s in syms:
        if s["extent_start"] <= line <= s["end"]:
            span = s["end"] - s["extent_start"]
            if best is None or span < (best["end"] - best["extent_start"]):
                best = s
    return best["name"] if best else None


# --------------------------------------------------------------------------
# diff -> changed lines on the OLD side
# --------------------------------------------------------------------------

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def changed_old_lines(repo: Path, commit: str, prefixes: tuple[str, ...]):
    """-> (modified_lines_by_file, insertion_anchors_by_file).

    Map file -> old-side line numbers the commit touched.

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
    inserts: dict[str, list[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("--- a/"):
            current = line[6:].strip()
            continue
        if line.startswith("--- "):
            current = None
            continue
        if line.startswith("@@") and current and is_pkg_source(current, prefixes):
            m = HUNK.match(line)
            if not m:
                continue
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count == 0:
                # `@@ -N,0` means "inserted AFTER old line N". Anchoring on N
                # alone attributed a pure insertion BETWEEN two symbols to
                # whichever symbol ended at N — a symbol the commit never
                # touched. Record the gap so the caller can require that both
                # sides fall inside one symbol before attributing it.
                inserts.setdefault(current, []).append(start)
            else:
                per_file.setdefault(current, []).extend(range(start, start + count))
    return per_file, inserts


def ground_truth(repo: Path, commit: str,
                 prefixes: tuple[str, ...]) -> tuple[dict[str, list[dict]], list[str]]:
    """(file -> changed symbols with extents, flat qualified list), resolved at the parent.

    Extents are carried through because graph nodes are matched to ground truth
    on ``(source_file, start line)``: graphify labels a method ``.json()`` with
    no class qualifier, so joining on names would be ambiguous, while its
    ``source_location`` is exactly tree-sitter's start line.
    """
    parent = git(repo, "rev-parse", f"{commit}^").strip()
    per_file, inserts = changed_old_lines(repo, commit, prefixes)
    by_file: dict[str, list[dict]] = {}
    flat: list[str] = []
    for path in sorted(set(per_file) | set(inserts)):
        try:
            src = git_bytes(repo, "show", f"{parent}:{path}")
        except subprocess.CalledProcessError:
            continue  # file did not exist at P (add), nothing to resolve against
        syms = symbol_table(src, path)
        hits: list[str] = []
        for ln in per_file.get(path, []):
            nm = enclosing(syms, ln)
            if nm and nm not in hits:
                hits.append(nm)
        for anchor in inserts.get(path, []):
            # Attribute a pure insertion only when BOTH sides of the insertion
            # point sit inside the same symbol. An insertion between two
            # definitions belongs to neither.
            before, after = enclosing(syms, anchor), enclosing(syms, anchor + 1)
            if before and before == after and before not in hits:
                hits.append(before)
        if hits:
            # Duplicate qualified names keep the entry whose extent actually
            # contains a changed line; a name-keyed dict silently kept the last.
            touched = set(per_file.get(path, [])) | set(inserts.get(path, []))
            chosen = []
            for n in hits:
                cands = [s for s in syms if s["name"] == n]
                best = next((c for c in cands
                             if any(c["extent_start"] <= l <= c["end"]
                                    for l in touched)), cands[0] if cands else None)
                if best:
                    chosen.append(dict(best))
            if chosen:
                by_file[path] = chosen
                flat.extend(f"{path}::{c['name']}" for c in chosen)
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


def pick_entry(message: str, gt_flat: list[str]) -> "str | None":
    """Symbol named in the commit message, preferring one that is in ``G``.

    Preferring a ``G`` member is not score-fitting: a real report names the
    function that misbehaved, and that function is by definition one the fix
    touched. What must never happen is picking ``E`` to make a tool look good —
    so the choice is a deterministic function of message word order alone.

    **Ambiguous leaf names are refused, not resolved.** If the message names
    ``__eq__`` and the fix touched both ``HTTPBasicAuth.__eq__`` and
    ``HTTPDigestAuth.__eq__``, the message does not determine an entry point.
    This previously collapsed into a dict keyed by leaf name, so the winner was
    decided by set-iteration order — an arbitrary choice wearing the appearance
    of a rule, and one that silently changed the corpus when unrelated code was
    refactored. Returning None drops the task instead.
    """
    gt_names = sorted({g.split("::", 1)[1] for g in gt_flat})
    gt_leaf: dict[str, list[str]] = {}
    for n in gt_names:
        gt_leaf.setdefault(n.split(".")[-1], []).append(n)

    for tok in IDENT.findall(message):
        if tok.lower() in STOPWORDS or len(tok) < 4:
            continue
        if tok in gt_names:
            return tok
        matches = gt_leaf.get(tok.split(".")[-1], [])
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None      # message is ambiguous; refuse rather than guess
    return None


# --------------------------------------------------------------------------

def followup_commits(repo: Path, commit: str, by_file: dict[str, list[dict]],
                     window: int = 400) -> list[str]:
    """Later commits that touch a symbol this fix touched — a completeness signal.

    Ground truth is "what the fix commit changed". If a LATER commit edits the
    same symbol, this fix may have been incomplete, and recall is then being
    scored against a target that was itself wrong. That does not make the task
    invalid — it makes it a task whose ceiling is unknown — so it is annotated
    rather than silently dropped, and the decision to exclude is left visible.
    """
    hits: list[str] = []
    for path, syms in by_file.items():
        try:
            log = git(repo, "log", f"-n{window}", "--format=%H",
                      f"{commit}..HEAD", "--", path)
        except subprocess.CalledProcessError:
            continue
        for later in log.split():
            try:
                mods, ins = changed_old_lines(repo, later, (path,))
                lines = mods.get(path, []) + ins.get(path, [])
            except subprocess.CalledProcessError:
                continue
            if not lines:
                continue
            for s in syms:
                if any(s["extent_start"] <= ln <= s["end"] for ln in lines):
                    if later not in hits:
                        hits.append(later)
                    break
    return hits


def build(repo: Path, prefixes: tuple[str, ...], repo_name: str,
          limit: int, scan: int, screen: int = 0) -> list[dict]:
    log = git(
        repo, "log", "--no-merges", f"-n{scan}", "--format=%H%x00%s%x00%b%x01",
        "--", *prefixes,
    )
    tasks: list[dict] = []
    skipped = {"no_gt": 0, "single_symbol": 0, "no_entry": 0,
               "revert": 0, "duplicate_entry": 0}
    # note: "no_entry" now also counts messages whose named symbol is ambiguous
    # across two or more ground-truth symbols (see pick_entry).
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
            by_file, flat = ground_truth(repo, sha, prefixes)
        except subprocess.CalledProcessError:
            continue
        if not flat:
            skipped["no_gt"] += 1
            continue
        if len(flat) < 2:
            skipped["single_symbol"] += 1
            continue

        parent = git(repo, "rev-parse", f"{sha}^").strip()
        entry = pick_entry(message, flat)
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
            "repo": repo_name,
            "commit": sha,
            "parent": parent,
            "subject": subject,
            "entry": entry,
            "ground_truth": flat,
            "discover": discover,
            "by_file": by_file,
        })
        if screen and len(tasks) <= screen:
            tasks[-1]["followups"] = followup_commits(repo, sha, by_file)
        if len(tasks) >= limit:
            break

    print(f"selected {len(tasks)} tasks; skipped {skipped}", file=sys.stderr)
    return tasks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(REPO))
    ap.add_argument("--name", default=None, help="corpus label for this repo")
    ap.add_argument("--prefix", action="append", default=[], metavar="PATH",
                    help="source-tree prefix to mine (repeatable). Required: an "
                         "unbounded scan pulls in vendored and generated code")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--scan", type=int, default=400)
    ap.add_argument("--screen", type=int, default=0,
                    help="run follow-up-commit completeness screening on the "
                         "first N selected tasks (slow: one git log per file)")
    ap.add_argument("--out", default=str(HERE / "tasks.json"))
    args = ap.parse_args()

    if not args.prefix:
        sys.exit("error: at least one --prefix is required")
    name = args.name or Path(args.repo).name
    tasks = build(Path(args.repo), tuple(args.prefix), name,
                  args.limit, args.scan, args.screen)
    Path(args.out).write_text(json.dumps(tasks, indent=2), encoding="utf-8")
    for t in tasks:
        fu = t.get("followups")
        mark = f"  [followups: {len(fu)}]" if fu else ""
        print(f"{t['repo']}/{t['commit'][:10]}  E={t['entry']:<30} "
              f"|G|={len(t['ground_truth'])} |D|={len(t['discover'])}"
              f"{mark}  {t['subject'][:44]}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
