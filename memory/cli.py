"""Command line for the memory graph.

  uv run python -m memory.cli demo                       # simulate a week, ingest, consolidate, ask
  uv run python -m memory.cli ingest frames.jsonl [--gps gps.jsonl]
  uv run python -m memory.cli note "remind me to buy milk at the supermarket"
  uv run python -m memory.cli consolidate
  uv run python -m memory.cli day 2026-10-26
  uv run python -m memory.cli facts | explain <fact_id> | last-seen laptop
  uv run python -m memory.cli context --lat 1.3467 --lon 103.68
  uv run python -m memory.cli viz                        # interactive graph in the browser
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from .db import graph_for
from .frames import attach_gps, load_json_records, parse_frame
from .segmenter import segment
from .store import TZ, MemoryStore


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%a %m-%d %H:%M")


def ingest_frames(store: MemoryStore, frames, user: str) -> list:
    visits = segment(frames, user)
    for v in visits:
        store.add_visit(v)
    return visits


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="memory")
    ap.add_argument("--user", default="demo")
    ap.add_argument("--db", default=None, help="embedded DB file (ignored if FALKORDB_HOST set)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest"); p.add_argument("frames"); p.add_argument("--gps")
    p = sub.add_parser("note"); p.add_argument("text"); p.add_argument("--at")
    sub.add_parser("consolidate")
    p = sub.add_parser("day"); p.add_argument("date")
    p = sub.add_parser("facts"); p.add_argument("--all", action="store_true")
    p = sub.add_parser("explain"); p.add_argument("fact_id")
    p = sub.add_parser("last-seen"); p.add_argument("tag")
    p = sub.add_parser("name-place"); p.add_argument("place_id"); p.add_argument("name")
    p = sub.add_parser("forget-visit"); p.add_argument("visit_id")
    p = sub.add_parser("context"); p.add_argument("--lat", type=float); p.add_argument("--lon", type=float)
    p = sub.add_parser("viz"); p.add_argument("--out"); p.add_argument("--no-open", action="store_true")
    sub.add_parser("reset")
    sub.add_parser("demo")
    a = ap.parse_args(argv)

    g = graph_for(a.user, a.db)
    store = MemoryStore(g)
    if a.cmd == "reset":
        try:
            g.delete()
        except Exception:
            pass
        print("graph deleted"); return
    store.setup()
    show = lambda x: print(json.dumps(x, indent=2, ensure_ascii=False, default=str))

    if a.cmd == "ingest":
        frames = [parse_frame(r) for r in load_json_records(a.frames)]
        if a.gps:
            attach_gps(frames, load_json_records(a.gps))
        visits = ingest_frames(store, frames, a.user)
        print(f"{len(frames)} frames -> {len(visits)} visits")
        for v in visits:
            print(f"  {_fmt(v.start_ms)}-{_fmt(v.end_ms)[-5:]}  {v.place_type:<11} {v.activity:<10} "
                  f"{', '.join(sorted(v.tags, key=v.tags.get, reverse=True)[:5])}")
    elif a.cmd == "note":
        ts = int(datetime.fromisoformat(a.at).timestamp() * 1000) if a.at else None
        show(store.add_note(a.text, ts))
    elif a.cmd == "consolidate":
        show(store.consolidate())
    elif a.cmd == "day":
        for r in store.day(a.date):
            print(f"{_fmt(r['start_ms'])}-{_fmt(r['end_ms'])[-5:]}  {r['kind']:<7} {r['place_type']:<11} "
                  f"{r['activity']:<10} {r['place_name'] or r['place_id'] or ''}")
    elif a.cmd == "facts":
        show(store.facts(a.all))
    elif a.cmd == "explain":
        show(store.explain(a.fact_id))
    elif a.cmd == "last-seen":
        show(store.last_seen(a.tag))
    elif a.cmd == "name-place":
        store.name_place(a.place_id, a.name); print("ok")
    elif a.cmd == "forget-visit":
        store.forget_visit(a.visit_id); print("forgotten")
    elif a.cmd == "context":
        show(store.context(a.lat, a.lon))
    elif a.cmd == "viz":
        from .viz import write_html
        out = write_html(store, a.out or f"data/graph_{a.user}.html", a.user)
        print(f"wrote {out}")
        if not a.no_open:
            import webbrowser
            webbrowser.open(out.resolve().as_uri())
    elif a.cmd == "demo":
        run_demo(store, a.user)
        print("\nSee it: uv run python -m memory.cli viz")


def run_demo(store: MemoryStore, user: str) -> None:
    from .demo import PLACES, synthetic_week

    frames = synthetic_week()
    visits = ingest_frames(store, frames, user)
    print(f"1) {len(frames)} frames -> {len(visits)} visits   graph: {store.stats()}")

    print("\n2) Where did I go on Wed 2026-10-21?")
    for r in store.day("2026-10-21"):
        print(f"   {_fmt(r['start_ms'])[-5:]}-{_fmt(r['end_ms'])[-5:]}  {r['kind']:<7} "
              f"{r['place_type']:<11} {r['activity']}")

    friday_evening = int(datetime(2026, 10, 23, 18, 0, tzinfo=TZ).timestamp() * 1000)
    store.add_note("remind me to buy milk at the supermarket", friday_evening - 3 * 3600_000)
    store.add_note("I really like the noodles at Koufu", friday_evening - 6 * 3600_000)

    print("\n3) Consolidation (episodic -> semantic):", store.consolidate(friday_evening + 7 * 3600_000))
    for f in store.facts():
        label = f.get("statement") or f"{f['kind']} {f.get('place_type', '')} {f.get('activity', '')}".strip()
        print(f"   [{f['source']:<8}] {label:<40} conf={f['confidence']} days={f.get('support_days', '-')}")

    freq = next(f for f in store.facts() if f["kind"] == "frequent_place" and f.get("place_type") == "food_court")
    store.name_place(freq["place_id"], "Koufu")
    print("\n4) Why do you think I often go to Koufu?\n  ", store.explain(freq["id"])["why"])

    print("\n5) When did I last see a laptop?\n  ", store.last_seen("laptop"))

    lat, lon, _ = PLACES["supermarket"]
    ctx = store.context(lat, lon)
    print("\n6) Context at the supermarket -> reminders here:",
          [r["text"] for r in ctx["reminders_here"]])
    lat, lon, _ = PLACES["koufu"]
    ctx = store.context(lat, lon)
    print("   Context at Koufu ->", ctx["place"], "| facts:",
          [f.get("kind") for f in ctx["facts"]], "| visits:", ctx["history"]["visits"])

    clinic = [v for v in visits if v.place_type == "healthcare"]
    print(f"\n7) Sensitive: {len(clinic)} clinic visit(s) stored, facts about it:",
          [f for f in store.facts() if f.get("place_type") == "healthcare"])


if __name__ == "__main__":
    main()
