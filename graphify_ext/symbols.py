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

# JS/TS bind most functions by assignment rather than declaration —
# `res.send = function () {}`, `const f = () => {}`, `{ handler() {} }`. Measured
# on expressjs/express `lib/response.js`: 9 declarations found by the declaration
# types alone, against 20 assigned functions forming the module's entire public
# API. graphify does not model these either (11 nodes for that file, none of them
# `res.send`), so they are invisible to the graph AND, until this was added, to
# the gap disclosure that exists to report exactly such absences.
_JS_FUNC_VALUES = ("function_expression", "arrow_function", "function",
                   "generator_function")
_JS_BINDERS = ("variable_declarator", "assignment_expression", "pair",
               "public_field_definition")

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
    try:
        src = (Path(root) / rel_path).read_bytes()
    except OSError as exc:
        return Unresolved(FILE_UNREADABLE, f"{type(exc).__name__}: {exc}",
                          rel_path, def_line)

    # Delegate to the single walker rather than re-implementing the traversal.
    # These were separate for a while and immediately diverged: the lister
    # learned to see JS functions bound by assignment (`res.send = function`)
    # and the resolver did not, so the pack could name a symbol it then refused
    # to slice.
    defs = definitions_from_source(src, rel_path, with_source=True)
    if defs is None:
        return Unresolved(PARSE_FAILED, "tree-sitter failed to parse the file",
                          rel_path, def_line)

    hits = [s for s in defs if s.def_line == def_line]
    if not hits:
        return Unresolved(
            NO_DEFINITION_AT_LINE,
            f"no definition begins at line {def_line} (graphify also emits "
            "doc/rationale nodes, which sit on lines that hold no definition)",
            rel_path, def_line)

    # Innermost-wins if two definitions share a binding line.
    sym = min(hits, key=lambda s: s.end - s.start)
    # `name` is the LEAF here, matching what the graph labels a symbol
    # (`.json()` -> json). The qualified form lives on definitions_from_source,
    # where the gap list needs `res.send` to be distinguishable from every other
    # `send` in the module.
    return Symbol(name=sym.name.split(".")[-1], kind=sym.kind, file=sym.file,
                  start=sym.start, end=sym.end, def_line=sym.def_line,
                  signature=sym.signature, source=sym.source)


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


def definitions_from_source(source: bytes, rel_path: str,
                            with_source: bool = False) -> list[Symbol] | None:
    """Every definition in ``source``, qualified by nesting. None if unparseable.

    Bytes-in rather than path-in so callers reading historical revisions (``git
    show <rev>:<path>``) use the SAME walker as callers reading the working tree.
    Two walkers for one job is how the definition of "a symbol" drifts between
    the benchmark's ground truth and the product's own output — and then the
    benchmark silently stops measuring the thing being built.
    """
    lang = language_for(rel_path)
    if lang is None:
        return None
    try:
        tree = _parser(lang).parse(source)
    except Exception:
        return None

    wanted = _DEF_TYPES[lang]
    out: list[Symbol] = []

    def _emit(node, qual: tuple[str, ...], kind: str, def_node) -> None:
        outer = _outermost(node)
        body = node.child_by_field_name("body")
        sig_end = body.start_byte if body is not None else node.end_byte
        out.append(Symbol(
            name=".".join(qual), kind=kind, file=str(rel_path),
            start=outer.start_point[0] + 1,
            end=outer.end_point[0] + 1,
            def_line=def_node.start_point[0] + 1,
            signature=source[def_node.start_byte:sig_end]
                .decode("utf-8", "replace").strip().rstrip(":{").strip()[:200],
            source=(source[outer.start_byte:outer.end_byte]
                    .decode("utf-8", "replace") if with_source else ""),
        ))

    def _bound_function(child):
        """(name, value_node) when this node binds a function to a name."""
        if lang == "python" or child.type not in _JS_BINDERS:
            return None
        val = child.child_by_field_name("value") or child.child_by_field_name("right")
        if val is None or val.type not in _JS_FUNC_VALUES:
            return None
        target = (child.child_by_field_name("name")
                  or child.child_by_field_name("left")
                  or child.child_by_field_name("key"))
        if target is None:
            return None
        # `res.send` keeps its receiver: the bare leaf would collide with every
        # other `send` in the module and make the name useless as an identifier.
        return target.text.decode("utf-8", "replace").strip(), val

    def walk(node, prefix: tuple[str, ...]) -> None:
        for child in node.children:
            if child.type in wanted:
                nm = child.child_by_field_name("name")
                if nm is None:
                    walk(child, prefix)
                    continue
                qual = (*prefix, nm.text.decode("utf-8", "replace"))
                _emit(child, qual,
                      "class" if "class" in child.type or "interface" in child.type
                      else "function", child)
                walk(child, qual)
                continue
            bound = _bound_function(child)
            if bound is not None:
                name, val = bound
                qual = (*prefix, name)
                # def_line is the BINDING line (`res.send = function ...`), which
                # is what a graph keyed on source_location would record.
                _emit(val, qual, "function", child)
                walk(val, qual)
                continue
            walk(child, prefix)

    walk(tree.root_node, ())
    return out


def definitions_in(root: Path, rel_path: str) -> list[Symbol] | None:
    """Every definition in a file, as Symbols. None if the file cannot be parsed.

    Used to find what the GRAPH does not contain. graphify never emits a node for
    a function nested inside another function (measured: 0 of 610 across 14
    checkouts of psf/requests — a design choice, not a defect), and id collisions
    drop others. An agent cannot be told "this is missing" by a graph that has no
    record of it, so the absence has to be recovered from source.
    """
    try:
        src = (Path(root) / rel_path).read_bytes()
    except Exception:
        return None
    return definitions_from_source(src, rel_path)


def resolve_node(root: Path, node: dict) -> Symbol | None:
    got = resolve_node_detail(root, node)
    return got if isinstance(got, Symbol) else None
