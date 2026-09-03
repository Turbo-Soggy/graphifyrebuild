import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = REPO_ROOT / "graphify-upstream"


@pytest.fixture()
def toy_graph() -> dict:
    """Small graph exercising callers, inheritance, tests, and config.

    handler() -> validate() -> sanitize()
    Base.check() with Sub inheriting Base and overriding check()
    test_validate() in tests/ calls validate()
    """
    def n(nid, label, file, line, **kw):
        d = {"id": nid, "label": label, "source_file": file,
             "source_location": f"L{line}", "file_type": "code", "type": "code"}
        d.update(kw)
        return d

    def e(s, t, rel, **kw):
        d = {"source": s, "target": t, "relation": rel, "confidence": "EXTRACTED"}
        d.update(kw)
        return d

    return {
        "directed": True,
        "nodes": [
            n("app.handler", "handler()", "src/app.py", 10, _callable=True),
            n("app.validate", "validate()", "src/app.py", 30, _callable=True),
            n("app.sanitize", "sanitize()", "src/app.py", 50, _callable=True),
            n("base.Base", "Base", "src/base.py", 1),
            n("base.Base.check", "check()", "src/base.py", 5, _callable=True),
            n("sub.Sub", "Sub", "src/sub.py", 1),
            n("sub.Sub.check", "check()", "src/sub.py", 5, _callable=True),
            n("t.test_validate", "test_validate()", "tests/test_app.py", 3, _callable=True),
            n("cfg.env", ".env.example", ".env.example", 1, file_type="document"),
        ],
        "edges": [
            e("app.handler", "app.validate", "calls"),
            e("app.validate", "app.sanitize", "calls"),
            e("base.Base", "base.Base.check", "method"),
            e("sub.Sub", "sub.Sub.check", "method"),
            e("sub.Sub", "base.Base", "inherits"),
            e("app.handler", "base.Base.check", "calls"),
            e("t.test_validate", "app.validate", "calls"),
        ],
    }


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A real git repo with two branches and a couple of commits."""
    def git(*args):
        r = subprocess.run(["git", "-C", str(tmp_path), *args],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"git {args} failed: {r.stderr}"
        return r.stdout.strip()

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)],
                   capture_output=True, check=True)
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "c1")
    git("checkout", "-q", "-b", "feature/x")
    (tmp_path / "b.py").write_text("def b():\n    pass\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "c2")
    git("checkout", "-q", "main")
    return tmp_path


@pytest.fixture()
def graph_dir(tmp_path: Path, toy_graph: dict) -> Path:
    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps(toy_graph), encoding="utf-8")
    return out


@pytest.fixture()
def upstream_on_path(monkeypatch):
    """Make the vendored upstream graphify package importable."""
    monkeypatch.syspath_prepend(str(UPSTREAM))
    yield
    for mod in list(sys.modules):
        if mod == "graphify" or mod.startswith("graphify."):
            sys.modules.pop(mod)
