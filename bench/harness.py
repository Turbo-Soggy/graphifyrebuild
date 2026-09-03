"""Shared harness for the graphify-ext vs stock differential tests.

Design choices that matter for trusting the results:

* **One sandbox, sequential runs.** Stock and ext are measured in the SAME
  directory rather than two parallel clones, so `source_file` values and node
  ids are identical by construction and a graph diff needs no path
  normalisation.
* **Upstream's own equality.** Graph comparison uses
  ``graphify.watch._canonical_graph_for_compare`` — the function upstream uses
  to decide whether a rebuild changed anything. Inventing our own definition of
  "identical" would let a real difference hide behind a lenient comparator.
* **Hook firing is detected synchronously.** Both stock and ext post-checkout
  hooks echo a line *before* launching the detached rebuild, so invoking the
  hook script directly with git's argv and reading stdout tells us whether the
  guards let it through — no sleeping, no polling, no race.
* **AST-only throughout.** Every build/rebuild path measured here is the
  no-LLM code path (`_rebuild_code`, `extract --code-only`), which is what the
  hooks actually run. Mixing in semantic extraction would make timings
  depend on network latency.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
VENV_SCRIPTS = Path(sys.executable).parent
GRAPHIFY = VENV_SCRIPTS / ("graphify.exe" if os.name == "nt" else "graphify")
GRAPHIFY_EXT = VENV_SCRIPTS / ("graphify-ext.exe" if os.name == "nt" else "graphify-ext")

OUT = "graphify-out"
CACHE = ".graphify-cache"


# ----------------------------------------------------------------- git helpers

def git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


# --------------------------------------------------------------- fs utilities

def _on_rm_error(func, path, exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def rmtree(p: Path) -> None:
    """rmtree that detaches links first and clears read-only git objects."""
    if not p.exists() and not os.path.islink(p):
        return
    if is_link(p):
        remove_link(p)
        return
    for child in sorted(p.rglob("*"), key=lambda x: -len(x.parts)):
        if is_link(child):
            remove_link(child)
    shutil.rmtree(p, onexc=_on_rm_error)


def is_link(p: Path) -> bool:
    if os.path.islink(p):
        return True
    if os.name != "nt":
        return False
    try:
        return bool(getattr(os.lstat(p), "st_reparse_tag", 0))
    except OSError:
        return False


def remove_link(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        try:
            os.rmdir(p)
        except OSError:
            pass


def source_dir(repo: Path) -> Path:
    """The directory holding the most production .py files.

    Repo-agnostic on purpose: flask keeps sources in src/flask, scrapy in
    scrapy/. Hardcoding either makes the suite silently touch nothing on the
    other repo — the change set would be empty and every "incremental" timing
    would measure a no-op.
    """
    best_dir, best_count = None, 0
    skip = {".git", "graphify-out", ".graphify-cache", "tests", "test",
            "docs", "examples", ".venv", "node_modules", "__pycache__"}
    for d in [repo, *[p for p in repo.rglob("*") if p.is_dir()]]:
        if any(part in skip for part in d.relative_to(repo).parts):
            continue
        n = len([f for f in d.glob("*.py")])
        if n > best_count:
            best_dir, best_count = d, n
    if best_dir is None:
        raise RuntimeError(f"no python source directory found in {repo}")
    return best_dir


def source_files(repo: Path, n: int) -> list[Path]:
    """``n`` production .py files to modify for a realistic branch difference."""
    files = sorted(source_dir(repo).glob("*.py"))
    if len(files) < n:
        raise RuntimeError(f"only {len(files)} .py files in {source_dir(repo)}")
    return files[:n]


def norm_rel(repo: Path, p: Path) -> str:
    """Repo-relative, forward-slash path — the form graphify's hooks pass."""
    return p.resolve().relative_to(repo.resolve()).as_posix()


def dir_size(p: Path) -> int:
    if not p.exists():
        return 0
    total = 0
    for f in p.rglob("*"):
        if f.is_file() and not is_link(f):
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


# ------------------------------------------------------------ sandbox control

def reset_graph_state(repo: Path) -> None:
    """Remove all graph artifacts: output dir (or link) and the branch cache."""
    out = repo / OUT
    if is_link(out):
        remove_link(out)
    elif out.exists():
        rmtree(out)
    cache = repo / CACHE
    if cache.exists():
        rmtree(cache)


BASE_TAG = "bench-base"
DEFAULT_BRANCH_FILE = "bench-default-branch"


def default_branch(repo: Path) -> str:
    """The sandbox's default branch — NOT assumed to be 'main'.

    Flask uses 'main', scrapy uses 'master', and hardcoding either makes the
    suite silently reset to nothing on the other (checkout failures are
    captured, so the run continues on a polluted tree). setup_sandbox.py
    records the real name at clone time; the fallbacks cover a sandbox created
    before that existed.
    """
    recorded = repo / ".git" / DEFAULT_BRANCH_FILE
    if recorded.is_file():
        name = recorded.read_text(encoding="utf-8-sig").strip()
        if name:
            return name

    # origin/HEAD before current HEAD. reset_repo() calls this to decide which
    # branch to KEEP and which to delete, and it runs while a feature branch is
    # checked out — so deriving the answer from current HEAD makes it keep the
    # feature branch and DELETE the real default. That is not hypothetical: it
    # destroyed the scrapy sandbox's `master`.
    origin = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True).stdout.strip()
    if origin:
        return origin.split("/", 1)[-1]

    for cand in ("main", "master"):
        if subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", cand],
                          capture_output=True).returncode == 0:
            return cand

    cur = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    if cur and cur != "HEAD":
        return cur
    raise RuntimeError(f"cannot determine default branch for {repo}")


def base_commit(repo: Path) -> str:
    """The pinned pristine baseline (see setup_sandbox.py).

    Deliberately NOT ``head(repo)``: the B-series creates branches, amends
    commits and diverges history, so a baseline captured at run time is
    whatever the PREVIOUS run left behind. Resetting to that silently
    invalidates every subsequent measurement — which is exactly the bug that
    produced two contradictory speedup numbers before this tag existed.
    """
    out = subprocess.run(["git", "-C", str(repo), "rev-list", "-n", "1", BASE_TAG],
                         capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(
            f"{repo} has no '{BASE_TAG}' tag — run bench/setup_sandbox.py first. "
            "Benchmarks must reset to a pinned pristine commit, not to HEAD.")
    return out.stdout.strip()


def assert_pristine(repo: Path) -> None:
    """Fail loudly if the working tree still carries benchmark artifacts."""
    stray = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    stray_lines = [ln for ln in stray.splitlines()
                   if OUT not in ln and CACHE not in ln]
    if stray_lines:
        raise RuntimeError(f"{repo} is not pristine after reset: {stray_lines[:5]}")
    behind = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    if behind != base_commit(repo):
        raise RuntimeError(
            f"{repo} HEAD {behind[:8]} != baseline {base_commit(repo)[:8]}")


def reset_repo(repo: Path, base_commit: str, keep_branches: tuple[str, ...] = ()) -> None:
    """Return the sandbox to a pristine git + graph state."""
    main = default_branch(repo)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-f", main],
                   capture_output=True)
    for line in git(repo, "branch", "--format=%(refname:short)").splitlines():
        name = line.strip()
        if name and name != main and name not in keep_branches:
            subprocess.run(["git", "-C", str(repo), "branch", "-q", "-D", name],
                           capture_output=True)
    subprocess.run(["git", "-C", str(repo), "reset", "-q", "--hard", base_commit],
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "clean", "-qfd", "-e", OUT,
                    "-e", CACHE], capture_output=True)
    reset_graph_state(repo)


def ensure_excludes(repo: Path) -> None:
    """Keep graph artifacts out of git via .git/info/exclude (untracked file,
    so it never dirties the worktree and never blocks a checkout)."""
    ex = repo / ".git" / "info" / "exclude"
    ex.parent.mkdir(parents=True, exist_ok=True)
    existing = ex.read_text(encoding="utf-8-sig") if ex.exists() else ""
    lines = {ln.strip() for ln in existing.splitlines()}
    want = [OUT, f"{OUT}/", f"{CACHE}/"]
    missing = [w for w in want if w not in lines]
    if missing:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        ex.write_text(existing + "\n".join(missing) + "\n", encoding="utf-8")


# --------------------------------------------------------------- graph compare

def load_graph(repo: Path) -> dict | None:
    p = repo / OUT / "graph.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def strip_ext_edges(graph: dict) -> dict:
    """Drop edges this build injected, so a stock-vs-ext diff compares only
    what the shared extractor produced."""
    g = dict(graph)
    for key in ("links", "edges"):
        if key in g and isinstance(g[key], list):
            g[key] = [e for e in g[key] if e.get("origin") != "graphify-ext"]
    return g


def canonical(graph: dict) -> dict:
    """Upstream's own canonical form (sorts collections, drops built_at_commit)."""
    from graphify.watch import _canonical_graph_for_compare
    return _canonical_graph_for_compare(strip_ext_edges(graph))


def graphs_identical(a: dict, b: dict) -> tuple[bool, str]:
    ca, cb = canonical(a), canonical(b)
    if ca == cb:
        return True, "identical"
    diffs = []
    for key in ("nodes", "links", "edges"):
        la = {json.dumps(x, sort_keys=True, default=str) for x in ca.get(key, [])}
        lb = {json.dumps(x, sort_keys=True, default=str) for x in cb.get(key, [])}
        if la != lb:
            diffs.append(f"{key}: {len(la - lb)} only-in-A, {len(lb - la)} only-in-B")
    other = [k for k in set(ca) | set(cb)
             if k not in ("nodes", "links", "edges") and ca.get(k) != cb.get(k)]
    if other:
        diffs.append("differing keys: " + ", ".join(sorted(other)))
    return False, "; ".join(diffs) or "unequal (no structural diff located)"


def node_edge_counts(graph: dict | None) -> tuple[int, int]:
    if not graph:
        return (0, 0)
    edges = graph.get("links", graph.get("edges", []))
    return len(graph.get("nodes", [])), len(edges)


# -------------------------------------------------------------------- timing

def timed(fn, *args, **kwargs) -> tuple[float, object]:
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    return time.perf_counter() - t0, result


def run(cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.setdefault("PYTHONHASHSEED", "0")
    e.setdefault("GRAPHIFY_MAX_WORKERS", "1")
    if env:
        e.update(env)
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, env=e)


# ------------------------------------------------------- graphify operations

def stock_full_build(repo: Path) -> subprocess.CompletedProcess:
    """Stock full build, AST-only (no API key needed)."""
    return run([str(GRAPHIFY), ".", "--code-only"], repo)


def stock_rebuild_full(repo: Path) -> subprocess.CompletedProcess:
    """Exactly what stock's post-checkout hook does: full-corpus _rebuild_code."""
    return run([sys.executable, "-c",
                "from pathlib import Path;from graphify.watch import _rebuild_code;"
                "import sys;sys.exit(0 if _rebuild_code(Path('.'), force=True) else 1)"],
               repo)


def stock_rebuild_incremental(repo: Path, changed: list[str]) -> subprocess.CompletedProcess:
    """Exactly what stock's post-commit hook does: _rebuild_code(changed_paths=...)."""
    payload = ",".join(repr(c) for c in changed)
    return run([sys.executable, "-c",
                "from pathlib import Path;from graphify.watch import _rebuild_code;"
                f"import sys;sys.exit(0 if _rebuild_code(Path('.'), changed_paths=[Path(p) for p in [{payload}]]) else 1)"],
               repo)


def ext_swap(repo: Path, branch: str | None = None) -> subprocess.CompletedProcess:
    args = [str(GRAPHIFY_EXT), "swap"]
    if branch:
        args += ["--branch", branch]
    return run(args, repo)


# ------------------------------------------------------------- hook execution

def hook_path(repo: Path, name: str) -> Path:
    return repo / ".git" / "hooks" / name


def sh() -> str:
    for cand in (r"C:\Program Files\Git\bin\sh.exe", "/bin/sh", "sh"):
        if Path(cand).exists() or cand == "sh":
            return cand
    return "sh"


def invoke_hook(repo: Path, name: str, *argv: str,
                env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a git hook script directly with the argv git would pass.

    Both stock and ext hooks echo a launch line BEFORE spawning the detached
    rebuild, so stdout is a synchronous, race-free signal of whether the hook's
    guards allowed it to proceed.

    Pass ``env={"GRAPHIFY_OUT": "<throwaway>"}`` for a positive-control run so
    the rebuild the hook spawns writes somewhere harmless.
    """
    p = hook_path(repo, name)
    if not p.exists():
        raise FileNotFoundError(f"no {name} hook at {p}")
    e = dict(os.environ)
    e["GIT_DIR"] = str(repo / ".git")
    e.setdefault("PYTHONHASHSEED", "0")
    if env:
        e.update(env)
    return subprocess.run([sh(), str(p), *argv], cwd=str(repo),
                          capture_output=True, text=True, env=e)


def wait_for_rebuild(out_dir: Path, timeout: float = 180.0) -> bool:
    """Wait for a spawned detached rebuild to finish writing ``out_dir``.

    Polls for graph.json to appear and stop growing; returns False on timeout.
    Used after a positive-control hook fire so cleanup never races the child.
    """
    graph = out_dir / "graph.json"
    deadline = time.time() + timeout
    last, stable = -1, 0
    while time.time() < deadline:
        size = graph.stat().st_size if graph.exists() else -1
        if size >= 0 and size == last:
            stable += 1
            if stable >= 3:
                return True
        else:
            stable = 0
        last = size
        time.sleep(0.5)
    return False


HOOK_FIRED_MARKERS = ("launching background rebuild", "Branch switched")


def hook_fired(result: subprocess.CompletedProcess) -> bool:
    out = (result.stdout or "") + (result.stderr or "")
    return any(m in out for m in HOOK_FIRED_MARKERS)


def install_stock_hooks(repo: Path) -> str:
    return run([str(GRAPHIFY), "hook", "install"], repo).stdout


def install_ext_hooks(repo: Path) -> str:
    return run([str(GRAPHIFY_EXT), "hook", "install"], repo).stdout


def uninstall_hooks(repo: Path) -> None:
    run([str(GRAPHIFY), "hook", "uninstall"], repo)
    for name in ("post-commit", "post-checkout"):
        p = hook_path(repo, name)
        if p.exists():
            p.unlink()


# ------------------------------------------------------------------- results

class Results:
    def __init__(self, title: str):
        self.title = title
        self.rows: list[tuple[str, str, bool, str]] = []

    def check(self, case: str, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((case, name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  [{case}] {name}"
              + (f" — {detail}" if detail else ""))
        return ok

    def info(self, case: str, name: str, detail: str) -> None:
        self.rows.append((case, name, True, detail))
        print(f"  INFO  [{case}] {name} — {detail}")

    def summary(self) -> int:
        failed = [r for r in self.rows if not r[2]]
        print(f"\n== {self.title} summary ==")
        for case, name, ok, detail in self.rows:
            print(f"  {'PASS' if ok else 'FAIL'}  [{case}] {name}"
                  + (f" — {detail}" if detail else ""))
        print(f"\n{len(self.rows) - len(failed)}/{len(self.rows)} checks passed")
        return 1 if failed else 0
