"""graphify-ext CLI.

Requirement 1:
  graphify-ext hook install|uninstall|status   customized git hooks
  graphify-ext swap [--branch B]               manual swap_or_build

Requirement 2:
  graphify-ext blast-radius "<node>" [--depth N] [--direction up|down|both]
                                     [--max-nodes N] [--relation REL ...]
                                     [--include-containment] [--list-relations]
                                     [--graph P] [--json]
  graphify-ext overrides "<node>" [--graph P]
  graphify-ext inject (<findings.json> | --semgrep out.json) [--graph P] [--no-store]
  graphify-ext test-link (--coverage cov.json | --heuristic) [--graph P] [--dry-run]
  graphify-ext config-scan [path] [--graph P] [--dry-run]
  graphify-ext reapply [--out DIR]
  graphify-ext triage report.json [--depth N] [--max-nodes N] [--graph P] [--out P]
  graphify-ext verify-fix snapshot --node X [--node Y ...] [--out DIR]
  graphify-ext verify-fix check [--out DIR] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

OUT_DEFAULT = os.environ.get("GRAPHIFY_OUT", "graphify-out")


def _graph_arg(p: argparse.ArgumentParser):
    p.add_argument("--graph", default=str(Path(OUT_DEFAULT) / "graph.json"),
                   help="path to graph.json (default: %(default)s)")


def _load_graph(path: str) -> dict:
    from graphify_ext import graphio
    gp = Path(path)
    if not gp.exists():
        sys.exit(f"error: graph not found at {gp} — build it first (graphify . / graphify update .)")
    return graphio.load(gp)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="graphify-ext")
    sub = ap.add_subparsers(dest="cmd", required=True)

    hook = sub.add_parser("hook", help="install/uninstall/status of customized git hooks")
    hook.add_argument("action", choices=["install", "uninstall", "status"])

    swap = sub.add_parser("swap", help="run swap_or_build for the current (or given) branch")
    swap.add_argument("--branch", default=None)

    brp = sub.add_parser("blast-radius", help="scoped subgraph around a node")
    brp.add_argument("node")
    brp.add_argument("--depth", type=int, default=2)
    brp.add_argument("--direction", choices=["up", "down", "both"], default="up")
    brp.add_argument("--max-nodes", type=int, default=500)
    brp.add_argument("--relation", action="append", default=[], metavar="REL",
                     help="edge relation to follow (repeatable). Omit for the "
                          "structural default set; see --list-relations")
    brp.add_argument("--include-containment", action="store_true",
                     help="also follow contains/method edges, answering "
                          "'what is in this class/file'. Off by default: "
                          "containment is the most common relation in a graph, "
                          "and a changed file already seeds every node in it")
    brp.add_argument("--list-relations", action="store_true",
                     help="print the relations available in this graph and exit")
    brp.add_argument("--json", action="store_true", dest="as_json")
    _graph_arg(brp)

    ctx = sub.add_parser("context",
                         help="fix-ready context pack: the seed symbol's SOURCE "
                              "plus its neighbourhood, within a token budget")
    ctx.add_argument("node")
    ctx.add_argument("--depth", type=int, default=2)
    ctx.add_argument("--direction", choices=["up", "down", "both"], default="both")
    ctx.add_argument("--budget", type=int, default=6000,
                     help="token budget for the whole pack (default 6000)")
    ctx.add_argument("--per-symbol-cap", type=int, default=80,
                     help="max lines emitted per symbol before explicit truncation")
    ctx.add_argument("--relation", action="append", default=[], metavar="REL")
    ctx.add_argument("--no-containment", action="store_true",
                     help="do NOT follow contains/method edges. Containment is ON "
                          "by default here (unlike blast-radius): measured on 14 "
                          "real fix commits it lifts recall 0.351 -> 0.494 at "
                          "depth 2, improves precision 0.131 -> 0.146, regresses "
                          "0/14 tasks, and pulls in only ~24%% of the seed's own "
                          "file. Turn it off to match blast-radius' narrower walk")
    ctx.add_argument("--json", action="store_true", dest="as_json")
    _graph_arg(ctx)

    ovp = sub.add_parser("overrides", help="overriding implementations of a method")
    ovp.add_argument("node")
    _graph_arg(ovp)

    inj = sub.add_parser("inject", help="merge external findings edges into graph.json")
    inj.add_argument("findings", nargs="?", help="findings JSON file (neutral format)")
    inj.add_argument("--semgrep", help="semgrep JSON output to convert and inject")
    inj.add_argument("--no-store", action="store_true",
                     help="apply once without persisting for hook re-application")
    inj.add_argument("--taint-rule", action="append", default=[], metavar="ID",
                     help="semgrep rule id (substring) that runs in taint mode. "
                          "Such findings often carry no dataflow trace; without "
                          "this they are reported as skipped, never guessed at")
    inj.add_argument("--assume-taint", action="store_true",
                     help="treat every trace-less semgrep finding as taint "
                          "(only safe when the scan used taint rules only)")
    _graph_arg(inj)

    tlp = sub.add_parser("test-link", help="produce+inject 'tests' edges")
    tlp.add_argument("--coverage", help="coverage.py JSON (with dynamic contexts)")
    tlp.add_argument("--heuristic", action="store_true", help="name-matching fallback")
    tlp.add_argument("--dry-run", action="store_true", help="print findings, don't inject")
    _graph_arg(tlp)

    csp = sub.add_parser("config-scan", help="produce+inject 'reads_config' edges")
    csp.add_argument("path", nargs="?", default=".")
    csp.add_argument("--dry-run", action="store_true")
    _graph_arg(csp)

    rap = sub.add_parser("reapply", help="re-inject stored findings after a rebuild")
    rap.add_argument("--out", default=OUT_DEFAULT)

    trp = sub.add_parser("triage", help="build agent fix-context for each vuln in a report")
    trp.add_argument("report", help="identify-layer report JSON (list of vulns)")
    trp.add_argument("--depth", type=int, default=2)
    trp.add_argument("--max-nodes", type=int, default=300)
    trp.add_argument("--out", help="write full contexts JSON here (default: stdout summary only)")
    _graph_arg(trp)

    vfp = sub.add_parser("verify-fix", help="pre/post fix edge diff for target nodes")
    vfp.add_argument("action", choices=["snapshot", "check"])
    vfp.add_argument("--node", action="append", default=[],
                     help="target node (repeatable; required for snapshot)")
    vfp.add_argument("--out", default=OUT_DEFAULT)
    vfp.add_argument("--json", action="store_true", dest="as_json")

    args = ap.parse_args(argv)

    if args.cmd == "hook":
        from graphify_ext import hooks_ext
        fn = {"install": hooks_ext.install, "uninstall": hooks_ext.uninstall,
              "status": hooks_ext.status}[args.action]
        print(fn())
        return 0

    if args.cmd == "swap":
        from graphify_ext.branch_cache import swap_or_build
        return 0 if swap_or_build(args.branch) else 1

    if args.cmd == "blast-radius":
        from graphify_ext import blast_radius as br
        from graphify_ext import graphio
        data = _load_graph(args.graph)
        if args.list_relations:
            import collections
            counts = collections.Counter(
                str(e.get("relation", "")) for e in graphio.edges(data))
            default = set(br.DEFAULT_RELATIONS)
            print(f"{'relation':22} {'count':>7}  followed by default?")
            for rel, n in counts.most_common():
                print(f"  {rel:20} {n:>7}  {'yes' if rel in default else 'no'}")
            return 0
        nid = graphio.resolve_node(data, args.node)
        if nid is None:
            sys.exit(f"error: no unique node match for {args.node!r}")
        # Empty --relation means "use the default set", matching stock
        # graphify's `affected --relation` (graphify/cli.py:1386).
        relations = tuple(args.relation) if args.relation else br.DEFAULT_RELATIONS
        if args.include_containment:
            relations = tuple(dict.fromkeys(relations + br.MEMBER_RELATIONS))
        radius = br.blast_radius(data, nid, depth=args.depth,
                                 direction=args.direction, max_nodes=args.max_nodes,
                                 relations=relations)
        if args.as_json:
            json.dump(radius, sys.stdout, indent=2)
            print()
        else:
            print(f"Blast radius of {args.node} (depth {args.depth}, {args.direction}): "
                  f"{radius['node_count']} nodes, {radius['edge_count']} edges, "
                  f"~{radius['estimated_tokens']} tokens"
                  + (" [TRUNCATED]" if radius["truncated"] else ""))
            if len(radius["relations"]) != len(br.DEFAULT_RELATIONS):
                print(f"  relations: {', '.join(radius['relations'])}")
            for n in radius["nodes"]:
                loc = n.get("source_file", "-")
                if n.get("source_location"):
                    loc += f":{n['source_location']}"
                print(f"  [{n['blast_depth']}] {n.get('label')} {loc}")
        return 0

    if args.cmd == "context":
        from graphify_ext import context as ctxmod
        from graphify_ext import graphio
        data = _load_graph(args.graph)
        nid = graphio.resolve_node(data, args.node)
        if nid is None:
            sys.exit(f"error: no unique node match for {args.node!r}")
        # repo_root_for, never Path.resolve(): graphify-out may be a symlink into
        # the per-branch cache, and resolving it would point source lookups at
        # the cache directory instead of the working tree.
        root = graphio.repo_root_for(args.graph)
        from graphify_ext import blast_radius as _br
        if args.relation:
            rels = tuple(args.relation)          # explicit set wins outright
        elif args.no_containment:
            rels = _br.DEFAULT_RELATIONS
        else:
            rels = tuple(dict.fromkeys(_br.DEFAULT_RELATIONS + _br.MEMBER_RELATIONS))
        pack = ctxmod.build_context(
            data, nid, root, depth=args.depth, direction=args.direction,
            budget=args.budget, per_symbol_cap=args.per_symbol_cap,
            relations=rels,
        )
        if args.as_json:
            json.dump(pack, sys.stdout, indent=2)
            print()
            return 0
        print(pack["text"], end="")
        if not pack["seed_resolved"]:
            print(f"!!! SEED NOT SLICED ({pack['seed_unresolved_reason']}): no "
                  f"source could be recovered for {args.node!r} itself.")
        print(f"\n--- {len(pack['included'])} symbol(s), {pack['tokens_used']} "
              f"tokens of {pack['budget']} ({pack['token_method']})")
        if pack["unresolved"]:
            print(f"--- {len(pack['unresolved'])} unresolved (no slice emitted):")
            for u in pack["unresolved"][:10]:
                print(f"      {u['label']} {u['file']}:{u['location']} "
                      f"[{u['reason_code']}] {u['reason']}")
        if pack["omitted"]:
            print(f"--- {len(pack['omitted'])} omitted for budget; raise --budget to include")
        return 0

    if args.cmd == "overrides":
        from graphify_ext import blast_radius as br
        from graphify_ext import graphio
        data = _load_graph(args.graph)
        nid = graphio.resolve_node(data, args.node)
        if nid is None:
            sys.exit(f"error: no unique node match for {args.node!r}")
        overrides = br.overrides_of(data, nid)
        if not overrides:
            print("No overriding implementations found.")
        for o in overrides:
            print(f"  {o.get('label')} in {o.get('source_file')} "
                  f"(class {o.get('owning_class')}) — needs its own review/fix")
        return 0

    if args.cmd == "inject":
        from graphify_ext import edge_inject
        if bool(args.findings) == bool(args.semgrep):
            sys.exit("error: pass exactly one of <findings.json> or --semgrep")
        from graphify_ext import graphio
        if args.semgrep:
            findings = edge_inject.from_semgrep(
                graphio.read_json(args.semgrep),
                taint_rules=args.taint_rule, assume_taint=args.assume_taint)
        else:
            findings = graphio.read_json(args.findings)
        return _apply_findings(args, findings)

    if args.cmd == "test-link":
        from graphify_ext import test_link
        if bool(args.coverage) == bool(args.heuristic):
            sys.exit("error: pass exactly one of --coverage or --heuristic")
        from graphify_ext import graphio
        if args.coverage:
            findings = test_link.from_coverage(graphio.read_json(args.coverage))
        else:
            findings = test_link.heuristic(_load_graph(args.graph))
        if args.dry_run:
            json.dump(findings, sys.stdout, indent=2)
            print()
            return 0
        return _apply_findings(args, findings)

    if args.cmd == "config-scan":
        from graphify_ext import config_link
        findings = config_link.scan(Path(args.path))
        print(f"config-scan: {len(findings['edges'])} candidate edge(s)")
        if args.dry_run:
            json.dump(findings, sys.stdout, indent=2)
            print()
            return 0
        return _apply_findings(args, findings)

    if args.cmd == "reapply":
        from graphify_ext import edge_inject
        n = edge_inject.reapply(Path(args.out))
        print(f"re-applied {n} external edge(s)")
        return 0

    if args.cmd == "triage":
        from graphify_ext import graphio, triage
        report = graphio.read_json(args.report)
        if isinstance(report, dict):
            report = report.get("vulns", [])
        contexts = triage.triage_report(Path(args.graph), report,
                                        depth=args.depth, max_nodes=args.max_nodes)
        print(triage.summarize(contexts))
        if args.out:
            Path(args.out).write_text(json.dumps(contexts, indent=2), encoding="utf-8")
            print(f"full contexts written to {args.out}")
        return 0

    if args.cmd == "verify-fix":
        from graphify_ext import verify_fix
        if args.action == "snapshot":
            if not args.node:
                sys.exit("error: snapshot requires at least one --node")
            entry = verify_fix.snapshot(Path(args.out), args.node)
            print(f"snapshot of {len(entry['nodes'])} node(s) taken"
                  + (f"; UNRESOLVED: {entry['unresolved']}" if entry["unresolved"] else ""))
            return 1 if entry["unresolved"] else 0
        result = verify_fix.check(Path(args.out))
        if args.as_json:
            json.dump(result, sys.stdout, indent=2)
            print()
        else:
            print(verify_fix.format_check(result))
        return 0 if result["clean"] else 2

    return 0


def _apply_findings(args, findings: dict) -> int:
    from graphify_ext import edge_inject
    skipped = findings.get("skipped") or []
    if skipped:
        print(f"{len(skipped)} finding(s) skipped (not turned into edges):")
        seen = set()
        for s in skipped:
            key = s.get("reason")
            if key in seen:
                continue
            seen.add(key)
            print(f"  - {key}")
        print("  (nothing is dropped silently; re-run with --taint-rule/"
              "--assume-taint if these are taint findings)")
    graph_path = Path(args.graph)
    if not graph_path.exists():
        sys.exit(f"error: graph not found at {graph_path}")
    if not getattr(args, "no_store", False):
        edge_inject.store_findings(graph_path.parent, findings)
    report = edge_inject.inject(graph_path, findings)
    print(f"applied {report['applied']} edge(s) "
          f"(replaced {report['removed_previous']} previous external edge(s))")
    if report["unresolved"]:
        print(f"{len(report['unresolved'])} finding(s) did not resolve to graph nodes:")
        for u in report["unresolved"][:10]:
            print(f"  - {u.get('relation')}: {u.get('reason')} "
                  f"{u.get('source_ref')} -> {u.get('target_ref')}")
        if len(report["unresolved"]) > 10:
            print(f"  ... and {len(report['unresolved']) - 10} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
