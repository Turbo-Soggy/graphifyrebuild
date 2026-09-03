"""Config/schema-linkage producer (Requirement 2, case 6).

Verified before building (spec checklist): the stock AST extractor emits NO
env-var or config-usage edges — grep of graphify v8's extract.py shows
``os.environ`` only for graphify's own debug flags. So this pass exists.

Scope: environment-variable linkage — the highest-signal, cheapest slice of
case 6. A function that reads ``FOO`` gets a ``reads_config`` edge to every
config-ish file that DEFINES ``FOO`` (.env*, docker-compose, CI yaml,
terraform). Changing validation logic in that function can violate
assumptions baked into those files, and vice versa.

Emits neutral findings (source_ref = code file:line of the read, resolved to
the enclosing function node at inject time; target_ref = config file node).
"""
from __future__ import annotations

import re
from pathlib import Path

# Read-side patterns per language family. Each must expose a named group `var`.
_READ_PATTERNS = (
    # Python: os.environ["X"], os.environ.get("X"), os.getenv("X")
    re.compile(r"""(?:os\.environ(?:\.get)?\s*[\[\(]|os\.getenv\s*\()\s*['"](?P<var>[A-Z][A-Z0-9_]+)['"]"""),
    # JS/TS: process.env.X, process.env["X"]
    re.compile(r"""process\.env(?:\.(?P<var>[A-Z][A-Z0-9_]+)|\[\s*['"](?P<var2>[A-Z][A-Z0-9_]+)['"]\s*\])"""),
    # Ruby: ENV["X"] / ENV.fetch("X")
    re.compile(r"""ENV(?:\.fetch)?\s*[\[\(]\s*['"](?P<var>[A-Z][A-Z0-9_]+)['"]"""),
    # Go: os.Getenv("X") / os.LookupEnv("X")
    re.compile(r'os\.(?:Getenv|LookupEnv)\s*\(\s*"(?P<var>[A-Z][A-Z0-9_]+)"'),
    # Java: System.getenv("X")
    re.compile(r"""System\.getenv\s*\(\s*"(?P<var>[A-Z][A-Z0-9_]+)"\s*\)"""),
)

_CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
                  ".rb", ".go", ".java", ".kt", ".cs", ".php"}

# Definition-side: config files and the pattern that defines a var in them.
_CONFIG_GLOBS = (
    ".env", ".env.*", "*.env",
    "docker-compose*.yml", "docker-compose*.yaml", "Dockerfile*",
    "*.tf", "*.tfvars",
    ".github/workflows/*.yml", ".github/workflows/*.yaml",
    "k8s/*.yaml", "k8s/*.yml", "helm/**/*.yaml",
)
_DEF_PATTERN = re.compile(r"^\s*(?:export\s+|ENV\s+|ARG\s+)?(?P<var>[A-Z][A-Z0-9_]+)\s*[=:]", re.M)

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
              "graphify-out", ".graphify-cache", "dist", "build", ".tox"}


def _iter_files(root: Path, suffixes: set[str]):
    for p in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.is_file() and p.suffix in suffixes:
            yield p


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def scan_env_reads(root: Path) -> list[tuple[str, int, str]]:
    """[(relative_file, line, VAR), ...] for every env-var read in code."""
    out: list[tuple[str, int, str]] = []
    for p in _iter_files(root, _CODE_SUFFIXES):
        text = _read_text(p)
        if not text:
            continue
        rel = p.relative_to(root).as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            for pat in _READ_PATTERNS:
                for m in pat.finditer(line):
                    var = m.group("var") or (m.groupdict().get("var2") or "")
                    if var:
                        out.append((rel, i, var))
    return out


def scan_env_definitions(root: Path) -> dict[str, list[str]]:
    """{VAR: [relative config files defining it]}"""
    defs: dict[str, list[str]] = {}
    seen: set[Path] = set()
    for glob in _CONFIG_GLOBS:
        for p in root.glob(glob):
            if not p.is_file() or p in seen:
                continue
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            seen.add(p)
            rel = p.relative_to(root).as_posix()
            for m in _DEF_PATTERN.finditer(_read_text(p)):
                defs.setdefault(m.group("var"), []).append(rel)
    return defs


def scan(root: Path) -> dict:
    """Full pass -> neutral findings: code read site --reads_config--> config file."""
    reads = scan_env_reads(root)
    defs = scan_env_definitions(root)
    edges = []
    for file, line, var in reads:
        for config_file in defs.get(var, []):
            edges.append({
                "relation": "reads_config",
                "source_ref": {"file": file, "line": line},
                "target_ref": {"file": config_file},
                "detail": f"env:{var}",
            })
    return {"edges": edges}
