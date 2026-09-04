"""Live progress dashboard for the fix eval (reads results.jsonl + run logs).

    python bench/fixeval/dashboard.py [--port 8765]

Renders on every request, so the page is always current; the page refreshes
itself every 15 s. Read-only: never touches the eval's files.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import statistics
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARMS = ("graph", "graph-guided", "nograph")
REPS = (0, 1)


def load():
    tasks = json.loads((HERE / "tasks.json").read_text(encoding="utf-8")) if (HERE / "tasks.json").exists() else []
    rows = []
    if (HERE / "results.jsonl").exists():
        for line in (HERE / "results.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    running = []
    for log in sorted(HERE.glob("run-*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        last_run = None
        for ln in lines:
            if "agent running" in ln:
                last_run = ln
            elif last_run and "resolved=" in ln and ln.split()[0] == last_run.split()[0]:
                last_run = None
        if last_run:
            parts = last_run.split()
            age = time.time() - log.stat().st_mtime
            if age < 1800:                 # a log idle for 30 min is a dead stream, not a run
                running.append((parts[0], parts[1], log.name, age))
    return tasks, rows, running


def render() -> str:
    tasks, rows, running = load()
    keys = sorted(t["key"] for t in tasks)
    total_cells = len(keys) * len(ARMS) * len(REPS)
    cells = {}
    for r in rows:                       # first recording of a cell wins (parallel-worker duplicates)
        cells.setdefault((r["key"], r["arm"], int(r.get("rep", 0))), r)
    done = len(cells)
    walls = [r["agent"].get("wall_s") for r in rows if r.get("agent", {}).get("wall_s")]
    mean_wall = statistics.mean(walls) if walls else 160
    streams = max(1, len(running))
    remaining = max(0, total_cells - done)
    eta_min = remaining * mean_wall / streams / 60

    def pct(a, b):
        return f"{100 * a / b:.0f}%" if b else "-"

    # per arm x rep
    arm_rows = []
    for arm in ARMS:
        for rep in REPS:
            cs = [cells[(k, arm, rep)] for k in keys if (k, arm, rep) in cells]
            res = sum(1 for c in cs if c["resolved"])
            arm_rows.append((arm, rep, len(cs), len(keys), res))
    # paired: tasks where every arm has >=1 rep
    common = [k for k in keys if all(any((k, a, r) in cells for r in REPS) for a in ARMS)]
    paired = []
    for arm in ARMS:
        cs = [cells[(k, arm, r)] for k in common for r in REPS if (k, arm, r) in cells]
        res = sum(1 for c in cs if c["resolved"])
        turns = [c["agent"].get("turns") or 0 for c in cs]
        cost = [c["agent"].get("cost_usd") or 0 for c in cs]
        paired.append((arm, res, len(cs), (sum(turns) / len(turns)) if turns else 0,
                       (sum(cost) / len(cost)) if cost else 0))
    total_cost = sum((r["agent"].get("cost_usd") or 0) for r in rows)

    # task grid
    def cell_html(k, arm):
        out = []
        for rep in REPS:
            c = cells.get((k, arm, rep))
            if c is None:
                cls, txt = "pending", "·"
            else:
                f2p = c["fail_to_pass"]
                ok = sum(1 for v in f2p.values() if v == "PASSED")
                if c["resolved"]:
                    cls, txt = "ok", "✓"
                elif c.get("pass_to_pass_broken"):
                    cls, txt = "broke", f"✗{len(c['pass_to_pass_broken'])}"
                elif ok:
                    cls, txt = "partial", f"{ok}/{len(f2p)}"
                else:
                    cls, txt = "fail", "✗"
                if not [f for f in c["edited_files"] if not f.startswith(".git/")]:
                    txt += "∅"
            out.append(f'<span class="c {cls}" title="{arm} rep{rep}">{txt}</span>')
        return "".join(out)

    grid = "".join(
        f"<tr><td class='k'>{html.escape(k)}</td>" + "".join(f"<td>{cell_html(k, a)}</td>" for a in ARMS) + "</tr>"
        for k in keys)
    run_html = "".join(
        f"<li><b>{html.escape(a)}</b> · {html.escape(k)} <span class='dim'>({html.escape(lg)}, {age/60:.0f} min since last write)</span></li>"
        for k, a, lg, age in running) or "<li class='dim'>no stream is mid-run</li>"
    recent = sorted(rows, key=lambda r: r.get("agent", {}).get("wall_s", 0), reverse=False)[-8:]
    recent_html = "".join(
        f"<li>{html.escape(r['key'])} · {r['arm']} rep{r.get('rep',0)} · "
        f"{'RESOLVED' if r['resolved'] else 'failed'} · t{r['agent'].get('turns')} · ${(r['agent'].get('cost_usd') or 0):.2f}</li>"
        for r in rows[-8:][::-1])

    arm_table = "".join(
        f"<tr><td>{a}</td><td>{rep}</td><td>{n}/{tot}</td><td><div class='bar'><div style='width:{pct(n,tot)}'></div></div></td><td>{res}/{n} resolved</td></tr>"
        for a, rep, n, tot, res in arm_rows)
    paired_table = "".join(
        f"<tr><td>{a}</td><td>{res}/{n}</td><td>{pct(res,n)}</td><td>{t:.1f}</td><td>${c:.2f}</td></tr>"
        for a, res, n, t, c in paired)

    return f"""<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="15">
<title>fix-eval progress</title>
<style>
body{{font:14px/1.45 system-ui,sans-serif;margin:0;padding:20px 28px;background:#0f1216;color:#e6e6e6}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:14px;margin:22px 0 8px;color:#9fb3c8;text-transform:uppercase;letter-spacing:.06em}}
.dim{{color:#8a94a6}} .big{{font-size:34px;font-weight:600}} .kpis{{display:flex;gap:28px;flex-wrap:wrap;margin:14px 0 6px}}
.kpi small{{display:block;color:#8a94a6}}
table{{border-collapse:collapse;width:100%}} td,th{{padding:4px 8px;text-align:left;border-bottom:1px solid #222a35;vertical-align:middle}}
th{{color:#9fb3c8;font-weight:500}} td.k{{font-family:ui-monospace,monospace;font-size:12px;color:#c9d1d9}}
.bar{{width:220px;height:8px;background:#222a35;border-radius:4px;overflow:hidden}} .bar div{{height:100%;background:#4c9be8}}
.c{{display:inline-block;min-width:26px;text-align:center;padding:1px 6px;margin-right:4px;border-radius:4px;font-family:ui-monospace,monospace;font-size:12px}}
.ok{{background:#1f6f43}} .fail{{background:#7a2e2e}} .partial{{background:#7a5a1e}} .broke{{background:#8a2a5a}} .pending{{background:#222a35;color:#667}}
ul{{margin:4px 0;padding-left:18px}} .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:28px}}
@media (max-width:900px){{.grid2{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>fix-eval progress <span class="dim">— {time.strftime('%H:%M:%S')}, auto-refresh 15 s</span></h1>
<div class="dim">{len(keys)} verifiable tasks × {len(ARMS)} arms × {len(REPS)} reps = {total_cells} cells · mean {mean_wall:.0f} s per run · {streams} stream(s) active</div>
<div class="kpis">
 <div class="kpi"><span class="big">{done}/{total_cells}</span><small>cells done ({pct(done,total_cells)})</small></div>
 <div class="kpi"><span class="big">~{eta_min:.0f} min</span><small>estimated remaining</small></div>
 <div class="kpi"><span class="big">${total_cost:.2f}</span><small>spent so far</small></div>
</div>
<div class="grid2">
<div>
<h2>per arm and repetition</h2>
<table><tr><th>arm</th><th>rep</th><th>done</th><th></th><th>resolved</th></tr>{arm_table}</table>
<h2>paired — the {len(common)} task(s) every arm has run</h2>
<table><tr><th>arm</th><th>resolved runs</th><th>rate</th><th>mean turns</th><th>mean cost</th></tr>{paired_table}</table>
<div class="dim" style="margin-top:6px">Rates average over repetitions. Read this table, not the raw counts, when arms have run different numbers of tasks.</div>
</div>
<div>
<h2>running now</h2><ul>{run_html}</ul>
<h2>most recent results</h2><ul>{recent_html}</ul>
</div>
</div>
<h2>task grid <span class="dim" style="text-transform:none;letter-spacing:0">(two cells per arm = rep 0, rep 1 · ✓ resolved · ✗ failed · n/m partial · ✗k broke k passing tests · ∅ no file edited · · pending)</span></h2>
<table><tr><th>task</th>{"".join(f"<th>{a}</th>" for a in ARMS)}</tr>{grid}</table>
</body></html>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass


def render_snapshot() -> str:
    """Theme-aware static snapshot for hosting off-machine (claude.ai artifact):
    same data as the live page, no auto-refresh, no document wrapper."""
    page = render()
    body = page[page.index("<body>") + 6: page.index("</body>")]
    body = body.replace("auto-refresh 15 s", "snapshot; republished by Claude as the run progresses")
    css = """
<title>Fix-Eval Run Board</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root{--bg:#f5f6f3;--fg:#1c232d;--dim:#5f6b7a;--line:#d9dde3;--track:#e6e9ee;--accent:#3b7dd8;--ok:#2e8b57;--fail:#b3413a;--part:#b8862b;--broke:#9c3a78;--pend:#c9ced6;--pendfg:#7a828e}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#11161c;--fg:#e4e8ee;--dim:#8d99a8;--line:#242c37;--track:#1e2531;--accent:#5b9cf0;--ok:#2f8f5a;--fail:#a8423b;--part:#a67f2a;--broke:#8e3a70;--pend:#1e2531;--pendfg:#6b7482}}
:root[data-theme="dark"]{--bg:#11161c;--fg:#e4e8ee;--dim:#8d99a8;--line:#242c37;--track:#1e2531;--accent:#5b9cf0;--ok:#2f8f5a;--fail:#a8423b;--part:#a67f2a;--broke:#8e3a70;--pend:#1e2531;--pendfg:#6b7482}
body{font:14px/1.45 "IBM Plex Sans",system-ui,sans-serif;margin:0;padding:24px 28px;background:var(--bg);color:var(--fg)}
h1{font-size:20px;font-weight:600;margin:0 0 4px;text-wrap:balance} h2{font-size:12px;margin:24px 0 8px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;font-weight:500}
.dim{color:var(--dim)} .big{font-size:34px;font-weight:600;font-variant-numeric:tabular-nums} .kpis{display:flex;gap:32px;flex-wrap:wrap;margin:14px 0 6px}
.kpi small{display:block;color:var(--dim)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums} td,th{padding:5px 8px;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--dim);font-weight:500} td.k{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px}
.bar{width:200px;max-width:100%;height:8px;background:var(--track);border-radius:4px;overflow:hidden} .bar div{height:100%;background:var(--accent)}
.c{display:inline-block;min-width:26px;text-align:center;padding:1px 6px;margin-right:4px;border-radius:4px;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px;color:#fff}
.ok{background:var(--ok)} .fail{background:var(--fail)} .partial{background:var(--part)} .broke{background:var(--broke)} .pending{background:var(--pend);color:var(--pendfg)}
ul{margin:4px 0;padding-left:18px} .grid2{display:grid;grid-template-columns:1fr 1fr;gap:32px} .wrap{overflow-x:auto}
@media (max-width:900px){.grid2{grid-template-columns:1fr}}
</style>"""
    return css + '<div class="wrap">' + body + "</div>"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--snapshot", help="write a theme-aware static snapshot to this path and exit")
    args = ap.parse_args()
    if args.snapshot:
        Path(args.snapshot).write_text(render_snapshot(), encoding="utf-8")
        print(f"snapshot -> {args.snapshot}")
        raise SystemExit(0)
    print(f"dashboard on http://127.0.0.1:{args.port}")
    HTTPServer(("127.0.0.1", args.port), H).serve_forever()
