import json
import os
import subprocess
from pathlib import Path

import pytest

from graphify_ext import branch_cache as bc
from graphify_ext import gitutil


def _git(root, *args):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class TestSlotNaming:
    def test_plain_branch_unchanged(self):
        assert bc.slot_name("main") == "main"

    def test_slash_sanitized_with_digest(self):
        s = bc.slot_name("feature/x")
        assert "/" not in s
        assert s.startswith("feature-x-")

    def test_no_collision_between_slash_and_dash(self):
        assert bc.slot_name("feature/x") != bc.slot_name("feature-x")

    def test_deterministic(self):
        assert bc.slot_name("feature/x") == bc.slot_name("feature/x")


class TestMetaStamp:
    def test_stamp_and_read_roundtrip(self, git_repo):
        slot = git_repo / ".graphify-cache" / "main"
        bc.stamp(slot, git_repo, "main")
        meta = bc.read_meta(slot)
        assert meta["branch"] == "main"
        assert meta["base_commit"] == gitutil.head_commit(git_repo)

    def test_no_meta_is_trusted(self, git_repo):
        slot = git_repo / ".graphify-cache" / "main"
        slot.mkdir(parents=True)
        assert bc.cache_is_trustworthy(slot, git_repo)

    def test_ancestor_commit_is_trusted(self, git_repo):
        slot = git_repo / ".graphify-cache" / "main"
        bc.stamp(slot, git_repo, "main")
        # Advance the branch: stamped commit becomes an ancestor of HEAD.
        (git_repo / "c.py").write_text("x = 1\n", encoding="utf-8")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-q", "-m", "c3")
        assert bc.cache_is_trustworthy(slot, git_repo)

    def test_history_rewrite_is_not_trusted(self, git_repo):
        (git_repo / "c.py").write_text("x = 1\n", encoding="utf-8")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-q", "-m", "c3")
        slot = git_repo / ".graphify-cache" / "main"
        bc.stamp(slot, git_repo, "main")
        # Rewrite: amend the tip — the stamped commit is no longer an ancestor.
        _git(git_repo, "commit", "-q", "--amend", "-m", "c3-rewritten")
        assert not bc.cache_is_trustworthy(slot, git_repo)

    def test_version_change_is_not_trusted(self, git_repo, monkeypatch):
        slot = git_repo / ".graphify-cache" / "main"
        monkeypatch.setattr(bc, "_graphify_version", lambda: "8.0.0")
        bc.stamp(slot, git_repo, "main")
        monkeypatch.setattr(bc, "_graphify_version", lambda: "9.0.0")
        assert not bc.cache_is_trustworthy(slot, git_repo)

    def test_unknown_current_version_is_trusted(self, git_repo, monkeypatch):
        slot = git_repo / ".graphify-cache" / "main"
        monkeypatch.setattr(bc, "_graphify_version", lambda: "8.0.0")
        bc.stamp(slot, git_repo, "main")
        monkeypatch.setattr(bc, "_graphify_version", lambda: "unknown")
        assert bc.cache_is_trustworthy(slot, git_repo)


class TestActivate:
    def _seed_slot(self, root, name="main"):
        slot = root / ".graphify-cache" / name
        slot.mkdir(parents=True)
        (slot / "graph.json").write_text('{"nodes": []}', encoding="utf-8")
        (slot / "manifest.json").write_text("{}", encoding="utf-8")
        return slot

    def test_activate_creates_link_or_copy(self, git_repo):
        slot = self._seed_slot(git_repo)
        mode = bc.activate(git_repo, slot)
        out = git_repo / "graphify-out"
        assert out.exists()
        assert (out / "graph.json").exists()
        assert mode in ("link", "copy")
        if mode == "link":
            assert bc.active_slot(git_repo) == slot.resolve()

    def test_activate_swaps_between_slots(self, git_repo):
        a = self._seed_slot(git_repo, "main")
        b = self._seed_slot(git_repo, "feat")
        (b / "graph.json").write_text('{"nodes": [{"id": "b"}]}', encoding="utf-8")
        bc.activate(git_repo, a)
        bc.activate(git_repo, b)
        out = git_repo / "graphify-out"
        assert json.loads((out / "graph.json").read_text())["nodes"] == [{"id": "b"}]

    def test_real_dir_migrates_into_slot(self, git_repo):
        out = git_repo / "graphify-out"
        out.mkdir()
        (out / "graph.json").write_text('{"nodes": [{"id": "orig"}]}', encoding="utf-8")
        (out / "manifest.json").write_text("{}", encoding="utf-8")
        slot = git_repo / ".graphify-cache" / "main"
        bc.activate(git_repo, slot)
        # Whatever the mode, the original data must survive in the slot and
        # remain visible through graphify-out.
        assert json.loads((slot / "graph.json").read_text())["nodes"] == [{"id": "orig"}]
        assert json.loads((out / "graph.json").read_text())["nodes"] == [{"id": "orig"}]

    def test_writes_through_link_land_in_slot(self, git_repo):
        slot = self._seed_slot(git_repo)
        mode = bc.activate(git_repo, slot)
        if mode != "link":
            pytest.skip("filesystem does not support links")
        out = git_repo / "graphify-out"
        (out / "GRAPH_REPORT.md").write_text("hi", encoding="utf-8")
        assert (slot / "GRAPH_REPORT.md").read_text() == "hi"


class TestHasCache:
    def test_empty_slot_has_no_cache(self, tmp_path):
        assert not bc.has_cache(tmp_path)

    def test_full_slot_has_cache(self, tmp_path):
        (tmp_path / "graph.json").write_text("{}", encoding="utf-8")
        (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
        assert bc.has_cache(tmp_path)

    def test_empty_files_do_not_count(self, tmp_path):
        (tmp_path / "graph.json").write_text("", encoding="utf-8")
        (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
        assert not bc.has_cache(tmp_path)


class TestGitutil:
    def test_current_branch(self, git_repo):
        assert gitutil.current_branch(git_repo) == "main"

    def test_previous_branch(self, git_repo):
        # fixture ends with: checkout feature/x -> checkout main
        assert gitutil.previous_branch(git_repo) == "feature/x"

    def test_detached_head(self, git_repo):
        head = gitutil.head_commit(git_repo)
        _git(git_repo, "checkout", "-q", head)
        assert gitutil.current_branch(git_repo) is None
