"""Bring the graph up to date after an edit, then re-apply the ext layer.

The loop an autonomous agent actually runs is edit → re-read context → edit
again. After the first edit the graph is stale for the edited files: the pack
reports them in ``stale_files`` and refuses mismatched slices, which is the
honest behaviour but leaves the agent with nothing to do about it except shell
out to ``graphify update``. This closes that loop from inside the tool surface.

What it does, in order:

1. ``graphify update`` for the given paths (or every file whose manifest hash
   no longer matches, when none are given) -- the same content-hash
   incremental primitive the hooks use, imported from the installed package
   when possible and run as a subprocess otherwise;
2. ``supplement.reapply`` (only if the slot opted in);
3. ``edge_inject.reapply`` (only if findings are stored).

It never decides on its own to do a full rebuild: a full re-extraction is a
different cost class and stays an explicit user action.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from graphify_ext import edge_inject, graphio, supplement


def stale_paths(out_dir: Path, root: Path) -> list[str]:
    """Files whose current MD5 differs from graphify's manifest, plus files in
    the manifest that no longer exist (so deletions propagate too)."""
    mp = Path(out_dir) / "manifest.json"
    if not mp.exists():
        return []
    try:
        manifest = graphio.read_json(mp)
    except Exception:
        return []
    if not isinstance(manifest, dict):
        return []
    out: list[str] = []
    for rel, entry in manifest.items():
        if not isinstance(entry, dict):
            continue
        recorded = str(entry.get("ast_hash") or entry.get("hash") or "")
        p = Path(root) / rel
        if not p.is_file():
            out.append(rel)
            continue
        if not recorded:
            continue
        h = hashlib.md5(usedforsecurity=False)
        try:
            h.update(p.read_bytes())
        except OSError:
            continue
        if h.hexdigest() != recorded:
            out.append(rel)
    return sorted(out)


def _incremental_update(root: Path, paths: list[str]) -> dict:
    """Run graphify's incremental rebuild for ``paths``. Returns a small report."""
    changed = [Path(p) for p in paths]
    try:
        from graphify.watch import _rebuild_code  # type: ignore
    except Exception:
        _rebuild_code = None
    if _rebuild_code is not None:
        try:
            _rebuild_code(Path(root), changed_paths=changed)
            return {"method": "graphify.watch._rebuild_code", "ok": True}
        except TypeError:
            pass  # signature drift; fall through to the CLI
        except Exception as exc:  # report, then try the CLI
            err = f"{type(exc).__name__}: {exc}"
        else:
            err = None
    else:
        err = "graphify not importable from this interpreter"
    cmd = ["graphify", "update", "."]
    try:
        r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"method": "graphify update (cli)", "ok": False,
                "error": f"{type(exc).__name__}: {exc}", "import_error": err}
    return {"method": "graphify update (cli)", "ok": r.returncode == 0,
            "stdout_tail": r.stdout[-600:], "stderr_tail": r.stderr[-600:],
            "import_error": err}


def refresh(out_dir: Path, root: Path | None = None,
            paths: list[str] | None = None) -> dict:
    """Incremental graph update for ``paths`` (default: every stale file), then
    re-application of supplement and injected edges. Returns a report."""
    out_dir = Path(out_dir)
    root = Path(root) if root is not None else graphio.repo_root_for(out_dir / "graph.json")
    if not (out_dir / "graph.json").exists():
        return {"ok": False, "error": f"no graph at {out_dir / 'graph.json'}"}
    targets = list(paths) if paths else stale_paths(out_dir, root)
    report: dict = {"root": str(root), "paths": targets}
    if not targets:
        report.update({"ok": True, "updated": False,
                       "note": "nothing stale: every manifest hash matches the working tree"})
    else:
        upd = _incremental_update(root, targets)
        report["update"] = upd
        report["updated"] = bool(upd.get("ok"))
        report["ok"] = bool(upd.get("ok"))
    try:
        sup = supplement.reapply(out_dir)
        report["supplement"] = ({"added_nodes": sup["added_nodes"],
                                 "added_edges": sup["added_edges"],
                                 "stale_files": sup["stale_files"]}
                                if sup else "not enabled for this slot")
    except Exception as exc:
        report["supplement"] = f"failed: {type(exc).__name__}: {exc}"
    try:
        n = edge_inject.reapply(out_dir)
        report["external_edges_reapplied"] = n
    except Exception as exc:
        report["external_edges_reapplied"] = f"failed: {type(exc).__name__}: {exc}"
    report["still_stale"] = stale_paths(out_dir, root)
    if report["still_stale"] and report.get("updated"):
        report["ok"] = False
        report["note"] = ("update ran but these files still do not match the manifest; "
                          "graphify may have skipped them (unsupported type) or the "
                          "update failed silently -- see update.stdout_tail")
    return report


def format_report(rep: dict) -> str:
    lines = []
    if not rep.get("ok", False) and rep.get("error"):
        return f"refresh: {rep['error']}"
    if rep.get("paths"):
        lines.append(f"refresh: {len(rep['paths'])} file(s) -> "
                     f"{'updated' if rep.get('updated') else 'UPDATE FAILED'} "
                     f"via {rep.get('update', {}).get('method', '?')}")
        for p in rep["paths"][:10]:
            lines.append(f"  {p}")
        if len(rep["paths"]) > 10:
            lines.append(f"  ... and {len(rep['paths']) - 10} more")
    else:
        lines.append("refresh: " + rep.get("note", "nothing to do"))
    sup = rep.get("supplement")
    if isinstance(sup, dict):
        lines.append(f"  supplement re-applied: {sup['added_nodes']} node(s), "
                     f"{sup['added_edges']} edge(s)"
                     + (f"; {len(sup['stale_files'])} file(s) still refused as stale"
                        if sup["stale_files"] else ""))
    else:
        lines.append(f"  supplement: {sup}")
    lines.append(f"  external edges re-applied: {rep.get('external_edges_reapplied')}")
    if rep.get("still_stale"):
        lines.append(f"!!! still stale after refresh ({len(rep['still_stale'])}): "
                     + ", ".join(rep["still_stale"][:8]))
        if rep.get("note"):
            lines.append("    " + rep["note"])
    return "\n".join(lines)


if __name__ == "__main__":  # manual smoke: python -m graphify_ext.refresh [paths]
    print(format_report(refresh(Path("graphify-out"), paths=sys.argv[1:] or None)))
