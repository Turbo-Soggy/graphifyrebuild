"""Plain-JSON graph.json access shared by the fix-context tooling.

Deliberately dependency-free (no networkx, no graphifyy import): every
Requirement-2 command operates on graph.json as data, so it works against any
graph regardless of which environment produced it.

graph.json vocabulary (verified against graphify v8 source):
  nodes: {id, label, source_file, source_location ("L<n>"), file_type, type, ...}
  edges: under "edges" (raw writer) or "links" (clustered/networkx writer):
         {source, target, relation, confidence (EXTRACTED|INFERRED|AMBIGUOUS),
          source_file?, source_location?}
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path


def read_json(path: Path):
    """Read a JSON file tolerating a UTF-8 BOM (PowerShell 5.1's Out-File and
    Set-Content default to BOM'd UTF-8 on Windows; upstream graphify reads its
    own markers with utf-8-sig for the same reason)."""
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def load(path: Path) -> dict:
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a graph.json object")
    return data


def repo_root_for(graph_path: "str | Path") -> Path:
    """Repo root for a graph.json — deliberately WITHOUT following symlinks.

    graph.json lives at ``<repo>/graphify-out/graph.json`` and ``source_file``
    values are repo-relative, so the root is two levels up. It must be derived
    with ``os.path.abspath`` (normalises, follows nothing) and never with
    ``Path.resolve()``: the per-branch cache replaces ``graphify-out`` with a
    symlink into ``.graphify-cache/<branch>/``, so ``resolve()`` returns
    ``<repo>/.graphify-cache`` as the "root". No source file exists under that
    path, so every file-reading guard in :func:`resolve_by_location` silently
    goes inert — in exactly the deployment those guards were added for.
    Measured on the connected repo before this helper existed.
    """
    return Path(os.path.abspath(str(graph_path))).parent.parent


def edges_key(data: dict) -> str:
    """The key edges actually live under in THIS file ('links' or 'edges')."""
    if "links" in data:
        return "links"
    if "edges" in data:
        return "edges"
    data["edges"] = []
    return "edges"


def edges(data: dict) -> list[dict]:
    return data[edges_key(data)]


def nodes(data: dict) -> list[dict]:
    return data.setdefault("nodes", [])


def is_file_node(node: dict) -> bool:
    """True for the node that stands for a whole file.

    graphify labels a file node with its basename -- EXCEPT index-style files,
    which get the parent directory too (``router/index.js`` for
    ``lib/router/index.js``), so that a repo with twenty ``index.js`` files has
    twenty distinguishable labels. Matching on the bare basename alone missed
    every one of those, and with it every definition the supplement should
    have materialised in them (express ``lib/router/index.js``: 0 of 12).
    """
    f = node.get("source_file")
    if not f or str(node.get("source_location") or "") != "L1":
        return False
    if node.get("_callable") or str(node.get("label", "")).endswith("()"):
        return False
    label = str(node.get("label", "")).replace("\\", "/")
    path = str(f).replace("\\", "/")
    return label == Path(path).name or (bool(label) and path.endswith("/" + label)) or label == path


def node_index(data: dict) -> dict[str, dict]:
    return {str(n.get("id")): n for n in nodes(data) if n.get("id") is not None}


def save_atomic(path: Path, data: dict) -> None:
    """Atomic write that follows symlinks (mirrors upstream paths._atomic_replace
    semantics so a linked graphify-out keeps working)."""
    real = Path(os.path.realpath(str(path)))
    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(real.parent), prefix=f".{real.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, str(real))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------- seed lookup

def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def _bare(label: str) -> str:
    label = _norm(label)
    return label[:-2] if label.endswith("()") else label


def _leaf(label: str) -> str:
    """The name a person types: no callable parens, no member dot.

    graphify labels a method ``.parse()``; upstream's ladder compares the bare
    form ``.parse`` against the query ``parse`` and never matches, so a method
    could only ever be reached by id or by substring -- and the substring step
    then reported ``parse`` as ambiguous against ``parser_count_tokens``.
    """
    return _bare(label).lstrip(".")


# `path/to/file.py:123` or `path/to/file.py:L123` -- the shape a stack trace,
# a SAST finding or a failing test names a location in.
_FILE_LINE_RE = re.compile(r"^(?P<file>[^:\n]+?\.[A-Za-z0-9_]+):L?(?P<line>\d+)$")


def resolve_node(data: dict, query: str, root: "Path | None" = None) -> str | None:
    """Resolve a user query (id, label, bare name, source path, or ``file:line``)
    to one node id.

    Port of upstream affected.resolve_seed's resolution ladder (exact id →
    exact label → bare callable name → source_file → unique substring), kept
    behaviorally aligned so ext commands and stock commands agree on what a
    name means. Two additions the stock ladder lacks:

    * ``file:line`` resolves through :func:`resolve_by_location` -- a bug
      report names a location far more often than it names a node id.
    * ``qualified_name`` is matched exactly after the label. Only supplement
      nodes carry it (``res.json``, ``Widget.build``); it lets a caller ask for
      a member by the name the source uses rather than by its bare leaf, which
      is what disambiguates twenty ``send`` leaves across one Node codebase.

    Returns ``None`` when nothing matches OR when several do; use
    :func:`candidates` to see which, rather than guessing.
    """
    idx = node_index(data)
    if query in idx:
        return query
    m = _FILE_LINE_RE.match(query.strip())
    if m:
        return resolve_by_location(data, m.group("file"), int(m.group("line")),
                                   root=root)
    q = _norm(query.rstrip("/\\") or query)

    exact = [i for i, n in idx.items() if _norm(str(n.get("label", ""))) == q]
    if len(exact) == 1:
        return exact[0]

    qual = [i for i, n in idx.items()
            if n.get("qualified_name") and _norm(str(n["qualified_name"])) == q]
    if len(qual) == 1:
        return qual[0]

    qb = _bare(q)
    bare = [i for i, n in idx.items() if _bare(str(n.get("label", ""))) == qb]
    if len(bare) == 1:
        return bare[0]

    ql = _leaf(q)
    leaf = [i for i, n in idx.items() if _leaf(str(n.get("label", ""))) == ql]
    if len(leaf) == 1:
        return leaf[0]

    qpath = _norm(Path(query).as_posix())
    by_file = [i for i, n in idx.items() if _norm(str(n.get("source_file", ""))) == qpath]
    if len(by_file) == 1:
        return by_file[0]
    if by_file:
        l1 = [i for i in by_file if str(idx[i].get("source_location", "")) == "L1"]
        if len(l1) == 1:
            return l1[0]

    contains = [i for i, n in idx.items() if q in _norm(str(n.get("label", "")))]
    if len(contains) == 1:
        return contains[0]
    return None


def candidates(data: dict, query: str, limit: int = 25) -> list[dict]:
    """Every node a query could mean, best match first.

    :func:`resolve_node` answers "the one node" or nothing; an agent whose seed
    came back as nothing needs to see WHY -- twenty ``send`` methods, or zero.
    Ranking: exact label/qualified-name match, then bare-name match, then
    substring on label / qualified name / id / source path. Callables before
    non-callables at equal rank, then a stable id order. Each row carries the
    fields needed to pick one: id, label, qualified_name, file, line, whether
    it is callable, and its origin (extractor vs supplement).
    """
    q = _norm(str(query).strip())
    qb = _bare(q)
    ql = _leaf(q)
    if not q:
        return []
    rows: list[tuple[tuple, dict]] = []
    for nid, n in node_index(data).items():
        label = _norm(str(n.get("label", "")))
        qual = _norm(str(n.get("qualified_name") or ""))
        path = _norm(str(n.get("source_file") or ""))
        # "alpha", "alpha()" and ".alpha()" are the same name; the parens and
        # the dot are graphify's callable/member markers, not part of what a
        # person types.
        if (label == q or _bare(label) == qb or _leaf(label) == ql
                or (qual and qual == q) or nid == query):
            rank = 0
        elif qual and qual.split(".")[-1] == ql:
            rank = 1
        elif q in label or (qual and q in qual):
            rank = 2
        elif q in _norm(nid) or q in path:
            rank = 3          # id/path substrings are the loosest signal
        else:
            continue
        is_callable = bool(n.get("_callable")) or label.endswith("()")
        rows.append(((rank, 0 if is_callable else 1, nid), {
            "id": nid,
            "label": n.get("label"),
            "qualified_name": n.get("qualified_name"),
            "file": n.get("source_file"),
            "location": n.get("source_location"),
            "callable": is_callable,
            "origin": n.get("origin") or n.get("_origin") or "ast",
            "match": ("exact", "bare-name", "substring", "id-or-path")[rank],
        }))
    rows.sort(key=lambda r: r[0])
    return [r for _, r in rows[:limit]]


_LOC_RE = re.compile(r"L(\d+)")


def node_line(n: dict) -> int | None:
    m = _LOC_RE.search(str(n.get("source_location", "")))
    return int(m.group(1)) if m else None


def resolve_by_location(data: dict, file: str, line: int,
                        root: "Path | None" = None) -> str | None:
    """Node enclosing ``file:line`` — the nearest preceding CALLABLE definition.

    graph.json stores no function extents, so containment has to be inferred.
    Three guards make that inference honest, in priority order:

    1. **Length bound.** A line past the end of the file resolves to nothing,
       rather than to the last definition in it.
    2. **Exact definition line.** A finding ON a ``def``/``class`` line belongs
       to that node, even though the line itself is unindented.
    3. **Top-level guard.** A non-blank line starting at column 0 is not inside
       any function body, so it resolves to the FILE node instead of to the
       preceding function. Without this, a module-level finding — a hardcoded
       secret, a taint source in config code — is attributed to whichever
       function happens to sit above it, which is confidently wrong rather
       than merely imprecise. Measured: 2 of 4 boundary cases mis-attributed
       before this guard.

    Otherwise: nearest preceding callable. The callable preference is the
    primary rule, not a tie-break — graphify emits a docstring node one line
    BELOW each function's own node, so "nearest preceding node of any kind"
    resolves every in-function location to prose.

    All three guards need ``root`` to read the file; without it only the
    nearest-preceding-callable rule applies, so callers that can supply a root
    should.
    """
    fnorm = _norm(Path(file).as_posix())

    lines: "list[str] | None" = None
    if root is not None:
        try:
            target = Path(root) / file
            if target.is_file():
                lines = target.read_text(encoding="utf-8",
                                         errors="replace").splitlines()
        except OSError:
            lines = None

    # Guard 1: line past end of file.
    if lines is not None and line > len(lines):
        return None

    callables: list[tuple[int, str]] = []
    others: list[tuple[int, str]] = []
    file_node: "str | None" = None
    basename = _norm(Path(file).name)
    for n in nodes(data):
        nid = n.get("id")
        if nid is None:
            continue
        if _norm(str(n.get("source_file", ""))) != fnorm:
            continue
        ln = node_line(n)
        if ln is None:
            continue
        is_callable = bool(n.get("_callable")) or str(n.get("label", "")).endswith("()")
        if ln == 1 and not is_callable and _norm(str(n.get("label", ""))) == basename:
            file_node = str(nid)
        if ln > line:
            continue
        (callables if is_callable else others).append((ln, str(nid)))

    # Guard 2: the definition line itself belongs to its own node.
    for ln, nid in callables:
        if ln == line:
            return nid

    # Guard 3: an unindented, non-blank line is not inside a function body.
    if lines is not None and 1 <= line <= len(lines):
        raw = lines[line - 1]
        if raw.strip() and not raw[:1].isspace():
            return file_node

    pool = callables or others
    if not pool:
        return None
    pool.sort(key=lambda t: t[0])
    return pool[-1][1]
