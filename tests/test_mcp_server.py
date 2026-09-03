"""Drive the graphify-ext MCP server over a real stdio session (research gap #4).

Spawns the actual server as a subprocess and talks to it with the MCP client
SDK — the client pattern is copied from ``crg/test_triage.py``, which already
does this against crg's server. Nothing here is mocked: a passing test means a
real agent can call these tools.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastmcp", reason="MCP surface is an optional extra")
pytest.importorskip("mcp", reason="MCP client SDK not installed")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _unwrap(result):
    """Prefer the structured dict; fall back to JSON in text content.

    Same shape-handling as crg/test_triage.py:75-87.
    """
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


async def _session(graph_path: Path, tools: str | None = None):
    args = ["-m", "graphify_ext.mcp_server"]
    if tools:
        args += ["--tools", tools]
    params = StdioServerParameters(command=sys.executable, args=args, cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


@pytest.fixture()
def graph_file(tmp_path, toy_graph):
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(toy_graph), encoding="utf-8")
    return p


def _run(coro):
    return asyncio.run(coro)


class TestMcpServer:
    def test_server_starts_and_exposes_its_tools(self, graph_file):
        async def go():
            async for s in _session(graph_file):
                listed = await s.list_tools()
                return sorted(t.name for t in listed.tools)
        names = _run(go())
        from graphify_ext.mcp_server import TOOL_NAMES
        assert names == sorted(TOOL_NAMES)

    def test_blast_radius_tool_returns_a_scoped_subgraph(self, graph_file):
        async def go():
            async for s in _session(graph_file):
                r = await s.call_tool("blast_radius_tool",
                                      {"node": "sanitize", "depth": 2,
                                       "graph": str(graph_file)})
                return _unwrap(r)
        out = _run(go())
        assert out["status"] == "ok"
        ids = {n["id"] for n in out["nodes"]}
        assert {"app.validate", "app.handler"} <= ids
        # The token budget must be reported, not implied.
        assert out["estimated_tokens"] > 0
        assert "tokens" in out["summary"]

    def test_relation_filtering_is_reachable_through_the_tool(self, graph_file):
        async def go():
            async for s in _session(graph_file):
                wide = _unwrap(await s.call_tool(
                    "blast_radius_tool",
                    {"node": "base.Base", "depth": 2, "graph": str(graph_file)}))
                narrow = _unwrap(await s.call_tool(
                    "blast_radius_tool",
                    {"node": "base.Base", "depth": 2, "relations": ["calls"],
                     "graph": str(graph_file)}))
                return wide, narrow
        wide, narrow = _run(go())
        assert {n["id"] for n in narrow["nodes"]} < {n["id"] for n in wide["nodes"]}

    def test_errors_are_returned_as_data_not_raised(self, graph_file):
        """crg convention: a tool reports {"status":"error"} rather than
        failing the call, so a client sees a structured result."""
        async def go():
            async for s in _session(graph_file):
                r = await s.call_tool("blast_radius_tool",
                                      {"node": "no_such_symbol_xyz",
                                       "graph": str(graph_file)})
                return _unwrap(r)
        out = _run(go())
        assert out["status"] == "error"
        assert "no unique node match" in out["error"]

    def test_list_relations_tool_reports_default_membership(self, graph_file):
        async def go():
            async for s in _session(graph_file):
                return _unwrap(await s.call_tool("list_relations_tool",
                                                 {"graph": str(graph_file)}))
        out = _run(go())
        assert out["status"] == "ok"
        by_rel = {r["relation"]: r for r in out["relations"]}
        assert by_rel["calls"]["followed_by_default"] is True
        # containment is present in the graph but deliberately not traversed
        assert by_rel["method"]["followed_by_default"] is False

    def test_tools_allow_list_actually_removes_tools(self, graph_file):
        async def go():
            async for s in _session(graph_file, tools="blast_radius_tool"):
                listed = await s.list_tools()
                return [t.name for t in listed.tools]
        assert _run(go()) == ["blast_radius_tool"]
