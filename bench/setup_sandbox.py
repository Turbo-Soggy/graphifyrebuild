#!/usr/bin/env python3
"""Create a pristine benchmark sandbox and pin its baseline with a git tag.

Why a tag rather than "whatever HEAD is when the script starts": the B-series
deliberately creates branches, amends commits, and diverges history. Capturing
the baseline at run time means the SECOND run resets to a commit the FIRST run
polluted — which silently invalidates every timing and correctness result
measured afterwards. The `bench-base` tag is written once, at clone time, and
every reset targets it.

Usage:
  python setup_sandbox.py [--name sandbox-a] [--repo-url URL] [--force]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402

BASE_TAG = "bench-base"
DEFAULT_URL = "https://github.com/pallets/flask.git"


def setup(path: Path, url: str, force: bool) -> Path:
    if path.exists():
        if not force:
            # Already pinned? Then it is reusable as-is.
            tags = subprocess.run(["git", "-C", str(path), "tag", "--list", BASE_TAG],
                                  capture_output=True, text=True).stdout.strip()
            if tags == BASE_TAG:
                print(f"{path.name}: already set up (tag {BASE_TAG} present)")
                return path
            print(f"{path.name}: exists but has no {BASE_TAG} tag — re-cloning")
        H.rmtree(path)

    print(f"cloning {url} -> {path}")
    subprocess.run(["git", "clone", "--depth", "1", url, str(path)],
                   check=True, capture_output=True)
    for cfg in (("user.email", "bench@example.com"), ("user.name", "bench"),
                ("commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(path), "config", *cfg],
                       check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "tag", BASE_TAG],
                   check=True, capture_output=True)
    # Record the real default branch: flask uses "main", scrapy uses "master".
    branch = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    (path / ".git" / H.DEFAULT_BRANCH_FILE).write_text(branch, encoding="utf-8")
    H.ensure_excludes(path)
    head = H.head(path)
    files = len(list(path.rglob("*.py")))
    print(f"{path.name}: pinned {BASE_TAG} at {head[:8]} "
          f"on '{branch}' ({files} .py files)")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="sandbox-a")
    ap.add_argument("--repo-url", default=DEFAULT_URL)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    setup(H.BENCH / args.name, args.repo_url, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
