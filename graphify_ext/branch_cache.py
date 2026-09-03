"""Per-branch incremental graph caching (Requirement 1).

One cached graph state per branch under ``.graphify-cache/<slot>/``, with
``graphify-out/`` kept as a link (symlink on POSIX, symlink-or-junction on
Windows) to the ACTIVE branch's slot, so every stock consumer
(``graphify query`` / ``explain`` / ``path``, the installed skill files, the
post-commit hook's manifest read/write) keeps reading the same path with zero
changes. Upstream explicitly supports a linked output dir: its atomic writer
resolves symlinks and writes THROUGH the link (graphify.paths._atomic_replace).

Verified upstream facts this module relies on (graphify v8 source, not README):

* ``manifest.json`` is portable: keys are forward-slash repo-relative paths
  (when ``save_manifest(root=...)`` is used, which the rebuild path does) and
  values carry content hashes (``ast_hash``/``semantic_hash``) — mtime is only
  a fast path, MD5 is the ground truth (graphify.detect.save_manifest /
  detect_incremental). Cross-branch reuse is therefore valid: after a branch
  switch bumps mtimes, the hash check decides what really changed.
* ``graphify update`` / ``_rebuild_code(root)`` with no ``changed_paths`` is a
  FULL corpus re-extract — the incremental gate is ``detect_incremental``
  feeding ``changed_paths``. So the swap-back reconcile here computes the
  changed set from the slot's manifest itself instead of naively invoking a
  full ``update``.
* The stock manifest has no git-history anchor and graph.json has no schema
  version field. Both stamps live in a slot-local sidecar
  (``graphify_ext_meta.json``) instead of inside manifest.json: injecting a
  non-file key into the manifest would surface as a phantom "deleted file" in
  detect_incremental's corpus sweep.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat as stat_mod
import time
from pathlib import Path

from graphify_ext import gitutil

CACHE_ROOT_NAME = os.environ.get("GRAPHIFY_EXT_CACHE", ".graphify-cache")
OUT_NAME = os.environ.get("GRAPHIFY_OUT", "graphify-out")
META_NAME = "graphify_ext_meta.json"
DETACHED_SLOT = "@detached"

# Files that constitute "a usable cache" in a slot.
_CACHE_CORE = ("graph.json", "manifest.json")


# ---------------------------------------------------------------- slot naming

def slot_name(branch: str) -> str:
    """Filesystem-safe, collision-free slot directory name for a branch.

    Plain sanitization ("feature/x" -> "feature-x") collides with a literal
    branch named "feature-x", so whenever sanitization changes the name a
    short digest of the ORIGINAL name is appended.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", branch).strip(".") or "branch"
    if safe != branch:
        safe += "-" + hashlib.sha1(branch.encode("utf-8")).hexdigest()[:8]
    return safe


def cache_root(root: Path) -> Path:
    return root / CACHE_ROOT_NAME


def slot_for(root: Path, branch: str) -> Path:
    return cache_root(root) / slot_name(branch)


def out_dir(root: Path) -> Path:
    return root / OUT_NAME


def has_cache(slot: Path) -> bool:
    return all((slot / f).is_file() and (slot / f).stat().st_size > 0 for f in _CACHE_CORE)


# ------------------------------------------------------------------ meta stamp

def _graphify_version() -> str:
    try:
        from importlib.metadata import version
        return version("graphifyy")
    except Exception:
        return "unknown"


def read_meta(slot: Path) -> dict:
    try:
        return json.loads((slot / META_NAME).read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def stamp(slot: Path, root: Path, branch: str | None) -> None:
    """Record the git/base-commit + graphify-version anchor for the slot.

    Called after every successful build/update, so the anchor always describes
    the state the cached graph + manifest were produced from.
    """
    meta = {
        "branch": branch,
        "base_commit": gitutil.head_commit(root),
        "graphify_version": _graphify_version(),
        "stamped_at": time.time(),
    }
    slot.mkdir(parents=True, exist_ok=True)
    tmp = slot / (META_NAME + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.replace(tmp, slot / META_NAME)


def cache_is_trustworthy(slot: Path, root: Path) -> bool:
    """Can the slot's cached graph be reconciled incrementally?

    False (=> full rebuild) when:
      * the recorded base commit is no longer an ancestor of HEAD — the
        branch history was rewritten (rebase/force-push) under the cache, or
        the commit was garbage-collected; the manifest then describes a tree
        state that never leads to HEAD and hash-reconciliation could preserve
        nodes from an abandoned history;
      * the graphify package version changed since the slot was built —
        stand-in for the missing graph.json schema-version field: an older
        graph may be schema-incompatible with the currently installed code.

    A slot with no meta stamp at all is trusted (no anchor recorded — the
    hash-based reconcile sorts out any file-level drift by itself).
    """
    meta = read_meta(slot)
    if not meta:
        return True
    ver = meta.get("graphify_version")
    if ver and ver != "unknown":
        cur = _graphify_version()
        if cur != "unknown" and cur != ver:
            return False
    base = meta.get("base_commit")
    if base:
        head = gitutil.head_commit(root)
        if head is None or not gitutil.is_ancestor(root, base, head):
            return False
    return True


# ------------------------------------------------------- link/junction plumbing

def _is_reparse_point(p: Path) -> bool:
    """True for symlinks AND Windows junctions (islink() misses junctions)."""
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
    """Remove a symlink or junction without touching the target's contents."""
    try:
        p.unlink()
    except OSError:
        # A directory junction needs rmdir, not unlink, on some Python builds.
        os.rmdir(p)


def _make_link(link: Path, target: Path) -> bool:
    """Point ``link`` at directory ``target``; returns False if impossible.

    POSIX: symlink. Windows: symlink (needs Developer Mode/privilege) with a
    directory-junction fallback (works unprivileged; target must be absolute).
    """
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


def active_slot(root: Path) -> Path | None:
    """The slot ``graphify-out`` currently points at, or None (real dir / absent)."""
    out = out_dir(root)
    if not out.exists() and not os.path.islink(out):
        return None
    if not _is_reparse_point(out):
        return None
    try:
        resolved = Path(os.path.realpath(out))
    except OSError:
        return None
    try:
        resolved.relative_to(cache_root(root).resolve())
    except ValueError:
        return None
    return resolved


def _link_traversal_works(link: Path) -> bool:
    """Functionally verify a just-created link: directory creation THROUGH it
    must work (a bare 'link exists' check is not enough — filter drivers can
    break reparse-point traversal for specific locations while the link
    itself looks fine)."""
    probe = link / ".graphify_ext_linkprobe"
    try:
        os.mkdir(probe)
        os.rmdir(probe)
        return True
    except OSError:
        return False


def activate(root: Path, slot: Path) -> str:
    """Make ``graphify-out`` present the given slot. Returns the mode used.

    Link mode (preferred): graphify-out becomes a symlink/junction into the
    slot; all writes land in the slot directly and no mirror-back is needed.

    Copy mode (fallback for filesystems without reparse points): the slot is
    copied over a real graphify-out directory; ``mirror_back`` must then sync
    graphify-out into the owning slot before the next swap (the checkout hook
    does this via git's ``@{-1}``, and the commit hook re-mirrors on commit).
    """
    out = out_dir(root)
    slot.mkdir(parents=True, exist_ok=True)

    if _is_reparse_point(out):
        if active_slot(root) == slot.resolve():
            return "link"
        _remove_link(out)
    elif out.exists():
        # Real directory. Adopt its contents into the slot ONLY when the slot
        # has no cache yet (first install / brand-new branch). When the slot
        # already has cache, the real dir belongs to the PREVIOUS branch —
        # mirror_back() (called before activate in the swap paths) has already
        # synced it into its owning slot, so copying it here would clobber the
        # target branch's cached state with the previous branch's.
        if not has_cache(slot):
            if any(slot.iterdir()):
                _clear_dir(slot, keep=frozenset({META_NAME}))
            _copy_tree(out, slot)
        shutil.rmtree(out, ignore_errors=True)
        if out.exists():
            # Something (AV, an open handle) is holding graphify-out: fall
            # back to copy mode in place rather than failing the swap.
            _clear_dir(out)
            _copy_tree(slot, out, exclude=frozenset({META_NAME}))
            _write_owner(out, slot)
            return "copy"

    # GRAPHIFY_EXT_LINK_MODE=copy forces the copy path even where links work.
    # Without a way to force it, copy mode could only ever be exercised on a
    # filesystem that happens to break links — which is not a thing you can
    # arrange on demand, so the "safe fallback" path could never be verified
    # as *equally correct*, only observed to be slower.
    forced = os.environ.get("GRAPHIFY_EXT_LINK_MODE", "").strip().lower()
    if forced not in ("copy", "link", ""):
        raise ValueError(
            f"GRAPHIFY_EXT_LINK_MODE must be 'copy', 'link', or unset; got {forced!r}")
    if forced != "copy" and _make_link(out, slot) and _link_traversal_works(out):
        return "link"
    if _is_reparse_point(out):
        # Link created but traversal through it is broken (observed in the
        # wild: filter drivers/EDR mishandle directory creation through
        # junctions in some locations, returning ERROR_ALREADY_EXISTS for
        # paths that do not exist). A half-working link would fail the next
        # rebuild mid-way, so tear it down and use copy mode.
        _remove_link(out)

    # Copy mode. The meta stamp stays slot-only: were it copied into the
    # shared out dir, later mirrors would smear one branch's anchor onto
    # another slot.
    out.mkdir(parents=True, exist_ok=True)
    _clear_dir(out)
    _copy_tree(slot, out, exclude=frozenset({META_NAME}))
    _write_owner(out, slot)
    return "copy"


_OWNER_FILE = ".graphify_ext_owner"


def _write_owner(out: Path, slot: Path) -> None:
    (out / _OWNER_FILE).write_text(str(slot.resolve()), encoding="utf-8")


def _read_owner(out: Path) -> Path | None:
    try:
        txt = (out / _OWNER_FILE).read_text(encoding="utf-8-sig").strip()
        return Path(txt) if txt else None
    except OSError:
        return None


def mirror_back(root: Path) -> None:
    """Copy-mode only: sync the real ``graphify-out`` dir into its owning slot.

    No-op in link mode (writes already landed in the slot through the link).
    """
    out = out_dir(root)
    if not out.exists() or _is_reparse_point(out):
        return
    owner = _read_owner(out)
    if owner is None:
        # Real dir with no owner record: adopt it into the previous branch's
        # slot if git can tell us which branch that was, else current branch.
        branch = gitutil.previous_branch(root) or gitutil.current_branch(root)
        if branch is None:
            return
        owner = slot_for(root, branch)
    if owner.exists() and any(owner.iterdir()):
        # The meta stamp lives ONLY in the slot (never in graphify-out), so a
        # plain clear would silently drop the base-commit anchor and disable
        # the history-rewrite check. Keep it.
        _clear_dir(owner, keep=frozenset({META_NAME}))
    _copy_tree(out, owner)
    # The owner file itself must not pollute the slot.
    try:
        (owner / _OWNER_FILE).unlink()
    except OSError:
        pass


# ------------------------------------------------------------ rebuild plumbing

@contextlib.contextmanager
def swap_lock(root: Path, timeout: float = 900.0):
    """Serialize swaps for one repo across PROCESSES.

    Two swaps running at once (rapid branch switching, or a checkout hook
    overlapping a commit hook) race inside graphify's own graph writer, which
    writes graph.json as temp-file + rename: one process's temp is consumed by
    the other and the rebuild dies with
    ``WinError 2 ... .graph.tmp.json -> graph.json``. Upstream's per-repo lock
    does not cover this, and the failure is nondeterministic — sometimes the
    fallback recovers, sometimes the swap exits nonzero.

    Held across activate + rebuild + stamp so the whole swap is atomic with
    respect to other swaps. The lock file lives in the cache root, which the
    slot-clearing paths never touch.
    """
    cache_root(root).mkdir(parents=True, exist_ok=True)
    lock_path = cache_root(root) / ".swap.lock"
    handle = open(lock_path, "a+b")
    acquired = False
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                time.sleep(0.25)
        if not acquired:
            # Degrade rather than abandon the swap: a stale graph is worse than
            # a racy rebuild, and the loser still converges on a correct graph.
            print(f"[graphify-ext] WARNING: swap lock busy for {timeout:.0f}s - "
                  f"proceeding without it")
        yield acquired
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _resolve_scan_root() -> Path:
    """Same recovery the stock hook bodies use: honor graphify-out/.graphify_root."""
    saved = Path(OUT_NAME) / ".graphify_root"
    if saved.exists():
        try:
            txt = saved.read_text(encoding="utf-8-sig").strip()
            if txt:
                return Path(txt)
        except OSError:
            pass
    return Path(".")


def _guarded(fn, *args, **kwargs):
    """Run a rebuild callable under the same guards the stock hook bodies apply:
    resource limits + a watchdog honoring GRAPHIFY_REBUILD_TIMEOUT."""
    import signal
    import threading

    from graphify.watch import _apply_resource_limits
    _apply_resource_limits()
    timeout = int(os.environ.get("GRAPHIFY_REBUILD_TIMEOUT", "600"))
    watchdog = None
    if timeout > 0:
        if hasattr(signal, "SIGALRM"):
            signal.signal(
                signal.SIGALRM,
                lambda *_: (_ for _ in ()).throw(
                    TimeoutError(f"graphify rebuild exceeded {timeout}s")),
            )
            signal.alarm(timeout)
        else:
            def _bail():
                print(f"[graphify-ext] rebuild exceeded {timeout}s", flush=True)
                os._exit(1)
            watchdog = threading.Timer(timeout, _bail)
            watchdog.daemon = True
            watchdog.start()
    try:
        return fn(*args, **kwargs)
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
        if watchdog is not None:
            watchdog.cancel()


def _force() -> bool:
    return os.environ.get("GRAPHIFY_FORCE", "").lower() in ("1", "true", "yes")


def _full_rebuild(scan_root: Path) -> bool:
    from graphify.watch import _rebuild_code
    # block_on_lock: a swap is a deliberate, user-visible operation (same
    # posture as the interactive `graphify update` CLI), so it waits for a
    # concurrent rebuild instead of reporting failure. Without this, mere lock
    # contention returns False, which the caller cannot distinguish from a
    # corrupt slot — so it would clear the cache and rebuild for no reason.
    return _guarded(_rebuild_code, scan_root, force=True, block_on_lock=True)


def _changed_set_from_manifest(scan_root: Path, manifest_path: Path) -> list[Path] | None:
    """Files that differ between the slot's cached state and the working tree.

    Uses upstream's own incremental primitive (detect_incremental, kind="ast")
    against the slot's manifest: content hashes are the ground truth, so
    checkout-driven mtime churn on identical files costs one MD5 each, not a
    re-extract. Returns None when the set cannot be computed (=> caller falls
    back to a full rebuild). Honors the same persisted excludes the stock
    rebuild honors.
    """
    try:
        from graphify.detect import detect_incremental
        from graphify.watch import _read_build_excludes, _read_build_gitignore
        out = Path(OUT_NAME)
        excludes = _read_build_excludes(out) or None
        gitignore = _read_build_gitignore(out)
        inc = detect_incremental(
            scan_root,
            manifest_path=str(manifest_path),
            kind="ast",
            extra_excludes=excludes,
            gitignore=gitignore,
        )
    except Exception as exc:
        print(f"[graphify-ext] incremental detection failed ({exc!r}); "
              f"falling back to full rebuild")
        return None
    changed = [Path(f) for lst in inc.get("new_files", {}).values() for f in lst]
    changed += [Path(f) for f in inc.get("deleted_files", [])]
    return changed


def _graph_is_readable(graph_path: Path) -> bool:
    """Is the cached graph actually parseable and shaped like a graph?

    ``has_cache()`` only checks that the file exists and is non-empty, which a
    truncated or corrupt graph.json satisfies. That is enough everywhere the
    rebuild runs anyway (it fails loudly and the caller falls back), but NOT on
    the zero-changed fast path below, which returns without touching the file —
    serving the corrupt graph to every consumer.
    """
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "nodes" in data


def _incremental_update(scan_root: Path, manifest_path: Path) -> bool:
    changed = _changed_set_from_manifest(scan_root, manifest_path)
    if changed is None:
        return _full_rebuild(scan_root)
    if not changed:
        # Nothing to re-extract — but the cached graph is about to be served
        # untouched, so this is the one place that must prove it is usable.
        graph_path = scan_root / OUT_NAME / "graph.json"
        if not _graph_is_readable(graph_path):
            print("[graphify-ext] cached graph is unreadable - rebuilding")
            return False
        print("[graphify-ext] branch cache is current - nothing to update")
        return True
    print(f"[graphify-ext] reconciling {len(changed)} changed file(s) incrementally")
    from graphify.watch import _rebuild_code
    return _guarded(_rebuild_code, scan_root, changed_paths=changed,
                    force=_force(), block_on_lock=True)


def _reapply_external_edges() -> None:
    """Re-apply injected external edges after a rebuild rewrote graph.json.

    Best-effort: the branch cache must keep working without Requirement 2.
    """
    try:
        from graphify_ext.edge_inject import reapply
        n = reapply(Path(OUT_NAME))
        if n:
            print(f"[graphify-ext] re-applied {n} external edge(s)")
    except Exception:
        pass


# ------------------------------------------------------------------ entrypoints

def swap_or_build(branch: str | None = None) -> bool:
    """Post-checkout replacement for the stock full rebuild.

    Runs inside the hook's already-detached child, so everything here is
    synchronous. Swap graphify-out to the target branch's slot, then either
    reconcile incrementally (trusted cache) or rebuild from scratch.
    """
    root = gitutil.git_root() or Path(".")
    with swap_lock(root):
        return _swap_or_build_locked(root, branch)


def _swap_or_build_locked(root: Path, branch: str | None) -> bool:
    os.chdir(root)
    scan_root = _resolve_scan_root()

    if branch is None:
        branch = gitutil.current_branch(root)

    # Copy-mode housekeeping: persist the previous branch's state before the
    # slot swap overwrites the shared real directory. No-op in link mode.
    mirror_back(root)

    if branch is None:
        # Detached HEAD: no stable slot key — dedicated scratch slot, always
        # rebuilt from scratch (by design; see spec fallback triggers).
        slot = cache_root(root) / DETACHED_SLOT
        slot.mkdir(parents=True, exist_ok=True)
        _clear_dir(slot)
        mode = activate(root, slot)
        print("[graphify-ext] detached HEAD - full rebuild into scratch slot")
        ok = _full_rebuild(scan_root)
        if ok:
            if mode == "copy":
                mirror_out_to(root, slot)
            stamp(slot, root, None)
        return ok

    slot = slot_for(root, branch)
    trusted = has_cache(slot) and cache_is_trustworthy(slot, root)
    mode = activate(root, slot)
    print(f"[graphify-ext] activated slot {slot.name} for branch {branch} ({mode} mode)")

    if trusted:
        # In link mode graphify-out/manifest.json IS the slot's manifest via
        # the link; in copy mode activate() just copied it there. Either way
        # the out-dir path is correct and matches what _rebuild_code re-saves.
        ok = _incremental_update(scan_root, scan_root / OUT_NAME / "manifest.json")
        if not ok:
            # A trusted-looking slot can still be unusable: a truncated or
            # corrupt graph.json passes the exists()/size checks but makes the
            # incremental rebuild fail. Never leave a failed reconcile as the
            # final state — that returns nonzero and leaves whatever malformed
            # graph was there in place. Clear the slot and rebuild from
            # scratch, which always converges.
            print("[graphify-ext] incremental reconcile failed - clearing slot "
                  "and falling back to full rebuild")
            try:
                _clear_dir(out_dir(root), keep=frozenset({_OWNER_FILE}))
            except OSError as exc:
                print(f"[graphify-ext] WARNING: could not clear output dir: {exc}")
            ok = _full_rebuild(scan_root)
    else:
        if has_cache(slot):
            print("[graphify-ext] cache not trustworthy "
                  "(history rewritten or graphify version changed) - full rebuild")
        else:
            print(f"[graphify-ext] no cache for branch {branch} - full rebuild")
        ok = _full_rebuild(scan_root)

    if ok:
        _reapply_external_edges()
        if mode == "copy":
            mirror_out_to(root, slot)
        stamp(slot, root, branch)
    return ok


def mirror_out_to(root: Path, slot: Path) -> None:
    """Copy-mode: push the freshly rebuilt graphify-out into its slot."""
    out = out_dir(root)
    if _is_reparse_point(out) or not out.exists():
        return
    _write_owner(out, slot)
    if slot.exists() and any(slot.iterdir()):
        _clear_dir(slot, keep=frozenset({META_NAME}))
    _copy_tree(out, slot)
    try:
        (slot / _OWNER_FILE).unlink()
    except OSError:
        pass


def post_commit_update(changed: list[Path]) -> bool:
    """Post-commit replacement body: stock incremental rebuild + slot stamping.

    Behavior-preserving vs stock (same _rebuild_code call with the hook's
    GRAPHIFY_CHANGED set); adds: self-healing activation (so a fresh clone or
    a manually rebuilt real graphify-out gets adopted into the branch's slot),
    external-edge re-application, and the base-commit stamp.
    """
    if not changed:
        return True
    root = gitutil.git_root() or Path(".")
    # Same serialization as swap_or_build: a commit hook can overlap a checkout
    # hook, and concurrent rebuilds race in graphify's graph writer.
    with swap_lock(root):
        return _post_commit_update_locked(root, changed)


def _post_commit_update_locked(root: Path, changed: list[Path]) -> bool:
    os.chdir(root)
    scan_root = _resolve_scan_root()
    branch = gitutil.current_branch(root)

    mode = "none"
    slot = None
    if branch is not None:
        slot = slot_for(root, branch)
        current = active_slot(root)
        if current is not None and current != slot.resolve():
            # graphify-out points at some other branch's slot: this is the
            # first commit on a branch created with `checkout -b` (no start
            # point => stock+ext hooks fire no rebuild, HEAD unchanged). The
            # new branch's state IS the active state by construction, so seed
            # the new slot from it before swapping — otherwise the incremental
            # update below would start from an empty graph.
            if not has_cache(slot) and has_cache(current):
                _clear_dir(slot) if slot.exists() and any(slot.iterdir()) else None
                _copy_tree(current, slot)
            mode = activate(root, slot)
        elif current is None:
            mode = activate(root, slot)
        else:
            mode = "link"

    print(f"[graphify-ext] {len(changed)} file(s) changed - incremental update")
    from graphify.watch import _rebuild_code
    ok = _guarded(_rebuild_code, scan_root, changed_paths=changed, force=_force())
    if ok:
        _reapply_external_edges()
        if slot is not None:
            if mode == "copy":
                mirror_out_to(root, slot)
            stamp(slot, root, branch)
        _refresh_lessons(scan_root)
    return ok


def _refresh_lessons(scan_root: Path) -> None:
    """Preserve the stock hook's best-effort work-memory reflection step."""
    try:
        md = scan_root / OUT_NAME / "memory"
        if md.is_dir() and any(md.glob("*.md")):
            from graphify.reflect import reflect
            gj = scan_root / OUT_NAME / "graph.json"
            reflect(memory_dir=md,
                    out_path=scan_root / OUT_NAME / "reflections" / "LESSONS.md",
                    graph_path=gj if gj.exists() else None)
    except Exception:
        pass
