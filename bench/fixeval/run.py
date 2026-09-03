"""End-to-end fix evaluation: does an agent FIX the bug from this context?

Everything else in bench/ measures retrieval. This measures the thing retrieval
is a proxy for, SWE-bench style, on the same frozen corpus:

  1. For a fix commit C with parent P, take ONLY the test files C changed. Apply
     that test diff to the tree at P and run those files. The tests that fail
     are the task's FAIL_TO_PASS set; the ones that pass must keep passing.
     A task with no failing test is not verifiable and is dropped, not scored.
  2. Hand a headless Claude Code agent (`claude -p`) the tree at P -- copied
     without .git (so the future commit cannot be read) and without
     graphify-out -- plus the commit message as the problem statement. Tools:
     Read, Edit, Write, Grep, Glob. No Bash: it cannot run tests it has not
     seen, and it cannot consult git.
  3. Two arms, identical except for one thing: the `graph` arm's prompt also
     carries the `graphify-ext context` pack for the task's entry symbol
     (shipped defaults). The `nograph` arm finds its own way with Grep/Read.
  4. Apply the test diff to the agent's tree, run the same files, score:
     resolved = every FAIL_TO_PASS test passes AND no PASS_TO_PASS test broke.

Per run we also record turns, cost, wall time and the files the agent edited,
because "resolved" alone hides how much of the budget the agent spent finding
the code -- the cost the pack exists to remove.

    python run.py select                 # build tasks.json: verifiable tasks + envs
    python run.py run [--arm graph|nograph|both] [--task requests/db575eee ...]
    python run.py report
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENTCTX = HERE.parent / "agentctx"
sys.path.insert(0, str(AGENTCTX))
sys.path.insert(0, str(HERE.parents[1]))
from evaluate import REPO_PATHS, prepare, resolve_entry, wt_for  # noqa: E402

TASKS = HERE / "tasks.json"
RESULTS = HERE / "results.jsonl"
VENVS = HERE / "venvs"
WORK = HERE / "work"
MODEL = os.environ.get("FIXEVAL_MODEL", "sonnet")
MAX_TURNS = int(os.environ.get("FIXEVAL_MAX_TURNS", "30"))
MAX_USD = os.environ.get("FIXEVAL_MAX_USD", "1.50")
VENV_PYTHON = os.environ.get("FIXEVAL_PYTHON", "py -3.11").split()

# Era pins for python worktrees: pip resolves the package's own pins, but the
# packages it depends on have since made breaking releases the old pins do not
# exclude. Chosen by commit year; wrong pins show up as import errors in
# `select`, which then drops the task rather than scoring it.
FLASK_PINS = {
    2022: ["werkzeug<2.3", "jinja2<3.2", "click<8.2", "itsdangerous<2.2", "pytest<8"],
    2023: ["werkzeug<2.3", "jinja2<3.2", "click<8.2", "pytest<8"],
    2024: ["werkzeug<3.1"],
}
REQUESTS_EXTRAS = ["pytest-httpbin", "pytest-mock", "trustme", "pysocks"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=True).stdout


def load_corpus() -> list[dict]:
    return json.loads((AGENTCTX / "corpus.json").read_text(encoding="utf-8"))


def task_key(t: dict) -> str:
    return f"{t['repo']}/{t['commit'][:8]}"


def changed_test_files(t: dict) -> list[str]:
    repo = REPO_PATHS[t["repo"]]
    files = git(repo, "diff", "--name-only", t["parent"], t["commit"]).split()
    tests = [f for f in files if re.search(r"(^|/)(tests?|__tests__|spec)(/|$)|test_|_test\.|\.test\.", f)]
    if t["repo"] == "express":
        tests = [f for f in tests if f.endswith(".js")]
    else:
        tests = [f for f in tests if f.endswith(".py")]
    return tests


def test_patch(t: dict, files: list[str]) -> str:
    repo = REPO_PATHS[t["repo"]]
    return git(repo, "diff", t["parent"], t["commit"], "--", *files)


def commit_year(t: dict) -> int:
    return int(git(REPO_PATHS[t["repo"]], "show", "-s", "--format=%cs", t["commit"])[:4])


# --------------------------------------------------------------------------- envs

def python_env(t: dict, tree: Path) -> Path:
    """A venv with the worktree's package installed (non-editable copy is not
    needed: tests run from the tree with the tree first on sys.path)."""
    venv = VENVS / task_key(t).replace("/", "-")
    py = venv / "Scripts" / "python.exe"
    if not py.exists():
        # The corpus spans 2016-2026; 2022-era flask still calls
        # pkgutil.get_loader, which Python 3.12 turns into a RuntimeError and
        # 3.14 removes. One interpreter for every python task keeps the
        # comparison even: 3.11 runs all of them.
        subprocess.run(VENV_PYTHON + ["-m", "venv", str(venv)], check=True)
        pins: list[str] = []
        if t["repo"] == "flask":
            y = commit_year(t)
            pins = FLASK_PINS.get(y, [] if y > 2024 else FLASK_PINS[2022])
            extras = ["asgiref", "python-dotenv", "greenlet"]
        else:
            extras = REQUESTS_EXTRAS
        r = subprocess.run([str(py), "-m", "pip", "install", "-q", "-e", str(tree),
                            "pytest<9", *pins, *extras], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            (venv / "install.err").write_text(r.stderr, encoding="utf-8")
    return venv


def run_pytest(t: dict, tree: Path, files: list[str]) -> dict:
    """{test_id: 'PASSED'|'FAILED'|'ERROR'}; {} plus error text when nothing ran."""
    py = python_env(t, tree) / "Scripts" / "python.exe"
    env = dict(os.environ)
    src = tree / "src"
    env["PYTHONPATH"] = str(src) + os.pathsep + str(tree) if src.exists() else str(tree)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    r = subprocess.run([str(py), "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA",
                        "--no-header", "-x" if False else "--continue-on-collection-errors",
                        *files], cwd=str(tree), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                       timeout=1800)
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        m = re.match(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED) (\S+?)(?: - .*)?$", line.strip())
        if m and "::" in m.group(2):
            out[m.group(2)] = m.group(1)
    return {"tests": out, "exit": r.returncode,
            "tail": (r.stdout[-1500:] + r.stderr[-800:]) if not out else ""}


def node_env(tree: Path) -> bool:
    if (tree / "node_modules").exists():
        return True
    r = subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--loglevel=error",
                        "--ignore-scripts"], cwd=str(tree), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       shell=True, timeout=1800)
    return (tree / "node_modules").exists()


def run_mocha(tree: Path, files: list[str]) -> dict:
    if not node_env(tree):
        return {"tests": {}, "exit": 1, "tail": "npm install failed"}
    r = subprocess.run(["npx", "mocha", "--reporter", "json", "--require", "test/support/env",
                        *files], cwd=str(tree), capture_output=True, text=True, encoding="utf-8", errors="replace", shell=True,
                       timeout=1800)
    text = r.stdout
    start = text.find("{")
    out: dict[str, str] = {}
    try:
        d = json.loads(text[start:])
        for p in d.get("passes", []):
            out[p["fullTitle"]] = "PASSED"
        for f in d.get("failures", []):
            out[f["fullTitle"]] = "FAILED"
        for f in d.get("pending", []):
            out[f["fullTitle"]] = "PENDING"
    except Exception:
        # env require may not exist in old trees; retry without it
        r = subprocess.run(["npx", "mocha", "--reporter", "json", *files], cwd=str(tree),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", shell=True, timeout=1800)
        text = r.stdout
        start = text.find("{")
        try:
            d = json.loads(text[start:])
            for p in d.get("passes", []):
                out[p["fullTitle"]] = "PASSED"
            for f in d.get("failures", []):
                out[f["fullTitle"]] = "FAILED"
        except Exception:
            return {"tests": {}, "exit": r.returncode, "tail": (r.stdout + r.stderr)[-1500:]}
    return {"tests": out, "exit": r.returncode, "tail": ""}


def run_tests(t: dict, tree: Path, files: list[str]) -> dict:
    return run_mocha(tree, files) if t["repo"] == "express" else run_pytest(t, tree, files)


# --------------------------------------------------------------------------- trees

def fresh_tree(t: dict, dest: Path) -> Path:
    """Copy of the worktree at P without .git or graphify-out (or node_modules,
    which is re-linked rather than copied)."""
    wt, _ = prepare(t)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(wt, dest, ignore=shutil.ignore_patterns(
        ".git", "graphify-out", "node_modules", "__pycache__", ".pytest_cache", "*.egg-info"))
    nm = wt / "node_modules"
    if nm.exists():
        shutil.copytree(nm, dest / "node_modules", symlinks=True)
    # The copy lives under THIS repository's tree. `git apply` run from a
    # subdirectory of a repo resolves patch paths against that repo's root and
    # silently ignores anything outside the subdirectory -- so every test diff
    # "applied" with exit 0 and changed nothing. An empty repo of its own makes
    # the copy the root. It has no commits, so nothing about the fix leaks.
    subprocess.run(["git", "init", "-q"], cwd=str(dest), capture_output=True)
    return dest


def apply_patch(tree: Path, patch: str) -> bool:
    (tree / ".fixeval.patch").write_text(patch, encoding="utf-8")
    # --ignore-whitespace: worktrees checked out with autocrlf carry CRLF while
    # the diff carries LF (requests); without it every hunk "does not apply".
    r = subprocess.run(["git", "apply", "--whitespace=nowarn", "--ignore-whitespace",
                        ".fixeval.patch"],
                       cwd=str(tree), capture_output=True, text=True, encoding="utf-8", errors="replace")
    (tree / ".fixeval.patch").unlink(missing_ok=True)
    return r.returncode == 0


def edited_files(before: Path, after: Path) -> list[str]:
    out = []
    for p in after.rglob("*"):
        if not p.is_file() or any(s in p.parts for s in (".git", "node_modules", "__pycache__", ".pytest_cache")):
            continue
        rel = p.relative_to(after)
        q = before / rel
        try:
            if not q.exists() or q.read_bytes() != p.read_bytes():
                out.append(rel.as_posix())
        except OSError:
            continue
    return sorted(out)


# --------------------------------------------------------------------------- select

def select(args) -> int:
    rows = []
    for t in load_corpus():
        key = task_key(t)
        try:
            _select_one(t, key, args, rows)
        except Exception as exc:  # one broken task must not end the pass
            import traceback
            print(f"{key:<22} skip: harness error {type(exc).__name__}: {exc}")
            traceback.print_exc()
    if args.repo and TASKS.exists():
        # per-repo runs merge into the existing file instead of clobbering it
        keep = [r for r in json.loads(TASKS.read_text(encoding="utf-8")) if r["repo"] != args.repo]
        rows = sorted(keep + rows, key=lambda r: r["key"])
    TASKS.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\n{len(rows)} verifiable task(s) -> {TASKS}")
    return 0


def _select_one(t: dict, key: str, args, rows: list) -> None:
    if True:
        files = changed_test_files(t)
        if args.repo and t["repo"] != args.repo:
            return
        if not files:
            print(f"{key:<22} skip: fix changed no test file")
            return
        if t["repo"] == "requests" and commit_year(t) < 2021:
            # vendored/old `six` breaks on Python 3.12 (no find_spec); the
            # 2016-2019 requests suites would need a third interpreter
            print(f"{key:<22} skip: pre-2021 requests suite (old six on py3.12)")
            return
        wt, _ = prepare(t)
        tree = fresh_tree(t, WORK / "select" / key.replace("/", "-"))
        patch = test_patch(t, files)
        if not apply_patch(tree, patch):
            print(f"{key:<22} skip: test patch does not apply")
            return
        res = run_tests(t, tree, files)
        tests = res["tests"]
        if not tests:
            print(f"{key:<22} skip: suite did not run -- {res['tail'][-200:].strip()!r}")
            return
        # gold run: the same tests with the REAL fix applied. SWE-bench semantics:
        # FAIL_TO_PASS = fails before, passes with the gold patch; PASS_TO_PASS =
        # passes in both. Tests failing in BOTH are environment failures and are
        # excluded rather than blamed on the agent.
        full = git(REPO_PATHS[t["repo"]], "diff", t["parent"], t["commit"])
        tree2 = fresh_tree(t, WORK / "select" / (key.replace("/", "-") + "-fixed"))
        if not apply_patch(tree2, full):
            print(f"{key:<22} skip: full fix patch does not apply")
            return
        gold = run_tests(t, tree2, files)["tests"]
        fails = sorted(k for k, v in tests.items() if v in ("FAILED", "ERROR") and gold.get(k) == "PASSED")
        passes = sorted(k for k, v in tests.items() if v == "PASSED" and gold.get(k) == "PASSED")
        env_fail = sorted(k for k, v in tests.items() if v in ("FAILED", "ERROR") and gold.get(k) != "PASSED")
        if not fails:
            pend = sum(1 for v in tests.values() if v == "PENDING")
            print(f"{key:<22} skip: no test goes fail->pass with the real fix "
                  f"({len(passes)} pass, {len(env_fail)} fail in both, {pend} pending)")
            return
        rows.append({**{k: t[k] for k in ("repo", "commit", "parent", "subject", "entry")},
                     "key": key, "test_files": files, "fail_to_pass": fails,
                     "pass_to_pass": passes, "env_failures": env_fail,
                     "message": git(REPO_PATHS[t["repo"]], "show", "-s", "--format=%B", t["commit"]).strip()})
        print(f"{key:<22} OK: {len(fails)} fail-to-pass, {len(passes)} pass-to-pass, "
              f"{len(env_fail)} excluded as environment failures")
        shutil.rmtree(tree, ignore_errors=True)
        shutil.rmtree(tree2, ignore_errors=True)
    if args.repo and TASKS.exists():
        # per-repo runs merge into the existing file instead of clobbering it
        keep = [r for r in json.loads(TASKS.read_text(encoding="utf-8")) if r["repo"] != args.repo]
        rows = sorted(keep + rows, key=lambda r: r["key"])
    TASKS.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\n{len(rows)} verifiable task(s) -> {TASKS}")
    return 0


# --------------------------------------------------------------------------- run

def context_pack(t: dict) -> str:
    """The shipped `graphify-ext context` output for the task's entry symbol,
    on the supplemented graph, as the agent would get it from the CLI."""
    from graphify_ext import blast_radius as br
    from graphify_ext import context as ctxmod
    from graphify_ext import graphio
    # tasks.json rows are trimmed; the corpus row carries `by_file`, which
    # resolve_entry needs to find the entry symbol's node by (file, def_line).
    ct = next(x for x in load_corpus() if task_key(x) == t["key"])
    wt, _ = prepare(ct)
    sup = wt / "graphify-out" / "graph.supplemented.json"
    gp = sup if sup.exists() else wt / "graphify-out" / "graph.json"
    data = graphio.load(gp)
    seed = resolve_entry(data, ct)
    if seed is None:
        return "(the code graph has no node for the symbol named in the report)"
    rels = tuple(dict.fromkeys(br.DEFAULT_RELATIONS + br.MEMBER_RELATIONS))
    manifest = None
    mp = wt / "graphify-out" / "manifest.json"
    if mp.exists():
        try:
            manifest = graphio.read_json(mp)
        except Exception:
            manifest = None
    pack = ctxmod.build_context(data, seed, wt, depth=2, direction="both", budget=6000,
                                relations=rels, index_budget=300, manifest=manifest)
    text = pack["text"]
    if pack["related_tests"]:
        text += "\n--- tests linked to the code above:\n" + "\n".join(
            f"  {r['test_file']}:{r['test_location']} {r['test_label']} --{r['relation']}--> {r['touches_label']}"
            for r in pack["related_tests"][:12])
    if pack["unmodelled"]:
        text += "\n--- definitions in the code above with NO graph node (read the enclosing body):\n" + "\n".join(
            f"  {u['name']} {u['file']}:{u['def_line']}" for u in pack["unmodelled"][:10])
    return text


def prompt_for(t: dict, arm: str, pack: str | None) -> str:
    lang = "JavaScript (Node)" if t["repo"] == "express" else "Python"
    head = (
        f"You are fixing a bug in the {t['repo']} repository ({lang}) checked out in the "
        f"current directory. Below is the maintainers' description of the change that is "
        f"needed. Make that change to the library source. Do NOT modify or add tests; the "
        f"maintainers' tests will be run against your change afterwards. Do not run "
        f"commands; use only file reading, searching and editing. When done, stop with a "
        f"one-paragraph summary of what you changed and why.\n\n"
        f"=== Change description (commit message) ===\n{t['message']}\n"
    )
    if arm == "graph":
        head += (
            "\n=== Code-graph context pack ===\n"
            "The project's code graph produced the following context for the symbol the "
            "description names: its source, the source of related symbols, an index of "
            "further symbols as file:line + signature, tests linked to them, and any "
            "definitions the graph has no node for. Use it as your starting point; open "
            "files only for what it does not show.\n\n" + (pack or "")
        )
    return head


def run_agent(tree: Path, prompt: str, log: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    cmd = ["claude", "-p", "--model", MODEL, "--max-turns", str(MAX_TURNS),
           "--max-budget-usd", str(MAX_USD), "--output-format", "json",
           "--permission-mode", "acceptEdits",
           "--allowedTools", "Read", "Edit", "Write", "Grep", "Glob",
           "--disallowedTools", "Bash", "WebFetch", "WebSearch", "Agent", "NotebookEdit"]
    t0 = time.perf_counter()
    r = subprocess.run(cmd, input=prompt, cwd=str(tree), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=env, shell=True, timeout=3600)
    wall = time.perf_counter() - t0
    log.write_text(r.stdout + "\n--- stderr ---\n" + r.stderr, encoding="utf-8")
    try:
        d = json.loads(r.stdout[r.stdout.find("{"):])
    except Exception:
        return {"ok": False, "wall_s": round(wall, 1), "error": (r.stdout + r.stderr)[-500:]}
    return {"ok": True, "wall_s": round(wall, 1), "turns": d.get("num_turns"),
            "cost_usd": d.get("total_cost_usd"), "stop_reason": d.get("stop_reason"),
            "terminal_reason": d.get("terminal_reason"), "is_error": d.get("is_error"),
            "result_tail": str(d.get("result", ""))[-600:]}


def run(args) -> int:
    tasks = json.loads(TASKS.read_text(encoding="utf-8"))
    wanted = set(args.task or [])
    arms = ["graph", "nograph"] if args.arm == "both" else [args.arm]
    done = set()
    if RESULTS.exists() and not args.redo:
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                done.add((d["key"], d["arm"]))
    for t in tasks:
        if wanted and t["key"] not in wanted:
            continue
        for arm in arms:
            if (t["key"], arm) in done:
                print(f"{t['key']:<22} {arm:<8} already done")
                continue
            slug = f"{t['key'].replace('/', '-')}-{arm}"
            tree = fresh_tree(t, WORK / slug)
            pack = context_pack(t) if arm == "graph" else None
            prompt = prompt_for(t, arm, pack)
            (WORK / f"{slug}.prompt.txt").write_text(prompt, encoding="utf-8")
            print(f"{t['key']:<22} {arm:<8} agent running ...", flush=True)
            agent = run_agent(tree, prompt, WORK / f"{slug}.agent.json")
            base = wt_for(t)
            edits = edited_files(base, tree)
            patched = apply_patch(tree, test_patch(t, t["test_files"]))
            res = run_tests(t, tree, t["test_files"]) if patched else {"tests": {}, "tail": "test patch failed to apply after edit"}
            f2p = {k: res["tests"].get(k) for k in t["fail_to_pass"]}
            p2p_broken = [k for k in t["pass_to_pass"] if res["tests"].get(k) not in ("PASSED", None, "SKIPPED")]
            resolved = all(v == "PASSED" for v in f2p.values()) and not p2p_broken and bool(f2p)
            row = {"key": t["key"], "arm": arm, "model": MODEL, "resolved": resolved,
                   "fail_to_pass": f2p, "pass_to_pass_broken": p2p_broken,
                   "edited_files": edits, "agent": agent,
                   "pack_chars": len(pack) if pack else 0,
                   "tests_ran": bool(res["tests"]), "test_tail": res.get("tail", "")[-400:]}
            with RESULTS.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            print(f"{t['key']:<22} {arm:<8} resolved={resolved} f2p={sum(1 for v in f2p.values() if v == 'PASSED')}/{len(f2p)} "
                  f"broken={len(p2p_broken)} turns={agent.get('turns')} cost=${agent.get('cost_usd')} "
                  f"edits={edits}", flush=True)
    return 0


def report(args) -> int:
    rows = [json.loads(l) for l in RESULTS.read_text(encoding="utf-8").splitlines() if l.strip()]
    by = {}
    for r in rows:
        by.setdefault(r["key"], {})[r["arm"]] = r
    print(f"{'task':<22} {'graph':<10} {'nograph':<10} turns(g/n)  cost(g/n)      edits(g/n)")
    for k in sorted(by):
        g, n = by[k].get("graph"), by[k].get("nograph")
        def fmt(r):
            return "-" if r is None else ("RESOLVED" if r["resolved"] else "failed")
        def tu(r):
            return "-" if r is None else str(r["agent"].get("turns"))
        def co(r):
            return "-" if r is None else f"{(r['agent'].get('cost_usd') or 0):.2f}"
        def ed(r):
            return "-" if r is None else str(len([f for f in r["edited_files"] if not f.startswith(".git/")]))
        print(f"{k:<22} {fmt(g):<10} {fmt(n):<10} {tu(g)}/{tu(n):<8} ${co(g)}/${co(n):<8} {ed(g)}/{ed(n)}")
    for arm in ("graph", "nograph"):
        rs = [r for r in rows if r["arm"] == arm]
        if not rs:
            continue
        res = sum(1 for r in rs if r["resolved"])
        turns = [r["agent"].get("turns") or 0 for r in rs]
        cost = [r["agent"].get("cost_usd") or 0 for r in rs]
        print(f"\n{arm:<8} resolved {res}/{len(rs)}   mean turns {sum(turns)/len(turns):.1f}   "
              f"mean cost ${sum(cost)/len(cost):.2f}   total ${sum(cost):.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--repo", default=None)
    r = sub.add_parser("run")
    r.add_argument("--arm", default="both", choices=["graph", "nograph", "both"])
    r.add_argument("--task", action="append")
    r.add_argument("--redo", action="store_true")
    sub.add_parser("report")
    args = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    return {"select": select, "run": run, "report": report}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
