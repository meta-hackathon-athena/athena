"""Deterministic multi-day wearable clock over the existing integrated runtime."""
from __future__ import annotations

import heapq
import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import VixyConfig, connect_graph
from .perception import load_cache
from .scenario import digest, load_scenario, ms


def replay_config(scenario, output: Path, *, state_dir: str | None = None) -> VixyConfig:
    return VixyConfig.from_env(user_id=scenario.user_id,
        session_ids=tuple(dict.fromkeys(r.session_id for r in scenario.recordings))).under(state_dir or output / "state")


def reset_disposable(cfg: VixyConfig, scenario) -> None:
    """Only the manifest's named disposable user, never vixy_demo or shared files."""
    if not scenario.disposable or not cfg.user_id.startswith(("test_", "m3_")) or cfg.graph_prefix != "vixy":
        raise ValueError("reset requires disposable:true and a user named test_* or m3_* (vixy prefix)")
    state = Path(cfg.runtime_db).parent
    marker = state / "replay-owner.json"
    owner = {"user_id": cfg.user_id, "graph": cfg.graph_name}
    files = [Path(cfg.events_db), Path(cfg.runtime_db), Path(cfg.db_path)]
    if marker.exists():
        if json.loads(marker.read_text()) != owner:
            raise ValueError("state directory belongs to another replay user")
    elif any(p.exists() for p in files):
        raise ValueError("refusing reset of existing state without matching replay ownership marker")
    graph = connect_graph(cfg)
    # Existing server graphs need explicit ownership too, even with an empty local directory.
    rows = graph.query("MATCH (m:VixyMeta {key:'graph'}) RETURN m.user_id").result_set
    if rows and (not marker.exists() or rows[0][0] != cfg.user_id):
        raise ValueError("refusing reset of existing graph without matching replay ownership")
    count = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
    if count and not rows:
        raise ValueError("refusing reset of graph without Vixy ownership metadata")
    if count:
        graph.delete()
    for path in files[:2]:
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
    # The embedded RDB may hold other graphs; never unlink it.
    state.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(owner))


class Monitor:
    def __init__(self, output: Path, emit=print):
        output.mkdir(parents=True, exist_ok=True)
        self.path = output / "replay.jsonl"
        self.file = self.path.open("a", encoding="utf-8")
        self.emit = emit

    def write(self, entry_type: str, at: int, **data):
        row = {"schema": "vixy.replay.v1", "type": entry_type,
               "simulated_time": datetime.fromtimestamp(at / 1000, timezone.utc).isoformat(), **data}
        self.file.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
        self.file.flush()
        if entry_type == "event":
            detail = data.get("text") or ", ".join(f"{d['label']} x{data['counts'].get(d['label'], '?')}" for d in data["objects"])
            self.emit(f"{row['simulated_time']} {data['recording']} {data['progress']:.0%} {data['kind']} "
                      f"{detail or 'no supported objects'} -> CE/{data['outcome']['status']} "
                      f"note={data['outcome']['note_id']} episodes={data['outcome']['visit_ids']}")
        else:
            self.emit(f"{row['simulated_time']} {entry_type}: {json.dumps(data, default=str, ensure_ascii=False)}")
        for d in data.get("decisions", []):
            self.emit(f"  {d['action']} {d['status']}: {d.get('message') or d['reason']} "
                      f"memories={[m.get('id') for m in d.get('supporting_memories', [])]}")

    def close(self):
        self.file.close()


def replay_scenario(manifest: Path, output: Path, *, fresh: bool = False, state_dir: str | None = None,
                    speed: float | None = None, pause_file: Path | None = None, question: str | None = None,
                    emit=print) -> dict:
    from .runtime import VixyRuntime
    from context_engine.timeutil import SimulatedClock

    scenario = load_scenario(manifest)
    speed = scenario.playback_speed if speed is None else speed
    if speed < 0 or not __import__("math").isfinite(speed):
        raise ValueError("replay speed must be finite and nonnegative")
    # Validate every cache before opening or resetting a graph.
    caches = [(rec, load_cache(scenario, rec, manifest, output)) for rec in scenario.recordings]
    actions = []
    for rec, cache in caches:
        for row in cache["events"]:
            actions.append((ms(datetime.fromisoformat(row["available_at"])), 1, rec.id, "event", rec, row))
        end = ms(rec.simulated_start + timedelta(seconds=cache["media"]["duration_s"]))
        actions.append((end, 2, rec.id, "boundary", rec, cache))
    for i, boundary in enumerate(scenario.dream_boundaries):
        actions.append((ms(boundary), 3, str(i), "dream", None, None))
    actions.sort(key=lambda row: row[:3])
    start = min(ms(r.simulated_start) for r in scenario.recordings)
    end = max(a[0] for a in actions)
    if scenario.end_time:
        if ms(scenario.end_time) < end:
            raise ValueError("end_time precedes the end of a recording/boundary")
        end = ms(scenario.end_time)
    if any(a[0] < start for a in actions):
        raise ValueError("dream boundaries cannot precede the first recording")
    cfg = replay_config(scenario, output, state_dir=state_dir)
    if fresh:
        reset_disposable(cfg, scenario)
    owner_path = Path(cfg.runtime_db).parent / "replay-owner.json"
    owner = {"user_id": cfg.user_id, "graph": cfg.graph_name}
    if owner_path.exists() and json.loads(owner_path.read_text()) != owner:
        raise ValueError("state directory belongs to another replay user")
    owner_path.parent.mkdir(parents=True, exist_ok=True)
    owner_path.write_text(json.dumps(owner))
    replay_key = digest({"caches": [c["key"] for _, c in caches], "start": start, "end": end,
                         "boundaries": [b.isoformat() for b in scenario.dream_boundaries], "tick": scenario.tick_seconds})[:24]
    ticks = ((at, 0, "", "tick", None, None) for at in range(start, end + 1, scenario.tick_seconds * 1000))
    monitor = Monitor(output, emit)
    counts = Counter()
    previous_media = {}
    t0 = time.perf_counter()
    def wait(seconds=0):
        until = time.monotonic() + seconds
        while True:
            if pause_file and pause_file.exists():
                paused = time.monotonic()
                while pause_file.exists():
                    time.sleep(0.1)
                until += time.monotonic() - paused
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))
    try:
        with VixyRuntime(cfg) as rt:
            clock = SimulatedClock(datetime.fromtimestamp(start / 1000, timezone.utc))
            rt.engine.clock = clock
            for rec, cache in caches:
                for warning in cache["warnings"]:
                    monitor.write("warning", ms(rec.simulated_start), recording=rec.id, warning=warning)
            for at, _, ident, kind, rec, row in heapq.merge(ticks, actions, key=lambda a: a[:3]):
                wait()
                clock.set(datetime.fromtimestamp(at / 1000, timezone.utc))
                action_id = f"{replay_key}:{kind}:{ident}:{at}"
                if kind == "tick":
                    decisions = rt.advance_clock(at, action_id)
                    if decisions:
                        monitor.write("clock", at, decisions=decisions)
                    continue
                if kind == "event":
                    event = row["event"]
                    offset = event["source"]["capture_end_s"]
                    duplicate = rt.ledger.status(event["event_id"]) == "done"
                    if speed and not duplicate:
                        wait(max(0, offset - previous_media.get(rec.id, 0)) / speed)
                    previous_media[rec.id] = offset
                    outcome = (rt.ingest_video if row["kind"] == "video" else rt.ingest_audio)(event)
                    counts[outcome.status] += 1
                    monitor.write("event", at, recording=rec.id, kind=row["kind"], source_event_id=event["event_id"],
                        capture_time=event["source"]["capture_time"], capture_offset_s=event["source"]["capture_offset_s"],
                        progress=min(1, offset / next(c["media"]["duration_s"] for r, c in caches if r.id == rec.id)),
                        objects=event.get("detections", []), counts=event["source"].get("counts", {}),
                        text=event.get("text"), outcome=outcome.to_dict(), decisions=outcome.decisions,
                        recall=rt.agent.summary().get("recall"))
                    if not outcome.ok:
                        raise RuntimeError(f"event {outcome.event_id} failed at {outcome.stage}: {outcome.error}")
                else:
                    checkpoint = f"replay:{action_id}"
                    if rt.ledger.get_kv(checkpoint):
                        continue
                    rt.advance_clock(at, action_id + ":clock")
                    if kind == "boundary":
                        visits = rt.flush_episodes(rec.session_id)
                        monitor.write("episodes", at, recording=rec.id, visit_ids=visits)
                    if kind == "dream" or rec.dream_after:
                        dream = rt.dream(now_ms=at)
                        monitor.write("dream", at, result=dream,
                                      memories=rt.memory.long_term_memories(limit=100))
                    rt.ledger.set_kv(checkpoint, True)
            clock.set(datetime.fromtimestamp(end / 1000, timezone.utc))
            rt.advance_clock(end, f"{replay_key}:end")
            answer = rt.ask(question, now_ms=end) if question else None
            if answer:
                monitor.write("ask", end, result=answer)
            result = {"schema": "vixy.replay_summary.v1", "graph": cfg.graph_name, "user_id": cfg.user_id,
                      "outcomes": dict(counts), "status": rt.status(), "agent": rt.agent.summary(),
                      "memories": rt.memory.long_term_memories(limit=100), "answer": answer,
                      "simulated_start_ms": start, "simulated_end_ms": end,
                      "wall_seconds": round(time.perf_counter() - t0, 3), "paid_api_calls": 0}
            (output / "summary.json").write_text(json.dumps(result, indent=2, default=str))
            monitor.write("summary", end, graph=cfg.graph_name, outcomes=dict(counts),
                          graph_labels=result["status"]["graph_labels"], wall_seconds=result["wall_seconds"])
            return result
    except Exception as exc:
        monitor.write("error", start, error_type=type(exc).__name__)
        raise
    finally:
        monitor.close()
