#!/usr/bin/env python3
"""Per-branch cache swap for code-review-graph (CRG) — Requirement 1.

One cached graph state per branch under ``.crg-cache/<slot>/``, with the whole
``.code-review-graph/`` DATA DIRECTORY presented as a link (symlink/junction,
copy-mode fallback) to the active branch's slot. The directory — not the bare
``graph.db`` file the spec sketched — because the DB runs in WAL mode: SQLite
drops ``-wal``/``-shm`` side-files next to the database, and a dir link keeps
those in the slot with zero special-casing. Every CRG CLI/MCP/daemon consumer
keeps reading ``.code-review-graph/graph.db`` untouched.

Verified against CRG v2.3.8 source (not docs) before implementing:

* DB path: ``<repo>/.code-review-graph/graph.db`` (incremental.get_db_path;
  resolution: registry --data-dir -> CRG_DATA_DIR env -> default). This script
  manages the DEFAULT location; a registry/env override is detected and
  refused loudly rather than silently mismanaged.
* CRG has NO git hooks — its ``hooks/`` directory is Claude Code agent hooks
  (SessionStart/PostToolUse). ``install-hook`` here writes a plain
  ``.git/hooks/post-checkout`` (and optional post-commit) directly.
* The spec's "CRG's DB has no git-history anchor" is WRONG: the DB metadata
  table stores ``git_head_sha`` at every build/update, and CRG's own
  ``resolve_incremental_base`` diffs against it — explicitly designed to
  reconcile "multi-commit pull, rebase, or branch switch", falling back to a
  full rebuild itself when the anchor is unusable. The trustworthiness check
  below reads that stored SHA as its primary source (sidecar as fallback).
* ``code-review-graph update`` prints "Incremental: N files updated" vs
  "Full rebuild (no usable incremental base)" — so update-vs-build is
  directly observable in output, which the verification steps rely on.

Every invocation prints exactly one of the four labeled outcomes:
  FULL BUILD | CACHE HIT + UPDATE | CACHE INVALID, REBUILDING | DETACHED HEAD, FULL BUILD

Usage:
  python swap_or_build.py                 # swap/build for the current branch
  python swap_or_build.py install-hook    # write .git/hooks/post-checkout
  python swap_or_build.py install-hook --with-post-commit
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

CACHE_ROOT_NAME = ".crg-cache"
DATA_DIR_NAME = ".code-review-graph"
MANIFEST_NAME = "manifest.json"
DETACHED_SLOT = "@detached"
def _default_crg_cli() -> str:
    """Prefer the code-review-graph launcher installed next to THIS
    interpreter (venv Scripts/bin), so hooks work without PATH setup."""
    override = os.environ.get("CRG_CLI", "").strip()
    if override:
        return override
    exe_dir = Path(sys.executable).parent
    for name in ("code-review-graph.exe", "code-review-graph"):
        cand = exe_dir / name
        if cand.exists():
            return str(cand)
    return "code-review-graph"


CRG_CLI = _default_crg_cli()


def log(msg: str) -> None:
    print(f"[crg-branch-cache] {msg}", flush=True)


# ------------------------------------------------------------------ git helpers

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True)


def git_root() -> Path | None:
    r = _git(Path("."), "rev-parse", "--show-toplevel")
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def current_branch(root: Path) -> str | None:
    r = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def previous_branch(root: Path) -> str | None:
    r = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{-1}")
    name = r.stdout.strip() if r.returncode == 0 else ""
    if not name or name.startswith("@") or all(c in "0123456789abcdef" for c in name):
        return None
    return name


def head_commit(root: Path) -> str | None:
    r = _git(root, "rev-parse", "HEAD")
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


# ---------------------------------------------------- link plumbing (vendored)
# Vendored from graphify_ext.branch_cache (same repo, unit-tested there) so
# this script stands alone. Junction traversal can be BROKEN by filter
# drivers even when creation succeeds — hence the functional probe.

def _is_reparse_point(p: Path) -> bool:
    if os.path.islink(p):
        return True
    if os.name != "nt":
        return False
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return bool(getattr(st, "st_reparse_tag", 0))


def _remove_link(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        os.rmdir(p)


def _make_link(link: Path, target: Path) -> bool:
    target_abs = target.resolve()
    try:
        link.symlink_to(target_abs, target_is_directory=True)
        return True
    except OSError:
        if os.name != "nt":
            return False
    try:
        import _winapi
        _winapi.CreateJunction(str(target_abs), str(link))
        return True
    except Exception:
        return False


def _link_traversal_works(link: Path) -> bool:
    probe = link / ".crg_linkprobe"
    try:
        os.mkdir(probe)
        os.rmdir(probe)
        return True
    except OSError:
        return False


def _copy_tree(src: Path, dst: Path, exclude: frozenset[str] = frozenset()) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in exclude:
            continue
        d = dst / item.name
        if item.is_dir() and not _is_reparse_point(item):
            _copy_tree(item, d)
        elif item.is_file():
            shutil.copy2(item, d)


def _clear_dir(p: Path, keep: frozenset[str] = frozenset()) -> None:
    for item in p.iterdir():
        if item.name in keep:
            continue
        if item.is_dir() and not _is_reparse_point(item):
            shutil.rmtree(item, ignore_errors=True)
        else:
            try:
                item.unlink()
            except OSError:
                try:
                    os.rmdir(item)
                except OSError:
                    pass


# ------------------------------------------------------------------ slot model

def slot_name(branch: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", branch).strip(".") or "branch"
    if safe != branch:
        import hashlib
        safe += "-" + hashlib.sha1(branch.encode("utf-8")).hexdigest()[:8]
    return safe


def slot_for(root: Path, branch: str) -> Path:
    return root / CACHE_ROOT_NAME / slot_name(branch)


def slot_data(slot: Path) -> Path:
    return slot / "data"


def has_cache(slot: Path) -> bool:
    db = slot_data(slot) / "graph.db"
    return db.is_file() and db.stat().st_size > 0


def crg_version() -> str:
    try:
        r = subprocess.run([CRG_CLI, "--version"], capture_output=True, text=True,
                           timeout=30)
        # "code-review-graph 2.3.8"
        return r.stdout.strip().split()[-1] if r.returncode == 0 else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def write_manifest(slot: Path, root: Path, branch: str | None) -> None:
    slot.mkdir(parents=True, exist_ok=True)
    payload = {
        "_base_commit": head_commit(root),
        "branch": branch,
        "crg_version": crg_version(),
        "stamped_at": time.time(),
    }
    tmp = slot / (MANIFEST_NAME + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, slot / MANIFEST_NAME)


def read_manifest(slot: Path) -> dict:
    try:
        return json.loads((slot / MANIFEST_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def db_metadata(db: Path, key: str) -> str | None:
    """Read one key from the slot DB's metadata table (CRG's own anchor store)."""
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def cache_is_trustworthy(slot: Path, root: Path) -> tuple[bool, str]:
    """(trustworthy, reason). Base commit prefers the DB's own git_head_sha —
    written by CRG at every build/update — over the sidecar stamp."""
    manifest = read_manifest(slot)
    ver = manifest.get("crg_version")
    if ver and ver != "unknown":
        cur = crg_version()
        if cur != "unknown" and cur != ver:
            return False, f"CRG version changed ({ver} -> {cur})"
    base = db_metadata(slot_data(slot) / "graph.db", "git_head_sha") \
        or manifest.get("_base_commit")
    if not base:
        return True, "no base-commit anchor recorded; trusting (update reconciles by diff)"
    head = head_commit(root)
    if head is None:
        return False, "cannot resolve HEAD"
    if not is_ancestor(root, base, head):
        return False, f"base commit {base[:10]} is not an ancestor of HEAD (history rewritten?)"
    return True, f"base commit {base[:10]} is an ancestor of HEAD"


# ------------------------------------------------------------------ activation

def data_dir(root: Path) -> Path:
    return root / DATA_DIR_NAME


def _refuse_if_redirected(root: Path) -> None:
    """CRG resolves its data dir as registry --data-dir -> CRG_DATA_DIR ->
    default. This script manages the DEFAULT location only; managing a
    redirected dir would swap a directory CRG isn't reading."""
    if os.environ.get("CRG_DATA_DIR", "").strip():
        sys.exit("[crg-branch-cache] ERROR: CRG_DATA_DIR is set — this cache "
                 "manages the default <repo>/.code-review-graph location only. "
                 "Unset CRG_DATA_DIR or extend the script.")


def active_slot_data(root: Path) -> Path | None:
    d = data_dir(root)
    if not _is_reparse_point(d):
        return None
    try:
        resolved = Path(os.path.realpath(d))
        resolved.relative_to((root / CACHE_ROOT_NAME).resolve())
        return resolved
    except (OSError, ValueError):
        return None


_OWNER_FILE = ".crg_cache_owner"


def mirror_back(root: Path) -> None:
    """Copy mode only: sync the real data dir into its owning slot before a swap."""
    d = data_dir(root)
    if not d.exists() or _is_reparse_point(d):
        return
    owner: Path | None = None
    try:
        txt = (d / _OWNER_FILE).read_text(encoding="utf-8-sig").strip()
        owner = Path(txt) if txt else None
    except OSError:
        pass
    if owner is None:
        branch = previous_branch(root) or current_branch(root)
        if branch is None:
            return
        owner = slot_data(slot_for(root, branch))
    if owner.exists() and any(owner.iterdir()):
        _clear_dir(owner)
    _copy_tree(d, owner, exclude=frozenset({_OWNER_FILE}))


def activate(root: Path, slot: Path) -> str:
    """Point .code-review-graph at the slot's data dir. Returns 'link' or 'copy'."""
    d = data_dir(root)
    target = slot_data(slot)
    target.mkdir(parents=True, exist_ok=True)

    if _is_reparse_point(d):
        if active_slot_data(root) == target.resolve():
            return "link"
        _remove_link(d)
    elif d.exists():
        if not has_cache(slot):
            # Adopt existing real data dir into this slot (first install).
            if any(target.iterdir()):
                _clear_dir(target)
            _copy_tree(d, target, exclude=frozenset({_OWNER_FILE}))
        shutil.rmtree(d, ignore_errors=True)
        if d.exists():
            _clear_dir(d)
            _copy_tree(target, d)
            (d / _OWNER_FILE).write_text(str(target.resolve()), encoding="utf-8")
            return "copy"

    if _make_link(d, target) and _link_traversal_works(d):
        return "link"
    if _is_reparse_point(d):
        _remove_link(d)

    d.mkdir(parents=True, exist_ok=True)
    _clear_dir(d)
    _copy_tree(target, d)
    (d / _OWNER_FILE).write_text(str(target.resolve()), encoding="utf-8")
    return "copy"


def mirror_out_to(root: Path, slot: Path) -> None:
    d = data_dir(root)
    if _is_reparse_point(d) or not d.exists():
        return
    target = slot_data(slot)
    if target.exists() and any(target.iterdir()):
        _clear_dir(target)
    _copy_tree(d, target, exclude=frozenset({_OWNER_FILE}))
    (d / _OWNER_FILE).write_text(str(target.resolve()), encoding="utf-8")


# ------------------------------------------------------------------ CRG calls

def run_crg(root: Path, *args: str) -> bool:
    """Run a CRG command, streaming its output (the update/build lines ARE the
    verification evidence), returning success."""
    try:
        r = subprocess.run([CRG_CLI, *args], cwd=str(root))
        return r.returncode == 0
    except OSError as exc:
        log(f"ERROR: could not run {CRG_CLI} {' '.join(args)}: {exc}")
        return False


# ------------------------------------------------------------------ main logic

def _ensure_ignores(root: Path) -> None:
    """Keep cache + data-dir link out of git via .git/info/exclude.

    NOT the repo .gitignore: that is a TRACKED file, so editing it dirties the
    worktree and git then refuses the very branch switches this cache serves.
    info/exclude is repo-local and untracked. The bare (no-slash) entry
    matters: a trailing-slash pattern does not match the .code-review-graph
    SYMLINK, and CRG's own ensure_repo_gitignore_excludes_crg only writes the
    slash form. The cache dir additionally self-ignores via an inner '*'
    .gitignore so it stays out even where info/exclude is bypassed."""
    cache = root / CACHE_ROOT_NAME
    cache.mkdir(parents=True, exist_ok=True)
    inner = cache / ".gitignore"
    if not inner.exists():
        inner.write_text("*\n", encoding="utf-8")
    exclude = root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8-sig") if exclude.exists() else ""
    lines = {ln.strip() for ln in existing.splitlines()}
    wanted = [DATA_DIR_NAME, f"{DATA_DIR_NAME}/", f"{CACHE_ROOT_NAME}/"]
    missing = [w for w in wanted if w not in lines]
    if missing:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        exclude.write_text(existing + "\n".join(missing) + "\n", encoding="utf-8")
        log(f"added {', '.join(missing)} to .git/info/exclude")


def swap_or_build(branch: str | None = None) -> bool:
    root = git_root()
    if root is None:
        log("ERROR: not inside a git repository")
        return False
    _refuse_if_redirected(root)
    os.chdir(root)
    try:
        _ensure_ignores(root)
    except OSError as exc:
        log(f"WARNING: could not update .gitignore: {exc}")

    if branch is None:
        branch = current_branch(root)

    started = time.time()
    mirror_back(root)  # copy-mode housekeeping; no-op in link mode

    if branch is None:
        slot = root / CACHE_ROOT_NAME / DETACHED_SLOT
        slot_data(slot).mkdir(parents=True, exist_ok=True)
        _clear_dir(slot_data(slot))
        mode = activate(root, slot)
        log(f"DETACHED HEAD, FULL BUILD (scratch slot, {mode} mode)")
        ok = run_crg(root, "build")
        if ok:
            if mode == "copy":
                mirror_out_to(root, slot)
            write_manifest(slot, root, None)
        _done(started, ok)
        return ok

    slot = slot_for(root, branch)
    exists = has_cache(slot)
    trustworthy, reason = cache_is_trustworthy(slot, root) if exists else (False, "")
    mode = activate(root, slot)

    if not exists:
        log(f"FULL BUILD (no cache for branch '{branch}'; slot {slot.name}, {mode} mode)")
        ok = run_crg(root, "build")
    elif not trustworthy:
        log(f"CACHE INVALID, REBUILDING ({reason}; slot {slot.name}, {mode} mode)")
        ok = run_crg(root, "build")
    else:
        log(f"CACHE HIT + UPDATE ({reason}; slot {slot.name}, {mode} mode)")
        ok = run_crg(root, "update")
        if not ok:
            # No silent failures: a failed reconcile (e.g. corrupt slot DB)
            # clears the active data dir and falls back to a fresh full build.
            log("update failed - clearing slot data, falling back to FULL BUILD")
            try:
                _clear_dir(data_dir(root), keep=frozenset({_OWNER_FILE}))
            except OSError as exc:
                log(f"WARNING: could not clear data dir: {exc}")
            ok = run_crg(root, "build")

    if ok:
        _reapply_injected(root)
        if mode == "copy":
            mirror_out_to(root, slot)
        write_manifest(slot, root, branch)
    else:
        log("ERROR: build/update failed; cache slot NOT stamped (next run rebuilds)")
    _done(started, ok)
    return ok


def _reapply_injected(root: Path) -> None:
    """Best-effort: re-resolve stored taint + config findings after a
    build/update, so injected edges survive a rebuilt graph. Both findings
    files live in the data dir (= the branch slot), so this is inherently
    per-branch. Never fails the swap."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    for label, module in (("taint", "taint_inject"), ("config", "config_link")):
        try:
            mod = __import__(module)
            report = mod.reapply(root)
            if report is not None:
                unresolved = len(report.get("unresolved", []))
                log(f"re-applied {report['applied']} {label} edge(s)"
                    + (f" ({unresolved} unresolved)" if unresolved else ""))
        except Exception as exc:
            log(f"WARNING: {label} reapply skipped: {exc!r}")


def _done(started: float, ok: bool) -> None:
    log(f"{'done' if ok else 'FAILED'} in {time.time() - started:.1f}s")


# ------------------------------------------------------------------ git hooks

_POST_CHECKOUT = """\
#!/bin/sh
# crg-branch-cache post-checkout hook (installed by swap_or_build.py)
# $3 == 1 -> branch switch; 0 -> file checkout (skip)
[ "$3" = "1" ] || exit 0
[ "$1" = "$2" ] && exit 0   # no-op checkout (e.g. checkout -b), tree unchanged
[ "${{CRG_CACHE_SKIP:-0}}" = "1" ] && exit 0
"{python}" "{script}" >> "{log}" 2>&1 &
exit 0
"""

_POST_COMMIT = """\
#!/bin/sh
# crg-branch-cache post-commit hook (installed by swap_or_build.py)
[ "${{CRG_CACHE_SKIP:-0}}" = "1" ] && exit 0
"{python}" "{script}" >> "{log}" 2>&1 &
exit 0
"""


def install_hook(with_post_commit: bool = False) -> None:
    root = git_root()
    if root is None:
        sys.exit("[crg-branch-cache] ERROR: not inside a git repository")
    hooks = root / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    logfile = root / CACHE_ROOT_NAME / "hook.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    subs = {
        "python": Path(sys.executable).as_posix(),
        "script": script.as_posix(),
        "log": logfile.as_posix(),
    }
    targets = [("post-checkout", _POST_CHECKOUT)]
    if with_post_commit:
        targets.append(("post-commit", _POST_COMMIT))
    for name, template in targets:
        p = hooks / name
        if p.exists() and "crg-branch-cache" not in p.read_text(encoding="utf-8",
                                                                errors="replace"):
            log(f"SKIPPED {name}: an unrelated hook already exists at {p}")
            continue
        p.write_text(template.format(**subs), encoding="utf-8", newline="\n")
        try:
            p.chmod(0o755)
        except OSError:
            pass
        log(f"installed {name} hook -> {p}")
    log(f"hook output goes to {logfile}")


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "install-hook":
        install_hook(with_post_commit="--with-post-commit" in args)
        return 0
    if args and args[0] not in ("swap",):
        print(__doc__)
        return 2 if args[0] not in ("-h", "--help") else 0
    return 0 if swap_or_build() else 1


if __name__ == "__main__":
    sys.exit(main())
