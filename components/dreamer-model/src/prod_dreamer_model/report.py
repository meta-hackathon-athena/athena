"""End product: read the graph and print everything the dreamer concluded.

    remind-me style answer:
      "Takes the bus in the morning around 08:12 on weekdays."
      "Eats lunch at the food court around 11:38 on weekdays."
      ...

Outputs a terminal report, a markdown report and a JSON dump, all derived from
the graph's :Pattern and :LongTermMemory nodes plus their evidence links.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .graph import ProdGraph

PATTERN_ORDER = [
    "routine", "schedule", "commute", "location", "activity",
    "preference", "social", "anomaly", "other",
]
MEMORY_ORDER = [
    "routine", "schedule", "fact", "place", "preference",
    "reminder", "relationship", "other",
]
DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _bar(value: Any) -> str:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return "  ?  "
    c = max(0.0, min(1.0, c))
    return f"{c:0.2f} {'#' * int(round(c * 10)):<10}"


def _ts(ms: Any, tz: ZoneInfo) -> str:
    if ms is None:
        return "?"
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=tz).strftime("%a %d %b %Y %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def _days_text(days: Any) -> str:
    if not days:
        return ""
    try:
        return ", ".join(DOW_NAMES[int(d)] for d in days if 0 <= int(d) <= 6)
    except (TypeError, ValueError):
        return str(days)


def collect(graph: ProdGraph) -> dict[str, Any]:
    stats = graph.stats()
    patterns = graph.fetch_patterns()
    memories = graph.fetch_memories()
    for item in patterns + memories:
        node_id = item.get("pattern_id") or item.get("memory_id")
        item["evidence"] = graph.fetch_evidence(node_id) if node_id else []
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "graph": graph.config.graph_name,
        "session_id": graph.config.session_id,
        "coverage": stats.as_dict(),
        "coverage_first": _ts(stats.first_ms, graph.tz),
        "coverage_last": _ts(stats.last_ms, graph.tz),
        "patterns": patterns,
        "long_term_memories": memories,
        "notes": graph.fetch_notes(),
        "remember": [m["statement"] for m in memories if m.get("statement")],
    }


def _sort(items: list[dict], order: list[str]) -> list[dict]:
    def key(item: dict) -> tuple[int, float]:
        kind = item.get("kind") or "other"
        rank = order.index(kind) if kind in order else len(order)
        return (rank, -float(item.get("confidence") or 0))

    return sorted(items, key=key)


def render_text(data: dict[str, Any], show_evidence: bool = True) -> str:
    cov = data["coverage"]
    out: list[str] = []
    out.append("=" * 72)
    out.append("  VIXY DREAMER — WHAT THE USER IS LIKE")
    out.append("=" * 72)
    out.append(f"  graph      : {data['graph']}   session: {data['session_id']}")
    out.append(f"  observed   : {cov['days']} days, {cov['frames']} frames, "
               f"{cov['notes']} notes, {cov['classes']} distinct classes")
    out.append(f"  coverage   : {data['coverage_first']}  ->  {data['coverage_last']}")
    out.append(f"  dreamed    : {len(data['patterns'])} patterns, "
               f"{len(data['long_term_memories'])} long-term memories")

    memories = _sort(data["long_term_memories"], MEMORY_ORDER)
    out.append("")
    out.append("-" * 72)
    out.append("  LONG-TERM MEMORY (what Vixy will remember)")
    out.append("-" * 72)
    if not memories:
        out.append("  (none yet — run the dream cycle)")
    for i, m in enumerate(memories, 1):
        out.append("")
        out.append(f"  [{i}] {m.get('memory_id')}   ({m.get('kind')})")
        out.append(f"      {m.get('statement')}")
        out.append(f"      confidence : {_bar(m.get('confidence'))}   "
                   f"status: {m.get('status')}")
        if m.get("from_patterns"):
            out.append(f"      from       : {', '.join(m['from_patterns'])}")
        if show_evidence:
            _evidence_lines(out, m)

    patterns = _sort(data["patterns"], PATTERN_ORDER)
    out.append("")
    out.append("-" * 72)
    out.append("  PATTERNS (with the evidence behind them)")
    out.append("-" * 72)
    if not patterns:
        out.append("  (none yet)")
    for i, p in enumerate(patterns, 1):
        out.append("")
        out.append(f"  [{i}] {p.get('pattern_id')}   ({p.get('kind')}, "
                   f"{p.get('status')})")
        out.append(f"      {p.get('statement')}")
        out.append(f"      confidence : {_bar(p.get('confidence'))}")
        meta = []
        if p.get("time_window"):
            meta.append(f"window {p['time_window']}")
        if p.get("days"):
            meta.append(f"days {_days_text(p['days'])}")
        if p.get("about"):
            about = [a for a in p["about"] if a]
            if about:
                meta.append("about " + ", ".join(about))
        if meta:
            out.append(f"      {' | '.join(meta)}")
        if show_evidence:
            _evidence_lines(out, p)

    if data.get("notes"):
        out.append("")
        out.append("-" * 72)
        out.append("  SPOKEN NOTES (observed)")
        out.append("-" * 72)
        for n in data["notes"]:
            out.append(f"  [{n.get('kind'):<10}] {n.get('text')}")

    if data.get("remember"):
        out.append("")
        out.append("-" * 72)
        out.append("  PLAIN ANSWER — things Vixy now knows")
        out.append("-" * 72)
        for line in data["remember"]:
            out.append(f"   * {line}")
    out.append("")
    return "\n".join(out)


def _evidence_lines(out: list[str], item: dict) -> None:
    evidence = [e for e in (item.get("evidence") or []) if e]
    if not evidence:
        out.append("      evidence   : (none linked)")
        return
    labels: list[str] = []
    for e in evidence[:5]:
        ref = e.get("frame_id") or e.get("note_id") or (e.get("id") if e.get("label") == "Visit" else None) or e.get("day_id") or "?"
        if e.get("local_date"):
            ref = f"{ref} ({e['local_date']})"
        labels.append(f"{e.get('label', '?')}:{ref}")
    suffix = f"  … +{len(evidence) - 5} more" if len(evidence) > 5 else ""
    out.append(f"      evidence   : {len(evidence)} item(s) — " + ", ".join(labels) + suffix)


def render_markdown(data: dict[str, Any]) -> str:
    cov = data["coverage"]
    lines = [
        f"# Vixy dreamer report — `{data['graph']}`",
        "",
        f"- session: `{data['session_id']}`",
        f"- observed: **{cov['days']} days**, {cov['frames']} frames, "
        f"{cov['notes']} notes, {cov['classes']} classes",
        f"- coverage: {data['coverage_first']} → {data['coverage_last']}",
        f"- dreamed: **{len(data['long_term_memories'])} long-term memories**, "
        f"{len(data['patterns'])} patterns",
        "",
        "## Long-term memory",
        "",
    ]
    memories = _sort(data["long_term_memories"], MEMORY_ORDER)
    if not memories:
        lines.append("_none yet_")
    for m in memories:
        lines.append(
            f"- **{m.get('statement')}**  \n"
            f"  `{m.get('memory_id')}` · {m.get('kind')} · confidence "
            f"{float(m.get('confidence') or 0):.2f} · {len(m.get('evidence') or [])} evidence item(s)"
        )
    lines += ["", "## Patterns", ""]
    patterns = _sort(data["patterns"], PATTERN_ORDER)
    if not patterns:
        lines.append("_none yet_")
    for p in patterns:
        extra = []
        if p.get("time_window"):
            extra.append(p["time_window"])
        if p.get("days"):
            extra.append(_days_text(p["days"]))
        suffix = f" _({'; '.join(extra)})_" if extra else ""
        lines.append(
            f"- **{p.get('statement')}**{suffix}  \n"
            f"  `{p.get('pattern_id')}` · {p.get('kind')} · confidence "
            f"{float(p.get('confidence') or 0):.2f} · {len(p.get('evidence') or [])} evidence item(s)"
        )
    if data.get("notes"):
        lines += ["", "## Spoken notes", ""]
        for n in data["notes"]:
            lines.append(f"- `{n.get('kind')}` — {n.get('text')}")
    return "\n".join(lines) + "\n"


def write_outputs(graph: ProdGraph, out_dir: str | Path = "dream_output") -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = collect(graph)
    json_path = out / "dream_output.json"
    md_path = out / "dream_report.md"
    json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(data), encoding="utf-8")
    # Local import: readable.py reuses the helpers above, so importing it at
    # module level would be circular.
    from .readable import write_readable

    text_path = write_readable(data, graph, out)
    return {"json": json_path, "markdown": md_path, "text": text_path}
