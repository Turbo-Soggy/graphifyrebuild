#!/usr/bin/env python3
"""Requirement 2 triage smoke test per claude-code-implementation-brief.md.

Spawns a LIVE ``code-review-graph serve`` (stdio transport, restricted to the
triage tool set via --tools) and calls the four tools an appsec fix-triage
agent would call, printing each tool's RAW JSON — the point is seeing exactly
what the agent would see, not a summary.

Usage:
  python test_triage.py --file src/flask/app.py --symbol Flask
  python test_triage.py --file src/flask/app.py --symbol Flask --post-fix
  (run from, or pass --repo, the sandbox repo; graph must be built)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config_link  # noqa: E402
import taint_inject  # noqa: E402

TRIAGE_TOOLS = (
    "query_graph_tool,get_impact_radius_tool,get_review_context_tool,"
    "get_knowledge_gaps_tool,detect_changes_tool"
)


def crg_cli() -> str:
    override = os.environ.get("CRG_CLI", "").strip()
    if override:
        return override
    exe_dir = Path(sys.executable).parent
    for name in ("code-review-graph.exe", "code-review-graph"):
        cand = exe_dir / name
        if cand.exists():
            return str(cand)
    return "code-review-graph"


def show(title: str, payload) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(payload, indent=2, default=str))


def _radius_qualified_names(impact) -> set[str]:
    """Collect qualified_name strings from every node list in the impact
    payload (changed_nodes, impacted nodes, however the detail level names
    them) — resilient to detail_level shape differences."""
    names: set[str] = set()

    def walk(obj):
        if isinstance(obj, dict):
            qn = obj.get("qualified_name")
            if isinstance(qn, str):
                names.add(qn)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(impact)
    return names


def unwrap(result) -> object:
    """Prefer the structured dict FastMCP returns; fall back to text content."""
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            try:
                parts.append(json.loads(text))
            except ValueError:
                parts.append(text)
    return parts[0] if len(parts) == 1 else parts


async def run(repo: Path, file: str, symbol: str, post_fix: bool) -> int:
    params = StdioServerParameters(
        command=crg_cli(),
        args=["serve", "--tools", TRIAGE_TOOLS],
        cwd=str(repo),
    )
    t_total = time.time()
    timings: dict[str, float] = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"server up; exposed tools ({len(names)}): {', '.join(names)}")

            async def call(tool: str, args: dict) -> object:
                t0 = time.time()
                result = await session.call_tool(tool, args)
                timings[tool] = time.time() - t0
                return unwrap(result)

            taint_summary = ""
            config_summary = ""
            if post_fix:
                # Post-fix flow: risk-scored delta for the working-tree edit.
                delta = await call("detect_changes_tool", {
                    "changed_files": [file], "detail_level": "standard",
                })
                show("detect_changes_tool (post-fix risk-scored delta)", delta)
            else:
                impact = await call("get_impact_radius_tool", {
                    "changed_files": [file], "detail_level": "standard",
                })
                show("get_impact_radius_tool (blast radius)", impact)

                # Taint-exposed subset (spec case 4): intersect the blast
                # radius with injected taint_edges — the appsec-relevant
                # slice of the structural radius.
                taint = taint_inject.taint_rows(repo)
                radius_names = _radius_qualified_names(impact)
                touched = {r["source_qualified"] for r in taint} | \
                          {r["target_qualified"] for r in taint}
                exposed = sorted(radius_names & touched)
                exposed_rows = [
                    r for r in taint
                    if r["source_qualified"] in radius_names
                    or r["target_qualified"] in radius_names
                ]
                if taint:
                    taint_summary = (
                        f"taint reachability: {len(taint)} injected edge(s); "
                        f"{len(exposed)} of {len(radius_names)} blast-radius "
                        f"nodes taint-exposed")
                else:
                    taint_summary = (
                        "taint reachability: no findings injected — run "
                        "taint_inject.py apply (structural blast-radius only)")
                show("taint-exposed subset of blast radius",
                     {"summary": taint_summary,
                      "exposed_nodes": exposed,
                      "taint_edges_in_radius": exposed_rows})

                callers = await call("query_graph_tool", {
                    "pattern": "callers_of", "target": symbol,
                    "detail_level": "standard",
                })
                show(f"query_graph_tool (callers_of {symbol})", callers)

                gaps = await call("get_knowledge_gaps_tool", {
                    "detail_level": "standard", "max_per_category": 50,
                })
                show("get_knowledge_gaps_tool (raw)", gaps)
                # Hotspot list nests under "gaps" in the tool response.
                hotspots = ((gaps or {}).get("gaps") or {}).get(
                    "untested_hotspots",
                    (gaps or {}).get("untested_hotspots", []))
                flagged = [h for h in hotspots
                           if symbol in json.dumps(h, default=str)]
                show(f"untested-hotspot verdict for '{symbol}'",
                     {"symbol_flagged_untested": bool(flagged),
                      "matching_entries": flagged,
                      "total_untested_hotspots": len(hotspots)})

                # Config/schema dependencies (spec case 6): what configuration
                # and DB-schema contracts the blast-radius code depends on.
                deps = config_link.dependencies_for(repo, sorted(radius_names))
                by_kind: dict[str, list[dict]] = {}
                for d in deps:
                    by_kind.setdefault(d["kind"], []).append(d)
                config_summary = (
                    f"config dependencies: {len(by_kind.get('READS_CONFIG', []))} "
                    f"env-var link(s), {len(by_kind.get('USES_SCHEMA', []))} "
                    f"schema link(s) across the blast radius"
                ) if deps else (
                    "config dependencies: none linked — run config_link.py scan "
                    "(or this code reads no configured env vars / known tables)")
                show("config/schema dependencies of blast radius",
                     {"summary": config_summary, "edges": deps})

                ctx = await call("get_review_context_tool", {
                    "changed_files": [file], "include_source": True,
                    "max_lines_per_file": 60, "detail_level": "standard",
                })
                show("get_review_context_tool (agent context blob)", ctx)

    print("\n===== triage summary =====")
    print(taint_summary or
          "taint reachability: not evaluated in --post-fix mode "
          "(run without --post-fix for the exposed-subset view)")
    if config_summary:
        print(config_summary)
    for tool, dt in timings.items():
        print(f"  {tool}: {dt:.2f}s")
    total = time.time() - t_total
    print(f"total round-trip (server spawn + {len(timings)} tool calls): {total:.1f}s")
    if not post_fix and total > 30:
        print("  WARNING: total exceeds interactive-agent-loop budget — flagging per brief")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).parent / "sandbox-flask"))
    ap.add_argument("--file", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--post-fix", action="store_true")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    if not (repo / ".code-review-graph").exists():
        sys.exit(f"error: no graph at {repo} — run 'code-review-graph build' first")
    return asyncio.run(run(repo, args.file, args.symbol, args.post_fix))


if __name__ == "__main__":
    sys.exit(main())
