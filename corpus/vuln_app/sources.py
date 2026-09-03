"""Untrusted-input sources for the taint-validation corpus.

Every function here returns attacker-controlled data. Ground truth for the
whole corpus lives in ../ground_truth.json.
"""


def read_query_param(request):
    """SOURCE: HTTP query parameter — fully attacker controlled."""
    return request.args.get("q")


def read_request_body(request):
    """SOURCE: raw request body."""
    return request.data


def read_header(request, name):
    """SOURCE: request header value."""
    return request.headers.get(name)


def read_config_constant():
    """NOT a source: a trusted, in-repo constant.

    Present so the corpus can prove the exposed-subset filter distinguishes
    'reachable from untrusted input' from 'merely reaches a sink'.
    """
    return "SELECT 1"
