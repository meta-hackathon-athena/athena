"""Export a user's memory graph to a self-contained interactive HTML page.

  uv run python -m memory.cli viz            # writes data/graph_<user>.html and opens it
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .store import TZ, MemoryStore

LAYER = {"Visit": "episodic", "Note": "episodic", "Place": "semantic", "Tag": "semantic", "Fact": "semantic",
         "Commitment": "agent", "Question": "agent", "AgentLog": "agent"}


def _fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%a %Y-%m-%d %H:%M")


def _caption(label: str, p: dict) -> str:
    if label == "Visit":
        t = datetime.fromtimestamp(p["start_ms"] / 1000, TZ).strftime("%a %H:%M")
        return f"{p.get('place_type', '?')}\n{t}"
    if label == "Place":
        return p.get("name") or p.get("place_type", "place")
    if label == "Tag":
        return p.get("name", "")
    if label == "Fact":
        kind = p.get("kind", "fact")
        if kind == "usual_activity":
            return f"usually {p.get('activity', '')}"
        if kind == "preference":
            return ("likes" if p.get("positive") else "dislikes") + " (said)"
        return kind.replace("_", " ")
    if label == "Note":
        text = p.get("text", "")
        return ("⏰ " if p.get("kind") == "reminder" else "🗣 ") + (text[:26] + "…" if len(text) > 26 else text)
    if label == "Commitment":
        t = datetime.fromtimestamp(p["start_ms"] / 1000, TZ).strftime("%a %H:%M")
        return f"📅 {p.get('title', '')}\n{t} · {p.get('status', '')}"
    if label == "Question":
        return f"❓ {p.get('status', '')}"
    if label == "AgentLog":
        return p.get("action", "log").lower()
    return label


def _display(props: dict) -> dict:
    out = {}
    for k, v in props.items():
        if k.endswith("_ms") and isinstance(v, (int, float)):
            out[k] = _fmt_ms(int(v))
        elif k == "start_min" or k == "usual_arrival_min":
            out[k] = f"{int(v) // 60:02d}:{int(v) % 60:02d}"
        elif k == "dow" and isinstance(v, int):
            out[k] = "Mon Tue Wed Thu Fri Sat Sun".split()[v]
        elif k == "dows" and isinstance(v, list):
            out[k] = ", ".join("Mon Tue Wed Thu Fri Sat Sun".split()[d] for d in v)
        elif isinstance(v, dict) and "latitude" in v:
            out[k] = f"{v['latitude']:.5f}, {v['longitude']:.5f}"
        else:
            out[k] = v
    return out


def export(store: MemoryStore) -> dict:
    nodes = []
    for nid, labels, props in store.q("MATCH (n) RETURN id(n), labels(n), properties(n)"):
        label, props = labels[0], dict(props)
        nodes.append({
            "id": nid, "label": label, "layer": LAYER.get(label, "other"),
            "caption": _caption(label, props), "props": _display(props),
            "sensitive": bool(props.get("sensitive")),
            "inactive": props.get("status") in ("inactive", "done"),
            "sort": props.get("start_ms") or props.get("ts_ms") or 0,
        })
    edges = [{"from": a, "to": b, "type": t, "props": dict(p)}
             for a, t, b, p in store.q("MATCH (a)-[r]->(b) RETURN id(a), type(r), id(b), properties(r)")]
    return {"nodes": nodes, "edges": edges, "stats": store.stats(),
            "generated": datetime.now(TZ).strftime("%Y-%m-%d %H:%M")}


def write_html(store: MemoryStore, out: str | Path, user: str) -> Path:
    data = json.dumps(export(store), default=str).replace("</", "<\\/")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(TEMPLATE.replace("__DATA__", data).replace("__USER__", user), encoding="utf-8")
    return out


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Veyra Memory Graph</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
:root{--bg:#f7f8fa;--panel:#fff;--text:#1d2330;--muted:#6b7385;--line:#e3e6ec;--accent:#3b6fd8}
@media (prefers-color-scheme:dark){:root{--bg:#14171d;--panel:#1c2028;--text:#e6e9ef;--muted:#9099ab;--line:#2c323d;--accent:#7aa2f7}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column}
header{display:flex;flex-wrap:wrap;gap:14px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:15px;margin:0 8px 0 0}
.grp{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.grp b{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
label.chip{display:inline-flex;gap:5px;align-items:center;padding:3px 8px;border:1px solid var(--line);border-radius:999px;cursor:pointer;user-select:none}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block}
input[type=search]{padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);min-width:200px}
button{padding:5px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);cursor:pointer}button:hover{border-color:var(--accent)}
main{flex:1;display:flex;min-height:0}#net{flex:1;min-width:0}
aside{width:360px;max-width:45vw;border-left:1px solid var(--line);background:var(--panel);overflow:auto;padding:14px 16px}
aside h2{font-size:16px;margin:4px 0 2px}.badge{font-size:11px;padding:2px 8px;border-radius:999px;color:#fff}
table{width:100%;border-collapse:collapse;margin:10px 0;font-size:13px}td{padding:4px 6px;border-bottom:1px solid var(--line);vertical-align:top;word-break:break-word}td:first-child{color:var(--muted);width:38%}
.conn{margin:10px 0 4px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.link{display:block;padding:3px 6px;border-radius:6px;cursor:pointer}.link:hover{background:var(--bg)}
.muted{color:var(--muted)}.warn{color:#d9534f;font-weight:600}
@media (max-width:760px){main{flex-direction:column}aside{width:auto;max-width:none;height:40vh;border-left:0;border-top:1px solid var(--line)}}
</style></head><body>
<header>
  <h1>Veyra memory · <span class="muted">__USER__</span></h1>
  <div class="grp"><b>Episodic</b><span id="f-episodic"></span></div>
  <div class="grp"><b>Semantic</b><span id="f-semantic"></span></div>
  <div class="grp"><b>Agent</b><span id="f-agent"></span></div>
  <div class="grp"><input type="search" id="q" placeholder="Search (koufu, laptop, eating…)"></div>
  <div class="grp"><button id="all">Show all</button><button id="fit">Fit</button>
    <label class="chip"><input type="checkbox" id="physics" checked> physics</label></div>
  <div class="grp muted" id="stats"></div>
</header>
<main><div id="net"></div><aside id="side"><p class="muted">Click a node to inspect it. Double-click to focus on its neighbourhood (double-click again to expand). Tags are hidden by default; toggle them above.</p>
<p class="muted"><b>Episodic</b> = what happened (visits, things you said).<br><b>Semantic</b> = what the system believes (places, concepts, facts). Facts link to their evidence via <i>SUPPORTED_BY</i>.</p></aside></main>
<script>
const DATA = __DATA__;
const STYLE = {
  Visit:{color:"#4C8BF5",shape:"dot",size:14}, Note:{color:"#8EC5FF",shape:"box"},
  Place:{color:"#2BB673",shape:"diamond",size:22}, Tag:{color:"#9AA3B2",shape:"dot",size:7},
  Fact:{color:"#F2A541",shape:"hexagon",size:20},
  Commitment:{color:"#B07CF7",shape:"square",size:16}, Question:{color:"#F7768E",shape:"triangle",size:14},
  AgentLog:{color:"#C0C6D2",shape:"dot",size:6}
};
const EDGE = {AT:"#2BB673",SAW:"#9AA3B2",NEXT:"#4C8BF5",DURING:"#8EC5FF",ABOUT:"#F2A541",SUPPORTED_BY:"#E07A5F",CONCERNS:"#C0C6D2"};
const dark = matchMedia("(prefers-color-scheme: dark)").matches;
const fontColor = dark ? "#e6e9ef" : "#1d2330";
const byId = new Map(DATA.nodes.map(n => [n.id, n]));
const visible = {Visit:true, Note:true, Place:true, Tag:false, Fact:true, Commitment:true, Question:true, AgentLog:false};
let focus = null;  // Set of node ids or null

const nodes = new vis.DataSet(DATA.nodes.map(n => {
  const s = STYLE[n.label] || {color:"#888",shape:"dot"};
  return {id:n.id, label:n.caption, shape:s.shape, size:s.size,
    color:{background:n.inactive ? "#c9ced8" : s.color, border:n.sensitive ? "#d9534f" : s.color,
           highlight:{background:s.color, border:"#222"}},
    borderWidth:n.sensitive ? 3 : 1, shapeProperties:{borderDashes:n.inactive ? [4,3] : false},
    font:{color:n.label === "Note" ? "#1d2330" : fontColor, size:n.label === "Tag" ? 10 : 12, multi:false},
    title:n.label + (n.sensitive ? " (sensitive)" : "")};
}));
const edges = new vis.DataSet(DATA.edges.map((e, i) => ({id:i, from:e.from, to:e.to, type:e.type,
  arrows:"to", color:{color:EDGE[e.type] || "#999", opacity:e.type === "SAW" ? .35 : .8},
  dashes:e.type === "SUPPORTED_BY", width:e.type === "NEXT" ? 2 : 1,
  title:e.type + (Object.keys(e.props).length ? " " + JSON.stringify(e.props) : "")})));
const neighbours = new Map();
DATA.edges.forEach(e => {
  (neighbours.get(e.from) || neighbours.set(e.from, []).get(e.from)).push({id:e.to, type:e.type, dir:"out"});
  (neighbours.get(e.to) || neighbours.set(e.to, []).get(e.to)).push({id:e.from, type:e.type, dir:"in"});
});

const nodeView = new vis.DataView(nodes, {filter: n => {
  const d = byId.get(n.id);
  if (focus) return focus.has(n.id);
  return visible[d.label];
}});
const edgeView = new vis.DataView(edges, {filter: e => true});
const net = new vis.Network(document.getElementById("net"), {nodes:nodeView, edges:edgeView}, {
  physics:{solver:"forceAtlas2Based", forceAtlas2Based:{gravitationalConstant:-45, springLength:110}, stabilization:{iterations:250}},
  interaction:{hover:true, tooltipDelay:150}, nodes:{font:{face:"system-ui"}}, edges:{smooth:{type:"continuous"}, font:{size:9}}
});

function refresh(){ nodeView.refresh(); edgeView.refresh(); }
function makeFilters(){
  for (const [label, s] of Object.entries(STYLE)) {
    const n = DATA.nodes.filter(x => x.label === label).length;
    const el = document.createElement("label"); el.className = "chip";
    el.innerHTML = `<input type="checkbox"><span class="dot" style="background:${s.color}"></span>`;
    el.append(`${label} (${n})`);
    const cb = el.querySelector("input"); cb.checked = visible[label];
    cb.onchange = () => { visible[label] = cb.checked; focus = null; refresh(); };
    document.getElementById("f-" + ({Visit:"episodic",Note:"episodic",Commitment:"agent",Question:"agent",AgentLog:"agent"}[label] || "semantic")).append(el);
  }
  const s = DATA.stats;
  document.getElementById("stats").textContent = `${s.visits} visits · ${s.places} places · ${s.facts} facts · ${s.notes} notes · exported ${DATA.generated}`;
}

function inspect(id){
  const n = byId.get(id), side = document.getElementById("side");
  side.replaceChildren();
  const h = document.createElement("h2"); h.textContent = n.caption.replace("\n", " · "); side.append(h);
  const b = document.createElement("span"); b.className = "badge"; b.style.background = (STYLE[n.label]||{}).color || "#888";
  b.textContent = `${n.label} · ${n.layer}`; side.append(b);
  if (n.sensitive) { const w = document.createElement("div"); w.className = "warn"; w.textContent = "Sensitive: never turned into facts"; side.append(w); }
  const t = document.createElement("table");
  for (const [k, v] of Object.entries(n.props)) {
    const tr = t.insertRow(); tr.insertCell().textContent = k;
    tr.insertCell().textContent = Array.isArray(v) ? v.join(", ") : String(v);
  }
  side.append(t);
  const groups = {};
  (neighbours.get(id) || []).forEach(x => { const key = (x.dir === "out" ? "→ " : "← ") + x.type; (groups[key] ||= []).push(x.id); });
  for (const [key, ids] of Object.entries(groups)) {
    const c = document.createElement("div"); c.className = "conn"; c.textContent = `${key} (${ids.length})`; side.append(c);
    ids.sort((a, b) => (byId.get(b).sort || 0) - (byId.get(a).sort || 0)).slice(0, 60).forEach(nid => {
      const m = byId.get(nid), a = document.createElement("span"); a.className = "link";
      a.textContent = `${m.label}: ${m.caption.replace("\n", " · ")}`;
      a.onclick = () => { if (focus) { focus.add(nid); refresh(); } net.selectNodes([nid]); net.focus(nid, {scale:1.1, animation:true}); inspect(nid); };
      side.append(a);
    });
  }
}

function focusOn(id){
  if (!focus) focus = new Set([id]);
  const frontier = [...focus];
  frontier.forEach(f => (neighbours.get(f) || []).forEach(x => focus.add(x.id)));
  focus.add(id); refresh(); setTimeout(() => net.fit({animation:true}), 300);
}

net.on("click", p => { if (p.nodes.length) inspect(p.nodes[0]); });
net.on("doubleClick", p => { if (p.nodes.length) focusOn(p.nodes[0]); });
document.getElementById("all").onclick = () => { focus = null; refresh(); net.fit({animation:true}); };
document.getElementById("fit").onclick = () => net.fit({animation:true});
document.getElementById("physics").onchange = e => net.setOptions({physics:{enabled:e.target.checked}});
document.getElementById("q").oninput = e => {
  const q = e.target.value.trim().toLowerCase();
  if (!q) { focus = null; refresh(); return; }
  const hits = DATA.nodes.filter(n => (n.caption + " " + JSON.stringify(n.props)).toLowerCase().includes(q)).map(n => n.id);
  focus = new Set(hits); hits.forEach(h => (neighbours.get(h) || []).forEach(x => { if (visible[byId.get(x.id).label]) focus.add(x.id); }));
  refresh(); if (hits.length) { net.selectNodes(hits); setTimeout(() => net.fit({animation:true}), 300); }
};
makeFilters();
</script></body></html>
"""
