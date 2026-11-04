"""Command line for the Active Agent.

  uv run python -m agent.cli eval                      # synthetic scenarios: pass/fail + decisions
  uv run python -m agent.cli sim                       # one simulated day at 1 frame/s + metrics
  uv run python -m agent.cli run events.jsonl          # feed vision/audio JSON (array or JSONL)
  uv run python -m agent.cli add "Dentist" --start 2026-10-27T18:00 --end 2026-10-27T19:00
  uv run python -m agent.cli questions | answer <question_id> yes
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime

from memory.db import graph_for
from memory.frames import load_json_records
from memory.store import TZ, MemoryStore

from .engine import ActiveAgent
from .llm import from_env
from .interpret import HybridInterpreter
from .memory_tools import AgentMemory


def _ms(s: str) -> int:
    dt = datetime.fromisoformat(s)
    return int((dt if dt.tzinfo else dt.replace(tzinfo=TZ)).timestamp() * 1000)


def _show(d) -> str:
    t = datetime.fromtimestamp(d.ts_ms / 1000, TZ).strftime("%H:%M:%S") if d.ts_ms else "--:--:--"
    return f"  {t}  {d.action.value:<13} {d.status:<10} {d.priority.value:<6} {d.message or d.reason}"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="agent")
    ap.add_argument("--user", default="demo")
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("eval"); p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("sim"); p.add_argument("--quiet", action="store_true")
    p = sub.add_parser("run"); p.add_argument("events"); p.add_argument("--all", action="store_true",
                                                                         help="also print NO_ACTION")
    p = sub.add_parser("add"); p.add_argument("title"); p.add_argument("--start", required=True); p.add_argument("--end")
    sub.add_parser("questions")
    sub.add_parser("commitments")
    p = sub.add_parser("answer"); p.add_argument("question_id"); p.add_argument("answer")
    a = ap.parse_args(argv)

    if a.cmd == "eval":
        from .scenarios import SCENARIOS, run_scenario
        path, failed = f"{tempfile.mkdtemp()}/eval.rdb", 0
        for name in SCENARIOS:
            fails, r = run_scenario(name, path)
            failed += bool(fails)
            m = r.agent.metrics.c
            print(f"{'PASS' if not fails else 'FAIL'}  {name:<30} obs={m['vision'] + m['audio']:<5} "
                  f"db={r.mem.queries:<4} llm={r.agent.interp.llm_calls} sent={len(r.notifier.sent)}")
            for msg in fails:
                print(f"      x {msg}")
            if a.verbose:
                for d in r.decisions:
                    if d.action.value != "NO_ACTION" or d.trigger in ("audio", "answer"):
                        print("    " + _show(d))
        print(f"\n{len(SCENARIOS) - failed}/{len(SCENARIOS)} scenarios passed")
        raise SystemExit(1 if failed else 0)

    if a.cmd == "sim":
        from .sim import simulate
        out = simulate(verbose=not a.quiet)
        print(json.dumps(out, indent=2, default=str))
        return

    mem = AgentMemory(MemoryStore(graph_for(a.user, a.db)))
    mem.setup()
    if a.cmd == "add":
        cid = mem.record_commitment(title=a.title, start_ms=_ms(a.start), end_ms=_ms(a.end) if a.end else None,
                                    status="confirmed", source="app")
        print(cid)
    elif a.cmd == "commitments":
        for c in mem.commitments_between(0, 2**62):
            print(f"  {c['id']}  {datetime.fromtimestamp(c['start_ms'] / 1000, TZ):%a %m-%d %H:%M}  "
                  f"{c['status']:<10} {c['title']}")
    elif a.cmd == "questions":
        for q in mem.q("MATCH (q:Question) WHERE q.status IN ['pending', 'asked'] RETURN q.id, q.status, q.text"):
            print(f"  {q[0]}  {q[1]:<8} {q[2]}")
    elif a.cmd == "answer":
        for d in ActiveAgent(mem).answer(a.question_id, a.answer):
            print(_show(d))
    elif a.cmd == "run":
        agent = ActiveAgent(mem, interpreter=HybridInterpreter(from_env()))
        records = sorted(load_json_records(a.events), key=lambda r: str(r.get("timestamp") or r.get("timestamp_start")))
        for rec in records:
            for d in agent.handle(rec):
                if a.all or d.action.value != "NO_ACTION" or d.trigger == "audio":
                    print(_show(d))
        print(json.dumps(agent.summary(), indent=2, default=str))


if __name__ == "__main__":
    main()
