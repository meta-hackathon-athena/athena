"""Vixy integrated backend CLI.

  uv run python -m vixy demo [--fresh]           # the whole Milestone 1 pipeline, with checks
  uv run python -m vixy demo-m2 [--fresh]        # Milestone 2: learn on day 1, apply it on day 2, ask about it
  uv run python -m vixy ask "What do you remember about Alex?" [--conversation c1] [--now ISO]
  uv run python -m vixy serve [--port 8765]      # versioned JSON API (/v1/ask, /v1/memories, ...)
  uv run python -m vixy decisions [--limit 20] | decision <id> | timeline 2026-11-02
  uv run python -m vixy migrate                  # explicit vixy.graph.v1 -> v2 upgrade (additive)
  uv run python -m vixy ingest events.jsonl      # CE v1 events or perception frames / ASR segments
  uv run python -m vixy flush | recover | status
  uv run python -m vixy dream                    # offline consolidation into the same graph
  uv run python -m vixy memories [--about Marta] | explain <memory_id> | day 2026-11-02

Storage comes from the environment (.env.example): FALKORDB_URL for the shared server
(docker compose up -d falkordb), else the embedded file. --state-dir keeps the SQLite
files (and the embedded graph) in one directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .config import VixyConfig, connect_graph
from .contracts import GRAPH_SCHEMA_VERSION


def _cfg(a) -> VixyConfig:
    cfg = VixyConfig.from_env(user_id=a.user)
    return cfg.under(a.state_dir) if a.state_dir else cfg


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _clock(ms: int | None, tz) -> str:
    return datetime.fromtimestamp(ms / 1000, tz).strftime("%a %H:%M:%S") if ms else "--"


def _records(path: str):
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            yield n, json.loads(line)


def cmd_ingest(rt, a) -> int:
    bad = 0
    for n, r in _records(a.events):
        kind = r.pop("kind", None)
        if kind in ("audio", "video"):
            out = (rt.ingest_audio if kind == "audio" else rt.ingest_video)({"session_id": rt.session_id, **r})
        elif "classes" in r:
            out = rt.ingest_perception_frame(r)
        else:
            out = rt.ingest_transcription(r)
        bad += not out.ok
        if not out.ok or any(d["action"] != "NO_ACTION" for d in out.decisions):
            print(f"line {n}: {out.status} {out.event_id} {out.stage or ''} {out.error or ''}".rstrip())
            for d in out.decisions:
                if d["action"] != "NO_ACTION":
                    print(f"    {d['action']:<13} {d['status']:<10} {d['message'] or d['reason']}")
    _print(rt.status())
    return 1 if bad else 0


def cmd_demo(a) -> int:
    from memory.store import TZ

    from . import demo_data
    from .dream_job import run_dream
    from .runtime import VixyRuntime

    cfg = _cfg(a) if a.state_dir else VixyConfig.from_env(user_id=a.user).under("data/demo")
    if a.fresh:
        for p in (cfg.events_db, cfg.runtime_db, cfg.db_path):
            for f in Path(p).parent.glob(Path(p).name + "*"):
                f.unlink()
        try:
            connect_graph(cfg).delete()                 # only this demo user's graph
        except Exception:
            pass                                        # graph did not exist yet
    print(f"Vixy M1 demo — user {cfg.user_id}, graph {cfg.graph_name} ({cfg.storage}), schema {GRAPH_SCHEMA_VERSION}")
    print(f"  context-engine store: {cfg.events_db}\n  runtime ledger:       {cfg.runtime_db}\n")
    checks: list[tuple[str, bool]] = []

    with VixyRuntime(cfg) as rt:
        for c in demo_data.calendar_ms():           # an appointment the user entered in the app
            rt.agent_memory.record_commitment(title=c["title"], start_ms=c["start_ms"], end_ms=c["end_ms"],
                                              status="confirmed", source="app", now_ms=c["start_ms"] - 86_400_000)
        timeline = demo_data.timeline()
        print(f"1. ingesting {len(timeline)} perception records through Context Engine -> Episode Writer -> Agent")
        outcomes = []
        for kind, raw in timeline:
            out = rt.ingest_perception_frame(raw) if kind == "video" else rt.ingest_transcription(raw)
            outcomes.append(out)
            for d in out.decisions:
                if d["action"] != "NO_ACTION" or kind == "audio":
                    print(f"   {_clock(d['ts_ms'], TZ)}  {d['action']:<13} {d['status']:<10} {d['priority']:<6} "
                          f"{d['message'] or d['reason']}")
        rt.flush_episodes()
        statuses = {s: sum(o.status == s for o in outcomes) for s in {o.status for o in outcomes}}
        print(f"   outcomes: {statuses}")
        checks.append(("every record accepted by Context Engine and fully processed",
                       statuses.get("processed", 0) + statuses.get("duplicate", 0) == len(outcomes)))
        labels = rt.memory.stats()
        print(f"   graph: {labels}")
        checks.append(("Notes and Visits written", labels.get("Note", 0) >= 9 and labels.get("Visit", 0) >= 8))
        delivered = [d for o in outcomes for d in o.decisions if d["action"] in ("NOTIFY", "ASK_USER") and d["status"] == "done"]
        checks.append(("conflict notification (HIGH)", any(d["priority"] == "high" and "Dentist" in (d["message"] or "") for d in delivered)))
        checks.append(("location reminder at the supermarket", any("Reminder: buy milk" in (d["message"] or "") for d in delivered)))

        print("\n2. replaying every record (idempotency)")
        before = rt.memory.stats()
        again = [rt.ingest_perception_frame(raw) if k == "video" else rt.ingest_transcription(raw) for k, raw in timeline]
        dup = sum(o.status == "duplicate" for o in again)
        print(f"   {dup}/{len(again)} duplicates; graph unchanged: {rt.memory.stats() == before}")
        checks.append(("replay creates nothing", dup == len(again) and rt.memory.stats() == before))

        print("\n3. Dreamer (offline, same graph, same context-engine history)")
        operational = {k: before.get(k) for k in ("Commitment", "Question", "AgentLog", "Note", "Visit")}
        dream = run_dream(cfg, rt.graph, rt.engine)
        print(f"   history: {dream['history']}; facts: {dream['facts']}; fallback wrote "
              f"{dream['fallback']['patterns']} patterns / {dream['fallback']['memories']} memories; "
              f"evidence: {dream['evidence']}")
        after = rt.memory.stats()
        checks.append(("Dreamer left operational/observed records alone",
                       {k: after.get(k) for k in operational} == operational))
        run_dream(cfg, rt.graph, rt.engine)
        checks.append(("re-running the Dreamer is idempotent", rt.memory.stats() == after))

        print("\n4. retrieval through the shared memory interface")
        for m in rt.memory.long_term_memories(limit=50):
            print(f"   [{m['kind']:<10}] {m['statement']}  (evidence: {m['evidence_count']})")
        marta = rt.memory.long_term_memories(about="Marta")
        if marta:
            ex = rt.memory.explain(marta[0]["id"])
            ev = ex["evidence"][0]
            original = rt.engine.get_event_record(ev["source_event_id"])
            print(f"\n   why '{ex['statement']}'?\n     <- {ev['label']} {ev['id']} said by {ev['speaker_id']} "
                  f"({ev['speaker_role']}) at {ev['timestamp']}\n     <- context-engine event {ev['source_event_id']} "
                  f"(sha256 {original.payload_sha256[:12]}…, matches: {original.payload_sha256 == ev['payload_sha256']})")
            checks.append(("memory about Marta is backed by the original utterance",
                           original.payload_sha256 == ev["payload_sha256"] and original.event["text"] == ev["text"]))
        else:
            checks.append(("memory about Marta is backed by the original utterance", False))
        via_agent = rt.agent_memory.long_term_memories(about="Marta")
        checks.append(("the agent reads the same memory", bool(via_agent) and via_agent[0]["id"] == marta[0]["id"]))
        status = rt.status()

    print("\n5. status")
    _print(status)
    print("\nchecks:")
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


def _ask_print(res: dict) -> None:
    print(f"[{res['status']}] {res['answer']}")
    for src in res["sources"]:
        ok = " ✓ original frame event found" if src["label"] == "Visit" else " ✓ text + sha256 match the original event"
        tick = {True: ok, False: " ✗ original event missing or different"}.get(src.get("verified"), "")
        print(f"    source {src['label']} {src['id']} {src.get('time', '')} "
              f"{src.get('source_event_id') or src.get('first_event_id') or ''}{tick}")


def cmd_demo_m2(a) -> int:
    """Milestone 2 end to end on the real runtime: day 1 -> Dreamer -> day 2 -> decisions -> /ask -> restart."""
    from memory.store import TZ

    from . import demo_m2
    from .api import VixyAPI
    from .runtime import VixyRuntime

    user = a.user or "demo_m2"                          # never the M1 'demo' user / vixy_demo graph
    cfg = VixyConfig.from_env(user_id=user)
    cfg = cfg.under(a.state_dir or f"data/{user}")
    if a.fresh:
        for p in (cfg.events_db, cfg.runtime_db, cfg.db_path):
            for f in Path(p).parent.glob(Path(p).name + "*"):
                f.unlink()
        try:
            connect_graph(cfg).delete()                 # only this demo user's graph
        except Exception:
            pass
    print(f"Vixy M2 demo — user {cfg.user_id}, graph {cfg.graph_name} ({cfg.storage}), schema {GRAPH_SCHEMA_VERSION}\n")
    checks: list[tuple[str, bool]] = []

    def feed(rt, day: int) -> list:
        outs = []
        for kind, raw in demo_m2.day_timeline(day):
            out = rt.ingest_perception_frame(raw) if kind == "video" else rt.ingest_transcription(raw)
            outs.append(out)
            for d in out.decisions:
                if d["action"] != "NO_ACTION" or any(w in d["reason"] for w in ("recalled", "stayed silent")):
                    print(f"   {_clock(d['ts_ms'], TZ)}  {d['action']:<13} {d['status']:<10} {d['message'] or d['reason']}")
        rt.flush_episodes()
        return outs

    with VixyRuntime(cfg) as rt:
        print("1. Day 1 (Mon 2 Nov): ingest through Context Engine -> Episode Writer -> Agent")
        day1 = feed(rt, 0)
        checks.append(("day 1 fully processed", all(o.ok for o in day1)))
        print("\n2. Dreamer (offline rules; same graph, same context-engine history)")
        d1 = rt.dream()
        sem = d1["fallback"]["semantic"]
        print(f"   claims {sem['claims']}, memories {d1['fallback']['memories']}, persons {sem['persons']}, "
              f"unverified notes {sem['notes_unverified']}, model calls {sem['model_calls']}")
        for m in rt.memory.long_term_memories(limit=50):
            print(f"   [{m.get('epistemic_status', '?'):<13}] {m['statement']}  (conf {m['confidence']}, evidence {m['evidence_count']})")
        alex = rt.memory.long_term_memories(about="Alex")
        checks.append(("Dreamer stored the reported allergy as an attributed, evidence-backed claim",
                       any(m.get("predicate") == "allergic_to" and m.get("epistemic_status") == "reported"
                           and m["evidence_count"] >= 1 for m in alex)))
        snap = rt.memory.stats()
        rt.dream()
        checks.append(("re-running the Dreamer is idempotent", rt.memory.stats() == snap))

    print("\n3. restart (new runtime process state); the simulated clock moves to Day 2 (Tue 3 Nov)")
    with VixyRuntime(cfg) as rt:
        checks.append(("memories survive the restart", bool(rt.memory.long_term_memories(about="Alex"))))
        day2 = feed(rt, 1)
        decided = [d for o in day2 for d in o.decisions]
        recall = [d for d in decided if d["deduplication_key"] and d["deduplication_key"].startswith("recall:")]
        sent = [d for d in recall if d["status"] == "done"]
        checks.append(("agent recalled Monday's allergy when peanuts were bought for Alex (attributed, not certain)",
                       any("Alex" in (d["message"] or "") and "allergic to peanuts" in (d["message"] or "")
                           and "haven't verified" in (d["message"] or "") for d in sent)))
        checks.append(("the same recall was not repeated", sum("allergic" in (d["message"] or "") for d in sent) == 1
                       and any(d["status"] == "suppressed" for d in recall)))
        silent = {o.event_id: o.decisions for o in day2 if o.kind == "audio"}
        chocolate = next(v for k, v in silent.items() if k.endswith("m2seg008"))
        jordan = next(v for k, v in silent.items() if k.endswith("m2seg009"))
        checks.append(("no action for chocolate (no relevant memory) or for Jordan (nothing known)",
                       all(d["action"] == "NO_ACTION" for d in chocolate + jordan)))
        checks.append(("plan with Marta checked against Marta's remembered presentation",
                       any("Marta" in (d["message"] or "") and "presentation" in (d["message"] or "") for d in sent)))
        checks.append(("milestone 1 behaviour intact (supermarket reminder)",
                       any("Reminder: buy milk" in (d["message"] or "") for d in decided if d["status"] == "done")))
        print("\n4. Dreamer again (incremental: Day 2 is new)")
        d2 = rt.dream()
        print(f"   new notes {d2['fallback']['new_notes']}, superseded {d2['fallback']['semantic']['superseded']}, "
              f"conflicts {d2['fallback']['semantic']['conflicts']}")
        checks.append(("a stated change superseded the old fact (history kept)",
                       d2["fallback"]["semantic"]["superseded"] >= 1))

        print("\n5. /ask through the versioned API (same runtime)")
        api = VixyAPI(rt)
        now = datetime.fromtimestamp(demo_m2.evening_ms(1) / 1000, TZ).isoformat()
        answers = {}
        for q in ["Where did I go yesterday?", "What did Marta tell me about the presentation?",
                  "What do you remember about Alex?", "Why did you remind me about this?",
                  "What evidence supports that memory?", "When am I meeting Marta?",
                  "What do you remember about Lena?", "What did I eat on Saturday?"]:
            code, body = api.handle("POST", "/v1/ask", body={"question": q, "conversation_id": "demo", "now": now})
            res = body["result"]
            answers[q] = res
            print(f"\n   Q: {q}")
            print("   A: ", end="")
            _ask_print(res)
        checks.append(("/ask: day-1 places answered from Visits", answers["Where did I go yesterday?"]["status"] == "answered"
                       and any(s["label"] == "Visit" for s in answers["Where did I go yesterday?"]["sources"])))
        checks.append(("/ask: says it can't confirm who spoke, cites the original utterance",
                       "can't confirm" in answers["What did Marta tell me about the presentation?"]["answer"]
                       and any(s.get("verified") for s in answers["What did Marta tell me about the presentation?"]["sources"])))
        why = answers["Why did you remind me about this?"]
        checks.append(("/ask: explains the reminder with its supporting memory and evidence",
                       why["status"] == "answered" and "allergic" in why["answer"] and len(why["items"]) >= 2))
        ev = answers["What evidence supports that memory?"]
        checks.append(("/ask: evidence follow-up re-checks the original event",
                       ev["status"] == "answered" and any(s.get("verified") for s in ev["sources"])))
        checks.append(("/ask: refuses to invent a memory", answers["What did I eat on Saturday?"]["status"] ==
                       "insufficient_evidence"))

        print("\n6. inspect the memory and the original event")
        mid = next(m["id"] for m in rt.memory.long_term_memories(about="Alex") if m.get("predicate") == "allergic_to")
        _, detail = api.handle("GET", f"/v1/memories/{mid}")
        e = detail["memory"]["evidence"][0]
        print(f"   {mid}: “{detail['memory']['statement']}” [{detail['memory']['epistemic_status']}]")
        print(f"     <- {e['label']} {e['id']} ({e['speaker_role']}, {e['speaker_id']}) at {e['timestamp']}")
        print(f"     <- context-engine event {e['original']['event_id']} sha256 {e['original']['payload_sha256'][:12]}… "
              f"matches: {e['original']['matches']}")
        checks.append(("memory -> Note -> original context-engine event (same sha256)", e["original"]["matches"] is True))
        decisions = api.handle("GET", "/v1/decisions", {"delivered": "1"})[1]["decisions"]
        dec = next(d for d in decisions if d.get("supporting_memory_ids"))
        _, ddetail = api.handle("GET", f"/v1/decisions/{dec['id']}")
        checks.append(("decision inspection shows its supporting memory and evidence",
                       bool(ddetail["decision"]["memories"]) and bool(ddetail["decision"]["memories"][0]["evidence"])))
        before = rt.memory.stats()
        again = [rt.ingest_perception_frame(raw) if k == "video" else rt.ingest_transcription(raw)
                 for k, raw in demo_m2.day_timeline(1)]
        checks.append(("replaying day 2 changes nothing", all(o.status == "duplicate" for o in again)
                       and rt.memory.stats() == before))
        print(f"\n   agent: {json.dumps(rt.agent.summary().get('recall'))}; llm calls {rt.agent.summary()['llm_calls']}")

    print("\nchecks:")
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vixy", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default=None, help="VIXY_USER_ID override")
    ap.add_argument("--state-dir", default=None, help="keep SQLite (+ embedded graph) files here")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("demo"); p.add_argument("--fresh", action="store_true", help="wipe this demo's state first")
    p = sub.add_parser("ingest"); p.add_argument("events")
    sub.add_parser("flush"); sub.add_parser("recover"); sub.add_parser("status")
    p = sub.add_parser("dream"); p.add_argument("--online", action="store_true",
                                                help="use the Meta Model API (not authorized in Milestone 1)")
    p = sub.add_parser("memories"); p.add_argument("--about")
    p = sub.add_parser("explain"); p.add_argument("memory_id")
    p = sub.add_parser("day"); p.add_argument("date")
    p = sub.add_parser("demo-m2"); p.add_argument("--fresh", action="store_true", help="wipe this demo's state first")
    p = sub.add_parser("ask"); p.add_argument("question"); p.add_argument("--conversation"); p.add_argument("--now")
    p = sub.add_parser("serve"); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8765)
    p = sub.add_parser("decisions"); p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("decision"); p.add_argument("decision_id")
    p = sub.add_parser("timeline"); p.add_argument("date")
    sub.add_parser("migrate")
    for cmd in ("process", "replay", "reset-replay"):
        p = sub.add_parser(cmd)
        p.add_argument("--scenario", type=Path, required=True)
        p.add_argument("--output", type=Path, help="local cache and monitor directory (default demo/outputs/<scenario user>)")
        if cmd == "process":
            p.add_argument("--force", action="store_true")
        if cmd == "replay":
            p.add_argument("--fresh", action="store_true")
            p.add_argument("--speed", type=float, help="0=virtual time; positive=media speed; day gaps are skipped")
            p.add_argument("--pause-file", type=Path, help="pause while this local sentinel file exists")
            p.add_argument("--question", help="ask the real backend at replay end, with evidence")
    p = sub.add_parser("fixture")
    p.add_argument("--directory", type=Path, default=Path("demo/inputs/synthetic"))
    p.add_argument("--fixture-user", default="m3_synthetic")
    a = ap.parse_args(argv)

    if a.cmd == "fixture":
        from .fixture import generate
        try:
            print(generate(a.directory, a.fixture_user))
            return 0
        except (ValueError, OSError) as exc:
            print(f"fixture failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if a.cmd in ("process", "replay", "reset-replay"):
        from .scenario import load_scenario
        from .perception import process_scenario
        from .replay import replay_config, replay_scenario, reset_disposable
        try:
            scenario = load_scenario(a.scenario)
            if a.user and a.user != scenario.user_id:
                raise ValueError("--user must match the scenario user_id")
            output = a.output or Path("demo/outputs") / scenario.user_id
            if a.cmd == "process":
                _print(process_scenario(a.scenario, output, force=a.force))
            elif a.cmd == "reset-replay":
                reset_disposable(replay_config(scenario, output, state_dir=a.state_dir), scenario)
                _print({"reset_user": scenario.user_id})
            else:
                replay_scenario(a.scenario, output, fresh=a.fresh, state_dir=a.state_dir, speed=a.speed,
                                pause_file=a.pause_file, question=a.question)
            return 0
        except (ValueError, OSError, RuntimeError, ImportError) as exc:
            # Validation errors do not echo media paths, input text, or credentials.
            from pydantic import ValidationError
            detail = ("invalid scenario fields: " + ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
                      if isinstance(exc, ValidationError) else str(exc) if type(exc) is ValueError
                      else "check scenario, media, cache, dependencies and local graph")
            print(f"{a.cmd} failed: {type(exc).__name__}: {detail}", file=sys.stderr)
            return 2

    if a.cmd == "demo":
        return cmd_demo(a)
    if a.cmd == "demo-m2":
        return cmd_demo_m2(a)
    if a.cmd == "migrate":
        from .schema import migrate
        cfg = _cfg(a)
        _print({"graph": cfg.graph_name, **migrate(connect_graph(cfg), cfg.user_id)})
        return 0

    from .runtime import VixyRuntime
    with VixyRuntime(_cfg(a)) as rt:
        if a.cmd == "ingest":
            return cmd_ingest(rt, a)
        if a.cmd == "flush":
            _print({"visits_written": rt.flush_episodes()})
        elif a.cmd == "recover":
            _print({"startup": rt.startup, "recovered_now": [o.to_dict() for o in rt.recover()]})
        elif a.cmd == "status":
            _print({**rt.status(), "startup": rt.startup})
        elif a.cmd == "dream":
            _print(rt.dream(online=a.online, log=print))
        elif a.cmd == "memories":
            _print(rt.memory.long_term_memories(about=a.about, limit=100))
        elif a.cmd == "explain":
            out = rt.memory.explain(a.memory_id)
            if not out:
                print(f"no memory {a.memory_id}", file=sys.stderr)
                return 2
            _print(out)
        elif a.cmd == "day":
            _print(rt.memory.day(a.date))
        elif a.cmd == "ask":
            from .api import _ms
            res = rt.ask(a.question, conversation_id=a.conversation, now_ms=_ms(a.now, "now") if a.now else None)
            _ask_print(res)
            print(f"    (conversation {res['conversation_id']}; answered by {res['answered_by']})")
        elif a.cmd == "decisions":
            _print(rt.memory.decisions(limit=a.limit))
        elif a.cmd == "decision":
            _print(rt.memory.decision(a.decision_id))
        elif a.cmd == "timeline":
            from .api import VixyAPI
            _print(VixyAPI(rt).handle("GET", "/v1/timeline", {"date": a.date})[1])
        elif a.cmd == "serve":
            from .api import serve
            server = serve(rt, a.host, a.port)
            print(f"Vixy API on http://{a.host}:{a.port}/v1 (graph {rt.config.graph_name}); Ctrl-C to stop")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
