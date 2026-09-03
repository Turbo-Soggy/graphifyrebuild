"""Resolve a graph node to its real source extent, signature, and body.

Why this module exists
----------------------
graphify records a symbol's position as ``source_location: "L<start>"`` and
nothing else (``graphify/extract.py`` writes ``f"L{node.start_point[0] + 1}"``).
There is no end line, no extent, no signature and no source text anywhere in
``graph.json``. So every answer the graph gives an agent is *name + file + line*,
and the agent must still open the file and work out where the symbol ends.

tree-sitter's ``end_point`` is available at extraction time and simply is not
recorded. This module recovers it by re-parsing the file with the same grammars
graphify already depends on, keyed off the start line the graph does record.

Honesty contract
----------------
If an extent cannot be resolved, ``resolve`` returns ``None`` and the caller
reports the symbol as unresolved. It never guesses a body by line arithmetic
(``next symbol's start - 1``): a wrong slice is worse for an agent than no slice,
because a wrong slice looks exactly as authoritative as a right one.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

# Definition node types per grammar. Kept explicit rather than "any node with a
# name field" so the set of things we claim to resolve is auditable.
_DEF_TYPES = {
    "python": ("function_definition", "class_definition"),
    "javascript": ("function_declaration", "class_declaration", "method_definition",
                   "generator_function_declaration"),
    "typescript": ("function_declaration", "class_declaration", "method_definition",
                   "generator_function_declaration", "interface_declaration",
                   "abstract_class_declaration"),
}

_EXT_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
}

# Wrappers that own a definition and carry the parts an agent needs to see —
# Python puts decorators in a `decorated_definition` PARENT, so slicing the
# function_definition alone silently drops `@property` and friends.
_WRAPPERS = ("decorated_definition", "export_statement")

_PARSERS: dict[str, object] = {}


def _parser(lang: str):
    if lang in _PARSERS:
        return _PARSERS[lang]
    from tree_sitter import Language, Parser
    if lang == "python":
        import tree_sitter_python as m
        raw = m.language()
    elif lang == "javascript":
        import tree_sitter_javascript as m
        raw = m.language()
    elif lang == "typescript":
        import tree_sitter_typescript as m
        raw = m.language_typescript()
    else:
        raise KeyError(lang)
    p = Parser(Language(raw))
    _PARSERS[lang] = p
    return p


def language_for(path: str) -> str | None:
    return _EXT_LANG.get(Path(str(path)).suffix.lower())


@dataclass
class Symbol:
    name: str
    kind: str
    file: str
    start: int          # first line of the definition, decorators included
    end: int            # last line of the definition
    def_line: int       # the line graphify records in source_location
    signature: str
    source: str

    def to_dict(self) -> dict:
        return asdict(self)


def _name_of(node) -> str:
    ident = node.child_by_field_name("name")
    if ident is not None:
        return ident.text.decode("utf-8", "replace")
    return node.type


def _outermost(node):
    """Climb through decorator/export wrappers so the slice includes them."""
    cur = node
    while cur.parent is not None and cur.parent.type in _WRAPPERS:
        cur = cur.parent
    return cur


@dataclass
class Unresolved:
    """Why an extent could not be recovered — a typed answer, never a bare None.

    Refusing to guess is only useful if the refusal is *legible*. A caller that
    receives ``None`` has to invent a reason, and an invented reason is itself a
    guess: reporting an unreadable file or a parser crash as "probably a
    docstring node" tells the agent something false with the same confidence as
    something true. ``code`` is stable and machine-readable; ``detail`` is for
    humans.
    """

    code: str
    detail: str
    file: str
    def_line: int

    def to_dict(self) -> dict:
        return asdict(self)


# Stable reason codes. Callers may branch on these; do not reword them silently.
UNSUPPORTED_LANGUAGE = "unsupported_language"
FILE_UNREADABLE = "file_unreadable"
PARSE_FAILED = "parse_failed"
NO_DEFINITION_AT_LINE = "no_definition_at_line"
BAD_SOURCE_LOCATION = "bad_source_location"
MISSING_SOURCE_FILE = "missing_source_file"


def resolve_detail(root: Path, rel_path: str, def_line: int) -> "Symbol | Unresolved":
    """The symbol defined at ``def_line`` of ``rel_path``, or a typed ``Unresolved``.

    ``def_line`` is 1-based and is matched against the definition keyword's own
    line — the same line graphify stores — not against the decorator line, so a
    node id taken straight from ``graph.json`` resolves without adjustment.
    """
    rel_path = str(rel_path)
    lang = language_for(rel_path)
    if lang is None:
        return Unresolved(
            UNSUPPORTED_LANGUAGE,
            f"no tree-sitter grammar wired for {Path(rel_path).suffix or 'this file type'}",
            rel_path, def_line)
    path = Path(root) / rel_path
    try:
        src = path.read_bytes()
    except OSError as exc:
        return Unresolved(FILE_UNREADABLE, f"{type(exc).__name__}: {exc}",
                          rel_path, def_line)

    try:
        tree = _parser(lang).parse(src)
    except Exception as exc:
        return Unresolved(PARSE_FAILED, f"{type(exc).__name__}: {exc}",
                          rel_path, def_line)

    wanted = _DEF_TYPES[lang]
    found = None

    def walk(node):
        nonlocal found
        if found is not None:
            return
        for child in node.children:
            if child.type in wanted and child.start_point[0] + 1 == def_line:
                found = child
                return
            walk(child)
            if found is not None:
                return

    walk(tree.root_node)
    if found is None:
        return Unresolved(
            NO_DEFINITION_AT_LINE,
            f"no {'/'.join(wanted)} begins at line {def_line} "
            "(graphify also emits doc/rationale nodes, which sit on lines that "
            "hold no definition)",
            rel_path, def_line)

    outer = _outermost(found)
    body = found.child_by_field_name("body")
    sig_end = body.start_byte if body is not None else found.end_byte
    signature = src[found.start_byte:sig_end].decode("utf-8", "replace").strip().rstrip(":{").strip()

    return Symbol(
        name=_name_of(found),
        kind="class" if "class" in found.type or "interface" in found.type else "function",
        file=str(rel_path),
        start=outer.start_point[0] + 1,
        end=outer.end_point[0] + 1,
        def_line=def_line,
        signature=signature,
        source=src[outer.start_byte:outer.end_byte].decode("utf-8", "replace"),
    )


def resolve(root: Path, rel_path: str, def_line: int) -> Symbol | None:
    """``resolve_detail`` narrowed to ``Symbol | None``.

    Kept for callers that only need "did it resolve". Anything that reports a
    failure to a human or an agent must use ``resolve_detail`` instead, so the
    reason it prints is the real one.
    """
    got = resolve_detail(root, rel_path, def_line)
    return got if isinstance(got, Symbol) else None


def resolve_node_detail(root: Path, node: dict) -> "Symbol | Unresolved":
    """Resolve a graph node dict straight from ``graph.json``."""
    src_file = node.get("source_file")
    loc = str(node.get("source_location") or "")
    if not src_file:
        return Unresolved(MISSING_SOURCE_FILE,
                          "graph node carries no source_file", "", 0)
    if not (loc.startswith("L") and loc[1:].isdigit()):
        return Unresolved(BAD_SOURCE_LOCATION,
                          f"source_location {loc!r} is not of the form 'L<int>'",
                          str(src_file), 0)
    return resolve_detail(root, str(src_file), int(loc[1:]))


def resolve_node(root: Path, node: dict) -> Symbol | None:
    got = resolve_node_detail(root, node)
    return got if isinstance(got, Symbol) else None
