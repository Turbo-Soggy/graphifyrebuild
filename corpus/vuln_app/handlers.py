"""Handlers with KNOWN taint outcomes.

Each function's docstring states its ground-truth classification. The machine
-readable version is ../ground_truth.json; these docstrings exist so a human
reviewing a failure can see the intent without cross-referencing.

Naming is deliberate: `tp_*` = true positive (a real source->sink flow),
`tn_*` = true negative (must NOT be reported as taint-exposed).
"""

from .sinks import render_html, run_shell, run_sql, sanitize
from .sources import read_config_constant, read_header, read_query_param


def tp_direct_sqli(request, conn):
    """TRUE POSITIVE: query param flows straight into a SQL sink, one hop."""
    raw = read_query_param(request)
    return run_sql(conn, "SELECT * FROM users WHERE name = '" + raw + "'")


def _exec_path(user_value):
    """Intermediate hop that OWNS the sink — carries taint without neutralising it.

    Deliberately a separate function from the one holding the source, so the
    corpus tests a flow whose two ends resolve to *different* nodes. A flow
    whose source and sink share one function cannot distinguish correct
    cross-function mapping from a lucky single-node match.
    """
    return run_shell("ls " + user_value)


def tp_multihop_shell(request):
    """TRUE POSITIVE: header (here) -> _exec_path -> shell sink (there).

    Source and sink live in different functions, so both ends must map to
    their own enclosing node.
    """
    value = read_header(request, "X-Path")
    return _exec_path(value)


def tp_reflected_xss(request):
    """TRUE POSITIVE: query param rendered into HTML without escaping."""
    raw = read_query_param(request)
    return render_html(raw)


def tn_sanitized_sql(request, conn):
    """TRUE NEGATIVE: the value is sanitised before reaching the sink.

    A sink is present and untrusted input is present, so a purely structural
    "does this function call a sink" check would flag it. Only real flow
    analysis clears it — which is why it belongs in the corpus.
    """
    raw = read_query_param(request)
    safe = sanitize(raw)
    return run_sql(conn, f"SELECT * FROM users WHERE name = '{safe}'")


def tn_constant_sql(conn):
    """TRUE NEGATIVE: reaches a SQL sink, but the value is a trusted constant.

    Distinguishes "reaches a sink" from "reachable from untrusted input".
    """
    return run_sql(conn, read_config_constant())


def tn_unreached_sink(conn, statement):
    """TRUE NEGATIVE: contains a sink but nothing untrusted ever calls it.

    Never invoked from any handler in this corpus, so it must not appear in
    the taint-exposed subset even though it is one hop from a sink.
    """
    return run_sql(conn, statement)


def tn_pure_helper(a, b):
    """TRUE NEGATIVE: no source, no sink. Control for over-reporting."""
    return a + b
