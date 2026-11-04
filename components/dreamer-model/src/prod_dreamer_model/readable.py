"""Human-readable narrative of a dream cycle: input first, conclusions second.

Writes ``dream_readable.txt`` — a plain-text document you can read top to bottom:

  PART 1  what was provided (a day-by-day diary reconstructed from the ingested
          frames, plus the raw node shape), and
  PART 2  what the dreamer concluded, with confidence and evidence.

It is derived entirely from the graph, so it always matches what was dreamed.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .graph import ProdGraph
from .report import DOW_NAMES, MEMORY_ORDER, PATTERN_ORDER, _days_text, _sort

WIDTH = 78

# Scene classes that name a place, most specific first. A frame's "hero" is the
# first of these it contains, so noise like "ceiling" or "poster" never wins.
SCENE_ORDER = [
    "bus interior", "food court", "canteen", "classroom", "library", "gym",
    "supermarket", "laboratory", "office", "cafe", "clinic", "hospital",
    "temple", "church", "mosque", "park", "bedroom", "kitchen", "living room",
    "home",
]
GAP_MIN = 12      # minutes without frames before a stay is split
MIN_STAY_MIN = 4  # shorter blips are dropped from the diary

PLACE_WORDS = {
    "bus interior": "on the bus",
    "food court": "at the food court",
    "canteen": "at the canteen",
    "classroom": "in class",
    "library": "in the library",
    "gym": "at the gym",
    "supermarket": "at the supermarket",
    "laboratory": "in the lab",
    "office": "at the office",
    "cafe": "at a cafe",
    "clinic": "at a clinic",
    "hospital": "at a hospital",
    "park": "in a park",
    "bedroom": "at home",
    "kitchen": "at home",
    "living room": "at home",
    "home": "at home",
    "other": "elsewhere",
}


def rule(title: str = "") -> str:
    if title:
        return f"{title} ".ljust(WIDTH, "-")[:WIDTH]
    return "-" * WIDTH


def box(title: str) -> str:
    return "=" * WIDTH + f"\n  {title}\n" + "=" * WIDTH


def hhmm(minutes: int) -> str:
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _hero(classes: list[str]) -> str:
    for name in SCENE_ORDER:
        if name in classes:
            return name
    return "other"


def _smooth(heroes: list[str], window: int = 2) -> list[str]:
    """Absorb single-frame dropouts so one missed class cannot split a stay."""
    out: list[str] = []
    for i, hero in enumerate(heroes):
        lo, hi = max(0, i - window), min(len(heroes), i + window + 1)
        counts = Counter(heroes[lo:hi])
        best = counts.most_common()
        top = {name for name, count in best if count == best[0][1]}
        out.append(hero if hero in top else best[0][0])
    return out


def _stay_segments(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse a day's frames into stays: hero class, start/end, top classes."""
    if not frames:
        return []
    heroes = _smooth([
        _hero(f.get("classes") or []) for f in frames
    ])

    runs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for frame, hero in zip(frames, heroes):
        minute = frame.get("local_min")
        if minute is None:
            continue
        if current is None or current["hero"] != hero:
            if current:
                runs.append(current)
            current = {
                "hero": hero,
                "start": minute,
                "end": minute,
                "frames": 1,
                "classes": Counter(frame.get("classes") or []),
            }
        else:
            current["end"] = minute
            current["frames"] += 1
            current["classes"].update(frame.get("classes") or [])
    if current:
        runs.append(current)

    # Drop blips, then re-join the same place either side of a dropped blip.
    kept = [r for r in runs if r["end"] - r["start"] >= MIN_STAY_MIN or r["frames"] >= 10]
    merged: list[dict[str, Any]] = []
    for run in kept:
        # A short unlabelled run is a walk between two places: fold it into
        # whichever stay it interrupts rather than printing "elsewhere".
        if (
            run["hero"] == "other"
            and run["end"] - run["start"] < 6
            and not (len(merged) > 1 and merged[-1]["hero"] == "other")
            and merged
        ):
            merged[-1]["end"] = max(merged[-1]["end"], run["end"])
            merged[-1]["frames"] += run["frames"]
            continue
        if (
            merged
            and merged[-1]["hero"] == run["hero"]
            and run["start"] - merged[-1]["end"] <= GAP_MIN
        ):
            last = merged[-1]
            last["end"] = run["end"]
            last["frames"] += run["frames"]
            last["classes"].update(run["classes"])
        else:
            merged.append(run)
    return merged


def _timeline(graph: ProdGraph) -> list[dict[str, Any]]:
    """One entry per local day: stays + notes, chronological."""
    frames = graph.fetch_frame_projection()
    notes = graph.fetch_notes()
    by_day: dict[str, list[dict]] = {}
    for frame in frames:
        day = frame.get("local_date")
        if day:
            by_day.setdefault(day, []).append(frame)

    notes_by_day: dict[str, list[dict]] = {}
    for note in notes:
        day = note.get("day_id")
        if not day:
            continue
        local = datetime.fromtimestamp(int(note["ts_ms"]) / 1000, graph.tz)
        notes_by_day.setdefault(day, []).append(
            {**note, "minute": local.hour * 60 + local.minute}
        )

    days: list[dict[str, Any]] = []
    for day in sorted(by_day):
        frames = sorted(by_day[day], key=lambda f: f["ts_ms"])
        stays = _stay_segments(frames)
        items: list[dict[str, Any]] = []
        for stay in stays:
            top = [name for name, _ in stay["classes"].most_common(5)]
            items.append({
                "kind": "stay",
                "start": stay["start"],
                "end": stay["end"],
                "hero": stay["hero"],
                "top": top,
                "frames": stay["frames"],
            })
        for note in notes_by_day.get(day, []):
            items.append({
                "kind": "note",
                "start": note["minute"],
                "kind_label": note.get("kind"),
                "text": note.get("text"),
            })
        items.sort(key=lambda i: (i["start"], 0 if i["kind"] == "stay" else 1))
        days.append({"day": day, "items": items, "frames": len(frames)})
    return days


def _raw_sample(graph: ProdGraph) -> str:
    """One node in the exact shape the perception layer emits."""
    frames = graph.fetch_frame_projection(limit=1)
    if not frames:
        return "  (no frames)"
    frame = frames[0]
    edges = graph.rows(
        """
        MATCH (f:Frame {frame_id: $id})-[s:SAW]->(c:Class)
        RETURN c.name AS name, s.count AS count, s.confidence AS confidence
        ORDER BY c.name
        """,
        {"id": frame["frame_id"]},
    )
    classes = [
        {"class": e["name"], "count": int(e.get("count") or 1),
         "confidence": round(float(e.get("confidence") or 0), 2)}
        for e in edges
    ]
    stamp = datetime.fromtimestamp(int(frame["ts_ms"]) / 1000, timezone.utc)
    node: dict[str, Any] = {
        "frame_id": frame["frame_id"],
        "timestamp": stamp.isoformat(),
        "classes": classes,
    }
    if frame.get("lat") is not None:
        node["gps"] = {"lat": round(frame["lat"], 4), "lon": round(frame["lon"], 4)}
    text = json.dumps(node, indent=2)
    return "\n".join("  " + line for line in text.splitlines())


def render_readable(data: dict[str, Any], graph: ProdGraph) -> str:
    cov = data["coverage"]
    out: list[str] = []
    out.append(box("VIXY DREAMER — READABLE SUMMARY"))
    out.append("")
    out.append(f"  graph      : {data['graph']}")
    out.append(f"  session    : {data['session_id']}")
    out.append(f"  observed   : {cov['days']} days, {cov['frames']:,} frames, "
               f"{cov['notes']} spoken notes, {cov['classes']} classes")
    out.append(f"  coverage   : {data['coverage_first']}  ->  {data['coverage_last']}")
    out.append(f"  dreamed    : {len(data['patterns'])} patterns, "
               f"{len(data['long_term_memories'])} long-term memories")
    out.append("")
    out.append("  This file reads top to bottom: first what the wearable provided,")
    out.append("  then what the dreamer concluded from it.")
    out.append("")

    # -- PART 1 ----------------------------------------------------------
    out.append(box("PART 1 — WHAT WAS PROVIDED"))
    out.append("")
    out.append("  The perception layer emitted one record per sampled frame. This is")
    out.append("  the whole raw input; nothing below is inferred. Example node:")
    out.append("")
    out.append(_raw_sample(graph))
    out.append("")
    out.append(f"  Those records went through context-engine into SQLite, then into")
    out.append(f"  the graph as frames. {cov['frames']:,} frames over {cov['days']} days "
               f"is too much to")
    out.append("  read one by one, so here is the same data as a day-by-day diary.")
    out.append("")

    for day in _timeline(graph):
        out.append(rule())
        out.append(f"  {day['day']}   ({day['frames']:,} frames)")
        out.append(rule())
        for item in day["items"]:
            if item["kind"] == "stay":
                where = PLACE_WORDS.get(item["hero"], f"({item['hero']})")
                top = ", ".join(item["top"][:4])
                span = f"{hhmm(item['start'])}-{hhmm(item['end'])}"
                out.append(f"    {span}  {where:<20} {top}")
            else:
                label = (item.get("kind_label") or "note").upper()
                text = (item.get("text") or "").strip()
                out.append(f"    {hhmm(item['start'])}  {label:<11} \"{text}\"")
        out.append("")

    # -- PART 2 ----------------------------------------------------------
    patterns = _sort(data["patterns"], PATTERN_ORDER)
    memories = _sort(data["long_term_memories"], MEMORY_ORDER)
    out.append(box("PART 2 — WHAT THE DREAMER CONCLUDED"))
    out.append("")
    out.append(f"  {len(patterns)} patterns and {len(memories)} long-term memories were "
               "written to the graph.")
    out.append("  Each one is linked to the frames, notes or days that support it.")
    out.append("")

    out.append(rule("  A. LONG-TERM MEMORY — what Vixy will remember"))
    out.append("")
    if not memories:
        out.append("    (none)")
    for i, m in enumerate(memories, 1):
        out.append(f"  {i:>2}. {m.get('statement')}")
        bits = [f"{m.get('kind')}", f"confidence {float(m.get('confidence') or 0):.2f}",
                f"{len([e for e in (m.get('evidence') or []) if e])} evidence item(s)"]
        out.append(f"      {' | '.join(bits)}")
        if m.get("from_patterns"):
            out.append(f"      distilled from: {', '.join(m['from_patterns'])}")
        out.append(f"      id: {m.get('memory_id')}")
        out.append("")

    out.append(rule("  B. PATTERNS — what recurs, and the evidence behind it"))
    out.append("")
    if not patterns:
        out.append("    (none)")
    for i, p in enumerate(patterns, 1):
        out.append(f"  {i:>2}. {p.get('statement')}")
        meta = [f"{p.get('kind')}", f"confidence {float(p.get('confidence') or 0):.2f}"]
        if p.get("time_window"):
            meta.append(str(p["time_window"]))
        if p.get("days"):
            meta.append(_days_text(p["days"]))
        out.append(f"      {' | '.join(meta)}")
        evidence = [e for e in (p.get("evidence") or []) if e]
        if evidence:
            refs = []
            for e in evidence[:4]:
                ref = e.get("frame_id") or e.get("note_id") or (e.get("id") if e.get("label") == "Visit" else None) or e.get("day_id") or "?"
                refs.append(f"{e.get('label', '?')}:{ref}")
            more = f" +{len(evidence) - 4} more" if len(evidence) > 4 else ""
            out.append(f"      evidence ({len(evidence)}): {', '.join(refs)}{more}")
        out.append(f"      id: {p.get('pattern_id')}")
        out.append("")

    if data.get("remember"):
        out.append(rule("  C. PLAIN ANSWER — things Vixy now knows"))
        out.append("")
        for line in data["remember"]:
            out.append(f"    * {line}")
        out.append("")

    # -- PART 3 ----------------------------------------------------------
    out.append(box("WHERE THIS LIVES"))
    out.append("")
    out.append(f"  live graph     : FalkorDB graph '{data['graph']}'")
    out.append("  raw input      : dream_output/dreamer_export.jsonl")
    out.append("  full structure : dream_output/dream_output.json")
    out.append("  this file      : dream_output/dream_readable.txt")
    out.append("")
    out.append("  To check any conclusion by hand, query the graph:")
    out.append("    MATCH (m:LongTermMemory)-[:SUPPORTED_BY]->(e)")
    out.append("    RETURN m.statement, labels(e)[0], e.frame_id, e.local_date")
    out.append("")
    return "\n".join(out)


def write_readable(data: dict[str, Any], graph: ProdGraph, out_dir: str | Path) -> Path:
    path = Path(out_dir) / "dream_readable.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_readable(data, graph), encoding="utf-8")
    return path
