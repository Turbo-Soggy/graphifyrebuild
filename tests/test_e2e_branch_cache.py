"""End-to-end branch-cache verification against the REAL graphifyy package.

Covers the spec's pre-ship checklist scenarios by driving the exact functions
the customized hook bodies invoke (synchronously — the detachment wrapper is
upstream's launcher, not under test here):

* first checkout of a branch  -> full build (fallback trigger 1)
* checkout -b + commit        -> new slot seeded from active, incremental
* switch-back                 -> cache reused, incremental reconcile, and the
                                 pre-switch edit is present (NOT a full rebuild)
* history rewrite             -> trustworthiness fails -> full rebuild (trigger 2)
* detached HEAD               -> scratch slot full rebuild (trigger 3)
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

pytest.importorskip("graphify.watch", reason="graphifyy not installed")

from graphify_ext import branch_cache as bc


def _rmtree_with_links(base: Path) -> None:
    """rmtree that first detaches symlinks/junctions instead of recursing into
    (or choking on) them — shutil.rmtree(ignore_errors=True) can leave a tree
    containing a junction half-deleted."""
    import shutil
    import stat
    if not base.exists():
        return
    for p in sorted(base.rglob("*"), key=lambda x: -len(x.parts)):
        if bc._is_reparse_point(p):
            bc._remove_link(p)

    def _clear_readonly(func, path, exc):
        # git object files are read-only; Windows unlink refuses them.
        os.chmod(path, stat.S_IWRITE)
        func(path)

    shutil.rmtree(base, onexc=_clear_readonly)


def _git(root, *args):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def _labels(root) -> set[str]:
    data = json.loads((root / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    return {n.get("label") for n in data.get("nodes", [])}


@pytest.fixture(params=["tmp", "local"])
def repo(request, tmp_path, monkeypatch):
    """Two locations on purpose: the system temp dir (where this machine's
    filter driver breaks junction traversal -> exercises COPY mode) and a
    directory next to the project (junctions work -> exercises LINK mode)."""
    if request.param == "local":
        project_root = Path(__file__).resolve().parent.parent
        base = project_root / ".e2e-tmp"
        _rmtree_with_links(base)
        base.mkdir()

        def _cleanup():
            # This finalizer runs before monkeypatch undoes chdir, so step out
            # of the tree first or the root rmdir fails with a busy handle.
            os.chdir(project_root)
            _rmtree_with_links(base)

        request.addfinalizer(_cleanup)
        tmp_path = base
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / ".gitignore").write_text(
        "graphify-out/\n.graphify-cache/\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("def func_a():\n    return 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "c1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRAPHIFY_MAX_WORKERS", "1")
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    monkeypatch.setenv("GRAPHIFY_VIZ_NODE_LIMIT", "0")
    return tmp_path


def test_full_branch_cache_lifecycle(repo, capsys):
    # 1. First build on main: no cache -> full build, slot stamped.
    assert bc.swap_or_build()
    assert "full rebuild" in capsys.readouterr().out
    assert "func_a()" in _labels(repo)
    main_slot = bc.slot_for(repo, "main")
    assert bc.has_cache(main_slot)
    assert bc.read_meta(main_slot)["branch"] == "main"

    # 2. checkout -b feature (no hook fires: HEAD unchanged); commit b.py.
    #    post_commit_update must seed feature's slot from main's and update
    #    incrementally.
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "b.py").write_text("def func_b():\n    return 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "c2")
    assert bc.post_commit_update([Path("b.py")])
    labels = _labels(repo)
    assert {"func_a()", "func_b()"} <= labels
    feature_slot = bc.slot_for(repo, "feature")
    assert bc.has_cache(feature_slot)

    # 3. Switch back to main: cache reused (no full rebuild), func_b absent.
    _git(repo, "checkout", "-q", "main")
    assert bc.swap_or_build()
    out = capsys.readouterr().out
    assert "full rebuild" not in out
    labels = _labels(repo)
    assert "func_a()" in labels and "func_b()" not in labels

    # 4. Spec scenario: edit+commit on main, switch away, switch back —
    #    the edit must be there via incremental reconcile, not full rebuild.
    (repo / "a.py").write_text(
        "def func_a():\n    return 1\n\ndef func_c():\n    return 3\n",
        encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "c3")
    assert bc.post_commit_update([Path("a.py")])
    assert "func_c()" in _labels(repo)
    capsys.readouterr()

    _git(repo, "checkout", "-q", "feature")
    assert bc.swap_or_build()
    assert "full rebuild" not in capsys.readouterr().out
    assert "func_c()" not in _labels(repo)  # feature predates the edit

    _git(repo, "checkout", "-q", "main")
    assert bc.swap_or_build()
    out = capsys.readouterr().out
    assert "full rebuild" not in out
    assert "func_c()" in _labels(repo)  # pre-switch edit survived the round-trip

    # 5. History rewrite on feature behind the cache's back -> full rebuild.
    _git(repo, "checkout", "-q", "feature")
    assert bc.swap_or_build()
    capsys.readouterr()
    _git(repo, "commit", "-q", "--amend", "-m", "c2-rewritten")
    # (no post_commit_update: simulates the rewrite arriving via force-push)
    _git(repo, "checkout", "-q", "main")
    assert bc.swap_or_build()
    capsys.readouterr()
    _git(repo, "checkout", "-q", "feature")
    assert bc.swap_or_build()
    assert "full rebuild" in capsys.readouterr().out
    assert {"func_a()", "func_b()"} <= _labels(repo)

    # 6. Detached HEAD -> scratch slot, full rebuild.
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", head)
    assert bc.swap_or_build()
    out = capsys.readouterr().out
    assert "detached HEAD" in out and "full rebuild" in out
    assert (bc.cache_root(repo) / bc.DETACHED_SLOT / "graph.json").exists()
