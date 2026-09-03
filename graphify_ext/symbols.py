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

Two line numbers, deliberately distinct
---------------------------------------
``def_line``  the line a graph keyed on ``source_location`` would record — the
              ``def``/``function`` keyword, or the line an assignment binds on.
              This is the JOIN KEY back to a graph node.
``start``     the first line of the whole construct: decorators included for
              Python, the ``res.send =`` binder included for JavaScript. This is
              the CONTAINMENT BOUND and the first line of ``source``.

They differ, and conflating them has produced two separate defects already: a
slice taken from ``def_line`` dropped ``@property``, and the benchmark used
``def_line`` as a containment bound so changed decorator lines fell outside
their own symbol. Any consumer must pick the one it means.

Honesty contract
----------------
If an extent cannot be resolved, callers get a typed ``Unresolved`` carrying the
reason that actually occurred. It never guesses a body by line arithmetic
(``next symbol's start - 1``): a wrong slice is worse for an agent than no slice,
because a wrong slice looks exactly as authoritative as a right one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

# Declaration node types per grammar. Kept explicit rather than "any node with a
# name field" so the set of things we claim to resolve is auditable.
_DEF_TYPES = {
    "python": ("function_definition", "class_definition"),
    "javascript": ("function_declaration", "class_declaration", "method_definition",
                   "generator_function_declaration"),
    "typescript": ("function_declaration", "class_declaration", "method_definition",
                   "generator_function_declaration", "interface_declaration",
                   "abstract_class_declaration"),
    # The grammars below are graphify's own hard dependencies, so they are
    # always installed wherever the graph was built. Each entry was verified
    # against the grammar's actual node types (see tests/test_symbols_langs.py);
    # a type not listed here is a type this module does not claim to slice.
    "go": ("function_declaration", "method_declaration", "type_spec"),
    "java": ("class_declaration", "interface_declaration", "enum_declaration",
             "record_declaration", "method_declaration", "constructor_declaration"),
    "rust": ("function_item", "function_signature_item", "struct_item", "enum_item",
             "trait_item", "union_item"),
    "ruby": ("class", "module", "method", "singleton_method"),
    "php": ("class_declaration", "interface_declaration", "trait_declaration",
            "enum_declaration", "function_definition", "method_declaration"),
    "kotlin": ("class_declaration", "object_declaration", "function_declaration"),
    "c_sharp": ("class_declaration", "interface_declaration", "struct_declaration",
                "record_declaration", "enum_declaration", "method_declaration",
                "constructor_declaration"),
}

# Node types that are a TYPE-LIKE container rather than a callable.
_TYPE_KINDS = ("class", "interface", "struct", "enum", "trait", "record", "module",
               "object", "type_spec", "union")

# Anonymous scopes whose children should be qualified by a name taken from a
# field: Rust's `impl Foo { fn m() }` has no name of its own but its methods
# are `Foo.m` to any reader, and Go/Java/C# have nothing comparable.
_SCOPE_TYPES = {
    "rust": {"impl_item": "type"},
}

_EXT_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".kt": "kotlin", ".kts": "kotlin",
    ".cs": "c_sharp",
}

# Wrappers that own a definition and carry the parts an agent needs to see —
# Python puts decorators in a `decorated_definition` PARENT, so slicing the
# function_definition alone silently drops `@property` and friends.
_WRAPPERS = ("decorated_definition", "export_statement")

# JS/TS bind most functions by assignment rather than declaration —
# `res.send = function () {}`, `const f = () => {}`, `{ handler() {} }`.
# Measured on expressjs/express `lib/response.js`: the declaration types alone
# found 9 symbols against 20 assigned functions forming the module's entire
# public API. graphify does not model these either, so they are invisible to the
# graph AND to the gap disclosure that exists to report such absences.
_JS_FUNC_VALUES = ("function_expression", "arrow_function", "function",
                   "generator_function")
_JS_CLASS_VALUES = ("class",)                      # `const C = class Inner {}`
# `field_definition` is JavaScript; `public_field_definition` is TypeScript.
# Listing only the TS spelling made identical .js and .ts files disagree.
_JS_BINDERS = ("variable_declarator", "assignment_expression", "pair",
               "field_definition", "public_field_definition")

# Statement nodes that own a binder. `res.send = function () {}` parses as an
# assignment_expression inside an expression_statement; the statement is what
# occupies lines 3..6, while the function value starts mid-line-3.
_BINDER_STATEMENTS = ("expression_statement", "lexical_declaration",
                      "variable_declaration", "export_statement")

_PARSERS: dict[str, object] = {}

# Parsed-file cache keyed by (path, mtime_ns, size). build_context resolves one
# node at a time and each resolve re-read and re-parsed the whole file: over a
# 20,843-node graph that took minutes, and the gap pass then parsed every file a
# second time. Invalidation is by stat, so an edited file is re-read.
_FILE_CACHE: dict[tuple[str, int, int], tuple] = {}
_CACHE_LIMIT = 256


def _read_and_parse(path: Path, rel_path: str, with_source: bool):
    """(symbols, error_code) for a file on disk, memoised on (mtime, size)."""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError as exc:
        return None, FILE_UNREADABLE, f"{type(exc).__name__}: {exc}"
    hit = _FILE_CACHE.get(key)
    if hit is not None and (hit[2] or not with_source):
        return hit[0], hit[1], None
    try:
        src = path.read_bytes()
    except OSError as exc:
        return None, FILE_UNREADABLE, f"{type(exc).__name__}: {exc}"
    syms, err = _definitions(src, rel_path, with_source)
    if len(_FILE_CACHE) >= _CACHE_LIMIT:
        _FILE_CACHE.clear()
    _FILE_CACHE[key] = (syms, err, with_source)
    return syms, err, None


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
    elif lang == "php":
        import tree_sitter_php as m
        raw = m.language_php()
    elif lang in ("go", "java", "rust", "ruby", "kotlin", "c_sharp"):
        m = __import__(f"tree_sitter_{lang}")
        raw = m.language()
    else:
        raise KeyError(lang)
    p = Parser(Language(raw))
    _PARSERS[lang] = p
    return p


def language_for(path: str) -> str | None:
    return _EXT_LANG.get(Path(str(path)).suffix.lower())


@dataclass
class Symbol:
    name: str           # qualified: "Class.method", "res.send", "outer.inner"
    kind: str
    file: str
    start: int          # first line of the construct (decorators/binder included)
    end: int            # last line of the construct
    def_line: int       # the line a graph records in source_location
    signature: str
    source: str
    # Leaf names of the calls made DIRECTLY in this body -- `foo(...)`,
    # `obj.foo(...)`, `new Foo(...)` -- excluding anything inside a nested
    # definition, which owns its own calls. Names only, never resolved here:
    # resolution needs the graph, and this module deliberately knows nothing
    # about it. Used by `supplement` to give a materialised definition the
    # call edges the extractor could not emit for a node it never created.
    calls: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Unresolved:
    """Why an extent could not be recovered — a typed answer, never a bare None.

    Refusing to guess is only useful if the refusal is *legible*. A caller that
    receives ``None`` has to invent a reason, and an invented reason is itself a
    guess. ``code`` is stable and machine-readable; ``detail`` must describe what
    was observed, never a likely-sounding cause.
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
AMBIGUOUS_DEFINITION = "ambiguous_definition"
# The one definition at the graph's line is not the symbol the graph named.
# This is what a STALE graph looks like from the inside: the file was edited
# after extraction, lines shifted, and the node's `source_location` now points
# at a different function. Returning that function's body under the requested
# name is the worst outcome this module can produce -- it is exactly as
# authoritative-looking as the right answer -- so it is refused by name.
DEFINITION_MISMATCH = "definition_mismatch"


def _outermost(node):
    """Climb through decorator/export wrappers so the slice includes them."""
    cur = node
    while cur.parent is not None and cur.parent.type in _WRAPPERS:
        cur = cur.parent
    return cur


def _binder_statement(node):
    """Climb an assignment to the statement that owns it (see _BINDER_STATEMENTS)."""
    cur = node
    while cur.parent is not None and cur.parent.type in _BINDER_STATEMENTS:
        cur = cur.parent
    return cur


def _clean_key(text: str) -> str | None:
    """Normalise a binding target, or None if it is not a usable name.

    Quoted keys keep their quotes in the raw text and computed keys are
    expressions, which produced names like ``'"my key"'`` and ``[dyn]``. A name
    that cannot be typed by a human is not a name, and emitting one puts a
    symbol in the graph-gap list that nobody can look up.
    """
    text = text.strip()
    if not text:
        return None
    if len(text) >= 2 and text[0] in "\"'`" and text[-1] == text[0]:
        text = text[1:-1].strip()
    if not text or text.startswith("[") or text[0].isdigit():
        return None                       # computed or numeric key
    # A space survives: {'my key': function(){}} is not an identifier,
    # but it is a name a reader can locate, and the gap list exists to say
    # "something is here". Only unreadable names are dropped.
    if any(c in text for c in "\t\n(){}"):
        return None
    return text


_CALL_TYPES = {
    "python": ("call",),
    "javascript": ("call_expression", "new_expression"),
    "typescript": ("call_expression", "new_expression"),
    "go": ("call_expression",),
    "java": ("method_invocation", "object_creation_expression"),
    "rust": ("call_expression",),
    "ruby": ("call",),
    "php": ("function_call_expression", "member_call_expression",
            "scoped_call_expression", "object_creation_expression"),
    "kotlin": ("call_expression",),
    "c_sharp": ("invocation_expression", "object_creation_expression"),
}

# Fields on a call node that hold the callee, in the order to try them.
_CALLEE_FIELDS = ("function", "method", "name", "constructor", "type")
# Member-access chains: descend to the field that names the LEAF.
_CHAIN_LEAF_FIELD = {
    "attribute": "attribute",              # python a.b
    "member_expression": "property",       # js a.b
    "selector_expression": "field",        # go a.b
    "field_expression": "field",           # rust a.b
    "scoped_identifier": "name",           # rust A::b
    "member_access_expression": "name",    # c# a.b
    "scoped_type_identifier": "name",      # rust A::B
    "generic_type": "type",                # java/c# Foo<T>
}
_LEAF_TYPES = ("identifier", "property_identifier", "field_identifier", "name",
               "simple_identifier", "type_identifier", "constant")


def _callee_leaf(call_node, lang: str) -> str | None:
    """Leaf identifier a call binds to: ``foo`` in ``a.b.foo(x)`` / ``foo(x)``.

    Only plain identifiers and member chains are accepted. A call on a
    computed expression (``fns[i]()``, ``(await x)()``) has no name and is
    dropped rather than guessed.
    """
    fn = None
    for field in _CALLEE_FIELDS:
        fn = call_node.child_by_field_name(field)
        if fn is not None:
            break
    if fn is None and lang == "kotlin":
        # kotlin's call_expression has no callee field: the callee is the
        # first named child, an identifier or a navigation chain `a.b.c`.
        for c in call_node.children:
            if c.is_named:
                fn = c
                break
        while fn is not None and fn.type == "navigation_expression":
            named = [c for c in fn.children if c.is_named]
            fn = named[-1] if named else None
        if fn is not None and fn.type == "navigation_suffix":
            named = [c for c in fn.children if c.is_named]
            fn = named[-1] if named else None
    if fn is None:
        return None
    for _ in range(8):
        nxt_field = _CHAIN_LEAF_FIELD.get(fn.type)
        if nxt_field is None:
            break
        nxt = fn.child_by_field_name(nxt_field)
        if nxt is None:
            return None
        fn = nxt
    if fn.type not in _LEAF_TYPES:
        return None
    text = fn.text.decode("utf-8", "replace").strip()
    return text or None


def _direct_calls(body, lang: str, def_types) -> tuple[str, ...]:
    """Ordered, de-duplicated callee leaf names directly inside ``body``.

    Descent stops at any nested definition or function-valued binder: those
    calls belong to the inner symbol, and attributing them to the outer one
    would invent a `calls` edge from a function that never makes the call.
    """
    call_types = _CALL_TYPES.get(lang, ())
    seen: dict[str, None] = {}
    stack = list(body.children)
    while stack:
        node = stack.pop()
        if node.type in def_types or node.type in (*_JS_FUNC_VALUES,
                                                    *_JS_CLASS_VALUES):
            continue
        if node.type in call_types:
            leaf = _callee_leaf(node, lang)
            if leaf:
                seen.setdefault(leaf, None)
        stack.extend(node.children)
    return tuple(seen)


def _definitions(source: bytes, rel_path: str, with_source: bool):
    """(symbols, error_code). Exactly one of the two is meaningful."""
    lang = language_for(rel_path)
    if lang is None:
        return None, UNSUPPORTED_LANGUAGE
    try:
        tree = _parser(lang).parse(source)
    except Exception:
        return None, PARSE_FAILED
    # tree-sitter does NOT raise on malformed input — it returns a tree with
    # ERROR nodes. Without this check, every binary or garbage file fell through
    # to "no definition begins at line N", a message that asserts a cause.
    had_error = tree.root_node.has_error

    wanted = _DEF_TYPES[lang]
    out: list[Symbol] = []

    def emit(construct, value_node, qual, kind, def_node) -> None:
        body = value_node.child_by_field_name("body")
        if body is None:
            # Grammars without a `body` field (ruby, kotlin): the first block-
            # like child is the body; the signature is what precedes it.
            for c in value_node.children:
                if c.type in ("body_statement", "function_body", "class_body",
                              "block", "declaration_list", "compound_statement",
                              "enum_body", "interface_body", "field_declaration_list"):
                    body = c
                    break
        sig_start = def_node.start_byte
        sig_end = body.start_byte if body is not None else value_node.end_byte
        out.append(Symbol(
            name=".".join(qual), kind=kind, file=str(rel_path),
            start=construct.start_point[0] + 1,
            end=construct.end_point[0] + 1,
            def_line=def_node.start_point[0] + 1,
            signature=source[sig_start:max(sig_start, sig_end)]
                .decode("utf-8", "replace").strip().rstrip(":{").strip()[:200],
            source=(source[construct.start_byte:construct.end_byte]
                    .decode("utf-8", "replace") if with_source else ""),
            calls=_direct_calls(body if body is not None else value_node,
                                lang, wanted),
        ))

    def bound_targets(node):
        """[(name, value_node)] when a node binds a function/class to a name.

        Handles the chain ``x = y = function(){}``, which binds both names; a
        single-target lookup silently lost ``x``.
        """
        if lang == "python" or node.type not in _JS_BINDERS:
            return []
        val = node.child_by_field_name("value") or node.child_by_field_name("right")
        names: list[str] = []
        while val is not None and val.type == "assignment_expression":
            inner = val.child_by_field_name("left")
            if inner is not None:
                nm = _clean_key(inner.text.decode("utf-8", "replace"))
                if nm:
                    names.append(nm)
            val = val.child_by_field_name("right")
        if val is None or val.type not in (*_JS_FUNC_VALUES, *_JS_CLASS_VALUES):
            return []
        target = (node.child_by_field_name("name")
                  or node.child_by_field_name("left")
                  or node.child_by_field_name("key")
                  or node.child_by_field_name("property"))
        if target is not None:
            nm = _clean_key(target.text.decode("utf-8", "replace"))
            if nm:
                names.insert(0, nm)
        return [(n, val) for n in names]

    def object_binding(node):
        """(name, object_node) when a node binds an OBJECT LITERAL to a name.

        Members of `const obj = { handler: function(){} }` are reported as
        `obj.handler` rather than a bare `handler`: the leaf alone collides with
        every other `handler` in the module, and this is the shape
        `module.exports = { ... }` takes throughout Node code.
        """
        if lang == "python" or node.type not in _JS_BINDERS:
            return None
        val = node.child_by_field_name("value") or node.child_by_field_name("right")
        if val is None or val.type != "object":
            return None
        target = (node.child_by_field_name("name")
                  or node.child_by_field_name("left")
                  or node.child_by_field_name("key"))
        if target is None:
            return None
        nm = _clean_key(target.text.decode("utf-8", "replace"))
        return (nm, val) if nm else None

    scopes = _SCOPE_TYPES.get(lang, {})

    def walk(node, prefix: tuple[str, ...]) -> None:
        for child in node.children:
            if child.type in scopes:
                # `impl Foo { ... }`: no symbol of its own, but its members are
                # Foo's. Qualify, do not emit.
                tnode = child.child_by_field_name(scopes[child.type])
                tname = tnode.text.decode("utf-8", "replace") if tnode is not None else ""
                for _ in range(4):        # strip generics: `Foo<T>` -> `Foo`
                    tnode2 = tnode.child_by_field_name("type") if tnode is not None else None
                    if tnode2 is None:
                        break
                    tnode = tnode2
                    tname = tnode.text.decode("utf-8", "replace")
                tname = tname.split("<", 1)[0].strip()
                walk(child, (*prefix, tname) if tname else prefix)
                continue
            if child.type in wanted:
                nm = child.child_by_field_name("name")
                if nm is None:
                    walk(child, prefix)
                    continue
                qual = (*prefix, nm.text.decode("utf-8", "replace"))
                kind = ("class" if any(k in child.type for k in _TYPE_KINDS)
                        else "function")
                emit(_outermost(child), child, qual, kind, child)
                walk(child, qual)
                continue

            obj = object_binding(child)
            if obj is not None:
                walk(obj[1], (*prefix, obj[0]))
                continue

            bound = bound_targets(child)
            if bound:
                construct = _binder_statement(child)
                for name, val in bound:
                    qual = (*prefix, name)
                    kind = "class" if val.type in _JS_CLASS_VALUES else "function"
                    emit(construct, val, qual, kind, child)
                    walk(val, qual)
                continue

            walk(child, prefix)

    walk(tree.root_node, ())
    # Errors alone are not fatal — real files often parse with recoverable
    # errors and still yield every definition. Errors AND nothing found is the
    # signal that the parse, not the file's contents, is why there is no answer.
    if had_error and not out:
        return None, PARSE_FAILED
    return out, None


def definitions_from_source(source: bytes, rel_path: str,
                            with_source: bool = False) -> list[Symbol] | None:
    """Every definition in ``source``, qualified by nesting. None if unusable.

    Bytes-in rather than path-in so callers reading historical revisions (``git
    show <rev>:<path>``) use the SAME walker as callers reading the working tree.
    Two walkers for one job is how the definition of "a symbol" drifts between
    the benchmark's ground truth and the product's own output.
    """
    syms, _ = _definitions(source, rel_path, with_source)
    return syms


def is_nested_in_function(name: str, all_names: dict,
                          def_line: int | None = None,
                          extents: dict | None = None) -> bool:
    """True when ``name`` is defined INSIDE the body of a function.

    ``name.count(".") >= 1`` is NOT this test: ``Class.method`` is dotted and not
    nested, and JavaScript's ``res.send`` is dotted and not nested either. Using
    the dot count told an agent that a missing method was "nested inside another
    definition — graphify emits no node for these", which is false; graphify
    emits method nodes.

    Nor is "an ancestor segment is a function" the test, which is what this
    checked before. ``proto.param = function param() {}`` where ``proto`` is
    itself a function (``var proto = module.exports = function (options) {}``)
    is a property assigned ONTO a function object at module level, not a
    closure inside its body -- and graphify's omission applies only to the
    latter. Measured: express ``lib/router/index.js`` binds its entire router
    API this way, and the old test declined to materialise any of it.

    So when ``def_line`` and ``extents`` (``name -> (start, end)``) are given,
    an ancestor counts only if its extent actually contains the definition.
    Without extents the kind-only test is kept for callers that have nothing
    better, and it errs towards "nested" (the disclosed direction).
    """
    parts = name.split(".")
    for i in range(1, len(parts)):
        anc = ".".join(parts[:i])
        kind = all_names.get(anc)
        if kind != "function":
            continue
        if def_line is None or extents is None or anc not in extents:
            return True
        lo, hi = extents[anc]
        if lo < def_line <= hi:
            return True
    return False


def resolve_detail(root: Path, rel_path: str, def_line: int,
                   expect: str | None = None) -> "Symbol | Unresolved":
    """The symbol whose ``def_line`` is ``def_line``, or a typed ``Unresolved``.

    ``expect`` is the graph node's label when the caller has one. Two definitions
    can share a binding line (``const f = () => 1, g = () => 2``); without a name
    to check against, one was returned silently and the agent received a
    different function's body under the name it asked for.
    """
    rel_path = str(rel_path)
    defs, err, io_detail = _read_and_parse(Path(root) / rel_path, rel_path, True)
    if io_detail is not None:
        return Unresolved(FILE_UNREADABLE, io_detail, rel_path, def_line)
    if defs is None:
        detail = ("no tree-sitter grammar wired for "
                  f"{Path(rel_path).suffix or 'this file type'}"
                  if err == UNSUPPORTED_LANGUAGE
                  else "tree-sitter could not parse this file")
        return Unresolved(err, detail, rel_path, def_line)

    hits = [s for s in defs if s.def_line == def_line]
    if not hits:
        return Unresolved(
            NO_DEFINITION_AT_LINE,
            f"the file parsed, and no definition begins at line {def_line}",
            rel_path, def_line)

    if len(hits) > 1:
        leaf = str(expect or "").strip().lstrip(".").rstrip("()")
        named = [s for s in hits if s.name.split(".")[-1] == leaf] if leaf else []
        if len(named) == 1:
            hits = named
        else:
            return Unresolved(
                AMBIGUOUS_DEFINITION,
                f"{len(hits)} definitions begin at line {def_line} "
                f"({', '.join(s.name for s in hits)})"
                + (f" and none is uniquely named {leaf!r}" if leaf
                   else "; no label was supplied to choose between them"),
                rel_path, def_line)

    sym = hits[0]
    leaf = str(expect or "").strip().lstrip(".").rstrip("()")
    if leaf and sym.name.split(".")[-1] != leaf and sym.name != leaf:
        return Unresolved(
            DEFINITION_MISMATCH,
            f"line {def_line} defines {sym.name!r}, not {leaf!r} -- the graph "
            f"is probably stale for this file (re-run graphify update)",
            rel_path, def_line)
    # `name` is the LEAF here, matching what the graph labels a symbol
    # (`.json()` -> json). The qualified form lives on definitions_from_source,
    # where the gap list needs `res.send` distinguishable from every other
    # `send` in the module.
    return Symbol(name=sym.name.split(".")[-1], kind=sym.kind, file=sym.file,
                  start=sym.start, end=sym.end, def_line=sym.def_line,
                  signature=sym.signature, source=sym.source, calls=sym.calls)


def resolve(root: Path, rel_path: str, def_line: int,
            expect: str | None = None) -> Symbol | None:
    """``resolve_detail`` narrowed to ``Symbol | None``.

    Kept for callers that only need "did it resolve". Anything that reports a
    failure to a human or an agent must use ``resolve_detail`` instead, so the
    reason it prints is the real one.
    """
    got = resolve_detail(root, rel_path, def_line, expect)
    return got if isinstance(got, Symbol) else None


def definitions_in(root: Path, rel_path: str) -> list[Symbol] | None:
    """Every definition in a file on disk. None if it cannot be read or parsed.

    Used to find what the GRAPH does not contain: graphify emits no node for a
    function nested inside another function (measured: 0 of 610 across 14
    checkouts of psf/requests), and id collisions drop others. An agent cannot
    be told "this is missing" by a graph that has no record of it.
    """
    defs, _err, io_detail = _read_and_parse(Path(root) / rel_path, rel_path, False)
    return None if io_detail is not None else defs


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
    return resolve_detail(root, str(src_file), int(loc[1:]),
                          expect=str(node.get("label") or "") or None)


def resolve_node(root: Path, node: dict) -> Symbol | None:
    got = resolve_node_detail(root, node)
    return got if isinstance(got, Symbol) else None
