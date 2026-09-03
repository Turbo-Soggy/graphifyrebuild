"""Small git helpers shared by the branch-cache layer.

Every call shells out to git with an explicit ``-C root`` so the functions
work regardless of the caller's CWD (git hooks run at the repo root, but the
CLI may not).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True,
    )


def current_branch(root: Path = Path(".")) -> str | None:
    """Short branch name, or ``None`` on detached HEAD / not a repo."""
    r = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def previous_branch(root: Path = Path(".")) -> str | None:
    """The branch checked out before the last switch (``@{-1}``), if resolvable."""
    r = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{-1}")
    if r.returncode != 0:
        return None
    name = r.stdout.strip()
    # rev-parse prints "@{-1}" itself or an empty string when unresolvable,
    # and a raw commit hash when the previous checkout was detached.
    if not name or name.startswith("@") or all(c in "0123456789abcdef" for c in name):
        return None
    return name


def head_commit(root: Path = Path(".")) -> str | None:
    r = _git(root, "rev-parse", "HEAD")
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    """True iff ``ancestor`` is an ancestor of ``descendant``.

    Any git failure (unknown object after a rewrite + gc, shallow clone edge
    cases) returns False — callers treat that as "cache not trustworthy",
    which degrades to a full rebuild, never to a wrong graph.
    """
    r = _git(root, "merge-base", "--is-ancestor", ancestor, descendant)
    return r.returncode == 0


def git_root(start: Path = Path(".")) -> Path | None:
    r = _git(start, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        return None
    top = r.stdout.strip()
    return Path(top) if top else None
