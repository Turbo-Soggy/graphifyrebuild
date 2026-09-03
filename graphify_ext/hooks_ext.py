"""Hook installer for the per-branch cache (Requirement 1, step 5).

Vendored-wrapper approach rather than editing the installed package in place:
``uv tool upgrade graphifyy`` overwrites site-packages, but the hook SCRIPTS it
generated live in ``.git/hooks`` and survive upgrades. This module composes
its own hook scripts from upstream's exported building blocks
(``_PYTHON_DETECT`` interpreter probe, ``_WORKTREE_GUARD``, the cross-platform
Python detached launcher — the one that already fixed the nohup-on-Git-for-
Windows failure, #1161) and swaps only the rebuild BODIES:

* post-checkout: calls ``graphify_ext.branch_cache.swap_or_build()`` instead
  of the stock unconditional full ``_rebuild_code(Path('.'))``.
* post-commit:   calls ``graphify_ext.branch_cache.post_commit_update()`` —
  the same incremental ``_rebuild_code(changed_paths=...)`` as stock, plus
  slot stamping and external-edge re-application.

The stock markers are reused deliberately, so exactly one graphify block ever
exists in each hook: installing this replaces a stock block in place, and
re-running stock ``graphify hook install`` reverts to stock (re-run
``graphify-ext hook install`` to re-apply). Both directions are clean.

Launcher constraint (from upstream ``_LAUNCHER_TEMPLATE``): the rebuild bodies
are carried inside a shell double-quoted ``-c "..."`` argument and a Python
triple-single-quoted string, so they must not contain double quotes, ``$``,
backticks, or backslashes — single-quoted Python strings only.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_CHECKOUT_BODY_EXT = """\
import sys
try:
    from graphify_ext.branch_cache import swap_or_build
    ok = swap_or_build()
    sys.exit(0 if ok else 1)
except Exception as exc:
    print('[graphify-ext] swap_or_build failed: ' + repr(exc))
    sys.exit(1)
"""

_COMMIT_BODY_EXT = """\
import os, sys
from pathlib import Path
try:
    from graphify_ext.branch_cache import post_commit_update
    changed_raw = os.environ.get('GRAPHIFY_CHANGED', '')
    changed = [Path(f.strip()) for f in changed_raw.strip().splitlines() if f.strip()]
    ok = post_commit_update(changed)
    sys.exit(0 if ok else 1)
except Exception as exc:
    print('[graphify-ext] post-commit update failed: ' + repr(exc))
    sys.exit(1)
"""

_EXT_STAMP = "# graphify-ext-customized\n"


def _compose_scripts():
    """Build the two hook scripts from upstream templates with our bodies.

    Uses upstream's own composition: take the stock script constants and
    replace the stock detached-launch line (whose payload embeds the stock
    rebuild body) with one carrying our body. The surrounding shell scaffold
    (rebase/merge guards, worktree guard, GRAPHIFY_CHANGED computation,
    interpreter probes, logging) is inherited verbatim.
    """
    from graphify import hooks as up

    commit = up._HOOK_SCRIPT.replace(
        up._detached_launch(up._REBUILD_BODY_COMMIT),
        _EXT_STAMP + up._detached_launch(_COMMIT_BODY_EXT),
    )
    checkout = up._CHECKOUT_SCRIPT.replace(
        up._detached_launch(up._REBUILD_BODY_CHECKOUT),
        _EXT_STAMP + up._detached_launch(_CHECKOUT_BODY_EXT),
    )
    for name, script, stock in (
        ("post-commit", commit, up._HOOK_SCRIPT),
        ("post-checkout", checkout, up._CHECKOUT_SCRIPT),
    ):
        if script == stock:
            raise RuntimeError(
                f"could not splice graphify-ext body into the {name} template — "
                f"upstream graphify hook layout changed; update graphify_ext.hooks_ext"
            )
    # The stock checkout hook exits early when graphify-out/ is missing
    # ("graph has been built before" guard). With per-branch slots the state
    # may live in .graphify-cache/ while graphify-out is absent (e.g. deleted
    # link), so widen the guard to accept either.
    checkout = checkout.replace(
        'if [ ! -d "graphify-out" ]; then',
        'if [ ! -e "graphify-out" ] && [ ! -d ".graphify-cache" ]; then',
    )
    return commit, checkout


def _verify_ext_importable(root: Path) -> str | None:
    """Warn if the interpreter the hooks will pin cannot import graphify_ext."""
    from graphify.hooks import _pinned_python
    pinned = _pinned_python()
    if not pinned:
        return None
    probe = "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('graphify_ext') else 1)"
    try:
        r = subprocess.run([pinned, "-c", probe], capture_output=True, timeout=30)
        if r.returncode != 0:
            return (
                f"WARNING: {pinned} cannot import graphify_ext. Install it into the "
                f"same environment as graphifyy (e.g. `uv tool install graphifyy "
                f"--with graphify-ext` or `pip install -e .` with that interpreter), "
                f"or the hooks will fail at git-trigger time (check the rebuild log)."
            )
    except Exception:
        return None
    return None


def install(path: Path = Path(".")) -> str:
    if importlib.util.find_spec("graphify") is None:
        raise RuntimeError(
            "graphifyy is not importable from this interpreter; install it first "
            "(the hook templates are composed from its hooks module)"
        )
    from graphify import hooks as up

    root = up._git_root(path)
    if root is None:
        raise RuntimeError(f"No git repository found at or above {path.resolve()}")
    hooks_dir = up._user_hooks_dir(up._hooks_dir(root))

    cfg = up._load_graphifyrc(root)
    viz_limit = cfg.get("viz_node_limit")
    viz_export = (
        f'export GRAPHIFY_VIZ_NODE_LIMIT="${{GRAPHIFY_VIZ_NODE_LIMIT:-{viz_limit}}}"\n'
        if viz_limit is not None else ""
    )

    commit_t, checkout_t = _compose_scripts()
    pinned = up._pinned_python()
    commit = commit_t.replace("__PINNED_PYTHON__", pinned).replace("__VIZ_LIMIT_EXPORT__", viz_export)
    checkout = checkout_t.replace("__PINNED_PYTHON__", pinned).replace("__VIZ_LIMIT_EXPORT__", viz_export)

    # Reuse the stock markers (see module docstring): a stock block gets
    # replaced in place, and only one graphify block ever exists per hook.
    commit_msg = up._install_hook(hooks_dir, "post-commit", commit,
                                  up._HOOK_MARKER, up._HOOK_MARKER_END)
    checkout_msg = up._install_hook(hooks_dir, "post-checkout", checkout,
                                    up._CHECKOUT_MARKER, up._CHECKOUT_MARKER_END)
    merge_msg = up._register_merge_driver(root)

    lines = [
        f"post-commit: {commit_msg}",
        f"post-checkout: {checkout_msg}",
        f"merge driver: {merge_msg}",
    ]
    warn = _verify_ext_importable(root)
    if warn:
        lines.append(warn)
    return "\n".join(lines)


def uninstall(path: Path = Path(".")) -> str:
    """Remove the hooks entirely (stock uninstaller handles our blocks too,
    since we share its markers)."""
    from graphify import hooks as up
    return up.uninstall(path)


def status(path: Path = Path(".")) -> str:
    from graphify import hooks as up
    base = up.status(path)
    root = up._git_root(path)
    if root is None:
        return base
    hooks_dir = up._user_hooks_dir(up._hooks_dir(root))
    flavors = []
    for name in ("post-commit", "post-checkout"):
        p = hooks_dir / name
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if _EXT_STAMP.strip() in text:
            flavors.append(f"{name}: graphify-ext variant")
        elif up._HOOK_MARKER in text or up._CHECKOUT_MARKER in text:
            flavors.append(f"{name}: STOCK variant (run 'graphify-ext hook install' to customize)")
    return base + ("\n" + "\n".join(flavors) if flavors else "")
