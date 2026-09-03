"""Test-coverage edge producer (Requirement 2, case 5).

Emits ``tests`` findings (test node --tests--> production node) in the neutral
findings format consumed by edge_inject. Two producers, most-accurate first:

* ``from_coverage``: ingest coverage.py JSON written with dynamic contexts
  (``coverage json`` after running pytest with ``--cov-context=test``). Each
  covered line's contexts name the tests that executed it — ground truth, no
  heuristics.
* ``heuristic``: name matching for repos without coverage data — a test node
  ``test_<name>`` (or ``<name>_test``/``<Name>Test``-style labels) is linked
  to the unique production callable with bare name ``<name>``.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from graphify_ext import graphio

# Mirrors upstream paths._TEST_DIR_SEGMENTS / _TEST_FILENAME_PATTERNS
# (vendored: keep the classifier identical so "test node" means the same
# thing here as in stock resolution tie-breaking).
_TEST_DIR_SEGMENTS = frozenset({"tests", "test", "spec", "specs", "__tests__"})
_TEST_FILENAME_PATTERNS = (
    re.compile(r"^test_.*", re.IGNORECASE),
    re.compile(r".*_test\..+$", re.IGNORECASE),
    re.compile(r".*\.test\..+$", re.IGNORECASE),
    re.compile(r".*\.spec\..+$", re.IGNORECASE),
    re.compile(r".*_spec\..+$", re.IGNORECASE),
    re.compile(r".*\.tests\.ps1$", re.IGNORECASE),
    re.compile(r".*Test\.java$"),
    re.compile(r".*Tests\.java$"),
    re.compile(r".*Tests\.cs$"),
)


def is_test_path(path: str) -> bool:
    if not path:
        return False
    pure = PurePosixPath(str(path).replace("\\", "/"))
    if any(seg.lower() in _TEST_DIR_SEGMENTS for seg in pure.parts):
        return True
    return any(p.match(pure.name) for p in _TEST_FILENAME_PATTERNS)


def from_coverage(cov_json: dict) -> dict:
    """coverage.py JSON (with contexts) -> tests findings.

    Context ids look like ``tests/test_x.py::test_foo|run``; the ``file:line``
    they cover resolves (via edge_inject's containment lookup) to the enclosing
    production node at inject time. Only non-empty, non-test-file targets are
    emitted, deduped at (test, file) granularity plus one representative line
    per covered function region (dedup by line is left to inject's node-level
    dedupe — many lines of one function all resolve to the same node).
    """
    edges: list[dict] = []
    for file, info in (cov_json.get("files") or {}).items():
        if is_test_path(file):
            continue
        contexts = info.get("contexts") or {}
        for line_str, ctx_list in contexts.items():
            try:
                line = int(line_str)
            except ValueError:
                continue
            for ctx in ctx_list:
                test_id = str(ctx).split("|", 1)[0].strip()
                if not test_id:
                    continue  # empty context = import-time execution
                test_ref = _context_to_ref(test_id)
                if test_ref is None:
                    continue
                edges.append({
                    "relation": "tests",
                    "source_ref": test_ref,
                    "target_ref": {"file": file, "line": line},
                    # EXTRACTED: a coverage run observed this test execute this
                    # line. Distinct from heuristic()'s INFERRED name match --
                    # an agent trusting a name match as if it were measured
                    # execution is the dangerous direction of error here.
                    "confidence": "EXTRACTED",
                    "detail": f"coverage:{test_id}",
                })
    return {"edges": edges}


def _context_to_ref(test_id: str) -> dict | None:
    """``tests/test_x.py::TestCls::test_foo`` -> best-effort node ref."""
    if "::" in test_id:
        file, _, rest = test_id.partition("::")
        func = rest.split("::")[-1]
        # Parametrized ids: test_foo[case-1] -> test_foo
        func = func.split("[", 1)[0]
        if func:
            return {"node": func}
        return {"file": file}
    if test_id.endswith(".py") or "/" in test_id:
        return {"file": test_id}
    return {"node": test_id}


_TEST_NAME_RE = re.compile(r"^test[_ ]?(?P<name>\w+)$", re.IGNORECASE)


def heuristic(data: dict) -> dict:
    """Name-matching fallback: test_<name> -> unique production callable <name>.

    Conservative: an ambiguous name (multiple production candidates) emits
    nothing rather than guessing — a wrong ``tests`` edge would falsely tell
    the agent a regression is covered.
    """
    prod_by_bare: dict[str, list[str]] = {}
    test_nodes: list[tuple[str, str]] = []
    for n in graphio.nodes(data):
        nid = n.get("id")
        label = str(n.get("label", ""))
        if nid is None or not label:
            continue
        bare = graphio._bare(label)
        if is_test_path(str(n.get("source_file", ""))):
            m = _TEST_NAME_RE.match(bare)
            if m:
                test_nodes.append((str(nid), m.group("name").casefold()))
        else:
            prod_by_bare.setdefault(bare, []).append(str(nid))

    edges = []
    for test_id, target_name in test_nodes:
        candidates = prod_by_bare.get(target_name, [])
        if len(candidates) == 1:
            edges.append({
                "relation": "tests",
                "source_ref": {"node": test_id},
                "target_ref": {"node": candidates[0]},
                # INFERRED: names line up; nothing observed this test exercise
                # the target. Never upgrade this to EXTRACTED.
                "confidence": "INFERRED",
                "detail": "heuristic:name-match",
            })
    return {"edges": edges}
