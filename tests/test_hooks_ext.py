"""Tests for the hook-script composition against the VENDORED upstream source.

These import the real graphify.hooks module from graphify-upstream/ (its
package __init__ is lazy, so no heavy deps load) and verify our splice
actually lands — the composition raises loudly if upstream's layout drifts.
"""
import re

import pytest


@pytest.fixture()
def hooks_ext(upstream_on_path):
    from graphify_ext import hooks_ext as he
    return he


class TestComposition:
    def test_bodies_spliced_into_both_scripts(self, hooks_ext):
        commit, checkout = hooks_ext._compose_scripts()
        assert "graphify_ext.branch_cache" in commit
        assert "post_commit_update" in commit
        assert "swap_or_build" in checkout
        assert hooks_ext._EXT_STAMP.strip() in commit
        assert hooks_ext._EXT_STAMP.strip() in checkout

    def test_stock_bodies_removed(self, hooks_ext):
        commit, checkout = hooks_ext._compose_scripts()
        # The stock rebuild entrypoint must not survive in either script.
        assert "_rebuild_code" not in commit
        assert "_rebuild_code" not in checkout

    def test_shell_scaffold_preserved(self, hooks_ext):
        commit, checkout = hooks_ext._compose_scripts()
        for script in (commit, checkout):
            assert "GRAPHIFY_PYTHON" in script          # interpreter probe kept
            assert "rebase-merge" in script             # rebase guard kept
            assert "GRAPHIFY_SKIP_HOOK" in script       # opt-out kept
        assert "GRAPHIFY_CHANGED" in commit             # change-set export kept
        assert 'BRANCH_SWITCH" != "1"' in checkout      # file-checkout guard kept

    def test_checkout_guard_widened_for_cache_dir(self, hooks_ext):
        _, checkout = hooks_ext._compose_scripts()
        assert '.graphify-cache' in checkout
        assert 'if [ ! -d "graphify-out" ]; then' not in checkout

    def test_launcher_constraint_respected(self, hooks_ext):
        # Bodies ride inside a double-quoted sh -c argument and a
        # triple-single-quoted Python string: no ", $, backtick, backslash.
        for body in (hooks_ext._CHECKOUT_BODY_EXT, hooks_ext._COMMIT_BODY_EXT):
            assert not re.search(r'["$`\\]', body), \
                f"forbidden launcher character in body:\n{body}"

    def test_markers_are_stock_markers(self, hooks_ext, upstream_on_path):
        from graphify import hooks as up
        commit, checkout = hooks_ext._compose_scripts()
        assert commit.startswith(up._HOOK_MARKER)
        assert commit.rstrip().endswith(up._HOOK_MARKER_END)
        assert checkout.startswith(up._CHECKOUT_MARKER)
        assert checkout.rstrip().endswith(up._CHECKOUT_MARKER_END)


class TestRelationsParity:
    def test_default_relations_superset_of_upstream(self):
        # Parse the constant from source instead of importing: affected.py
        # pulls in networkx, which this test environment deliberately lacks.
        import ast
        from conftest import UPSTREAM
        from graphify_ext.blast_radius import DEFAULT_RELATIONS
        tree = ast.parse((UPSTREAM / "graphify" / "affected.py").read_text(encoding="utf-8"))
        upstream_relations = None
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", "") == "DEFAULT_AFFECTED_RELATIONS"
                            for t in node.targets)):
                upstream_relations = ast.literal_eval(node.value)
        assert upstream_relations, "DEFAULT_AFFECTED_RELATIONS not found upstream"
        assert set(upstream_relations) <= set(DEFAULT_RELATIONS)


class TestInstallOnRepo:
    def test_install_writes_customized_hooks(self, hooks_ext, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        msg = hooks_ext.install(git_repo)
        assert "post-commit" in msg and "post-checkout" in msg
        pc = git_repo / ".git" / "hooks" / "post-commit"
        co = git_repo / ".git" / "hooks" / "post-checkout"
        assert "graphify-ext-customized" in pc.read_text(encoding="utf-8")
        assert "graphify-ext-customized" in co.read_text(encoding="utf-8")

    def test_install_replaces_stock_block_in_place(self, hooks_ext, git_repo,
                                                   monkeypatch, upstream_on_path):
        from graphify import hooks as up
        monkeypatch.chdir(git_repo)
        up.install(git_repo)                     # stock first
        hooks_ext.install(git_repo)              # then customize
        co = (git_repo / ".git" / "hooks" / "post-checkout").read_text(encoding="utf-8")
        assert co.count(up._CHECKOUT_MARKER) == 1   # exactly one block
        assert "graphify-ext-customized" in co

    def test_status_reports_variant(self, hooks_ext, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        hooks_ext.install(git_repo)
        s = hooks_ext.status(git_repo)
        assert "graphify-ext variant" in s
