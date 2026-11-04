"""Cross-repository integration: context-engine + Episode Writer + Active Agent + Dreamer
over one FalkorDB graph, with real components (no mocks except the LLM).

Backends: "embedded" (falkordblite file, always) and "server" (the shared FalkorDB from
docker-compose.yml; set VIXY_TEST_FALKORDB_URL=redis://localhost:6390, else skipped).

Letters refer to the Milestone 1 verification list (A ingestion ... J regression).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from context_engine import ContextEngine
from vixy import adapters, demo_data
from vixy.config import VixyConfig, connect_graph
from vixy.dream_job import dreamer_config, run_dream
from vixy.episode_writer import note_id_for
from vixy.runtime import VixyRuntime
from vixy.schema import SchemaMismatch, bootstrap

ROOT = Path(__file__).resolve().parents[1]
SERVER = os.getenv("VIXY_TEST_FALKORDB_URL")
BACKENDS = ["embedded", pytest.param("server", marks=[pytest.mark.server, pytest.mark.skipif(
    not SERVER, reason="set VIXY_TEST_FALKORDB_URL to the shared FalkorDB (docker compose up -d falkordb)")])]
TIMELINE = demo_data.timeline()


def _config(backend: str, state: Path, user: str | None = None) -> VixyConfig:
    return VixyConfig(user_id=user or f"it_{uuid.uuid4().hex[:8]}", falkordb_url=SERVER if backend == "server" else None,
                      db_path=str(state / "vixy.rdb"), events_db=str(state / "events.sqlite3"),
                      runtime_db=str(state / "runtime.sqlite3"))


@pytest.fixture(params=BACKENDS)
def cfg(request, tmp_path):
    c = _config(request.param, tmp_path)
    c.extra["backend"] = request.param
    yield c
    try:
        connect_graph(c).delete()
    except Exception:
        pass


def _feed(rt: VixyRuntime, items=TIMELINE):
    return [rt.ingest_perception_frame(raw) if kind == "video" else rt.ingest_transcription(raw) for kind, raw in items]


def _calendar(rt: VixyRuntime):
    for c in demo_data.calendar_ms():
        rt.agent_memory.record_commitment(title=c["title"], start_ms=c["start_ms"], end_ms=c["end_ms"],
                                          status="confirmed", source="app", now_ms=c["start_ms"] - 86_400_000)


def _count(g, label: str) -> int:
    return g.query(f"MATCH (n:{label}) RETURN count(n)").result_set[0][0]


def _snapshot(g) -> dict:
    return {r[0]: r[1] for r in g.query("MATCH (n) RETURN labels(n)[0], count(n)").result_set} | {
        "edges": g.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]}


def _run_py(code: str, env: dict | None = None) -> str:
    out = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=ROOT, capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(ROOT), "MODEL_API_KEY": "", "BASETEN_API_KEY": "",
                              **(env or {})}, timeout=300)
    assert out.returncode == 0, out.stderr[-3000:]
    return out.stdout


# --------------------------------------------------------------------------- B. normalization
def test_adapters_preserve_ids_times_counts_speaker_and_gps():
    frame = {"frame_id": "frame_001", "timestamp": "2026-11-04T10:30:00Z", "latitude": 1.35, "longitude": 103.69,
             "classes": [{"class": "person", "count": 2, "confidence": 0.94}, {"class": "cup", "confidence": 0.7}]}
    v = adapters.video_from_perception(frame, session_id="u1", user_id="u1")
    assert (v.event_id, v.timestamp.isoformat(), v.frame_ref) == ("cam0:frame_001", "2026-11-04T10:30:00+00:00", "cam0/frame_001")
    assert [(d.label, d.confidence) for d in v.detections] == [("cup", 0.7), ("person", 0.94)]
    assert v.source["counts"] == {"person": 2}                       # cup had no count: none invented
    assert v.source["frame_id"] == "frame_001" and v.source["schema"] == "vixy.source.v1"
    assert (v.location.latitude, v.location.longitude) == (1.35, 103.69)
    assert v.description == "person x2, cup"                          # synthetic caption of detections only
    obs = adapters.agent_observation("video", v.model_dump(mode="json"))
    assert {c["class"]: c.get("count") for c in obs["classes"]} == {"person": 2, "cup": None}
    assert obs["frame_id"] == "cam0:frame_001" and obs["latitude"] == 1.35

    seg = {"segment_id": "s7", "recording_id": "r1", "start_time": "2026-11-04T18:30:02+08:00",
           "end_time": "2026-11-04T18:30:05+08:00", "text": " Alex is allergic to peanuts. ", "speaker_id": "spk_1",
           "speaker_is_wearer": False, "confidence": 0.91, "latitude": 1.3, "longitude": 103.7}
    a = adapters.audio_from_transcription(seg, session_id="u1", user_id="u1")
    assert (a.event_id, a.start_time.isoformat(), a.end_time.isoformat()) == (
        "asr:r1:s7", "2026-11-04T10:30:02+00:00", "2026-11-04T10:30:05+00:00")
    assert (a.text, a.speaker_id, a.confidence, a.source["speaker_is_wearer"]) == (
        "Alex is allergic to peanuts.", "spk_1", 0.91, False)
    agent_in = adapters.agent_observation("audio", a.model_dump(mode="json"))
    assert (agent_in["segment_id"], agent_in["speaker_is_user"], agent_in["latitude"]) == ("asr:r1:s7", False, 1.3)
    unknown = adapters.audio_from_transcription({**seg, "speaker_is_wearer": None, "speaker_id": None},
                                                session_id="u1", user_id="u1")
    assert "speaker_is_wearer" not in unknown.source and unknown.speaker_id is None
    assert adapters.agent_observation("audio", unknown.model_dump(mode="json"))["speaker_is_user"] is None
    with pytest.raises(Exception):                                     # naive time: CE policy, rejected
        adapters.audio_from_transcription({**seg, "start_time": "2026-11-04T18:30:02"}, session_id="u1", user_id="u1")
    rel = adapters.audio_from_transcription({"id": 3, "start": 2.0, "end": 4.5, "text": "hi", "recording_id": "r9"},
                                            session_id="u1", user_id="u1",
                                            recording_started_at=a.start_time)   # reuses CE's Whisper adapter
    assert rel.event_id == "asr:r9:3" and rel.source["segment_id"] == "3"


# ------------------------------------------------------------- A, C, D. ingestion, episodes, agent
def test_events_flow_through_context_engine_to_episode_writer_and_agent(cfg):
    with VixyRuntime(cfg) as rt:
        _calendar(rt)
        seen, handle = [], rt.agent.handle

        def spy(obs):                                         # what the agent receives, and the graph at that moment
            eid = obs.get("segment_id") or obs.get("frame_id")
            note = rt.store.q("MATCH (n:Note {source_event_id: $e}) RETURN n.id", {"e": eid}) if "transcript" in obs else None
            seen.append((eid, "transcript" in obs, bool(note)))
            return handle(obs)

        rt.agent.handle = spy
        bad = rt.ingest_video({"event_id": "x", "session_id": cfg.default_session, "timestamp": "2026-11-02T08:00:00",
                               "description": "no offset"})
        outcomes = _feed(rt)
        rt.flush_episodes()
        g = rt.graph

        # A. every record accepted by CE; the invalid one rejected before CE and never seen by the agent
        assert bad.status == "rejected" and bad.stage == "ingest"
        assert all(o.status == "processed" for o in outcomes)
        assert rt.engine.store.count(cfg.default_session) == len(TIMELINE)
        assert len(seen) == len(TIMELINE) and all(rt.engine.get_event_record(e) is not None for e, _, _ in seen)
        # the Note existed before the agent handled each utterance
        assert all(had_note for _, is_audio, had_note in seen if is_audio)

        # C. Notes and Visits with provenance
        notes = rt.store.q("MATCH (n:Note) RETURN n.id, n.source_event_id, n.payload_sha256, n.speaker_role, n.session_id")
        assert len(notes) == len(demo_data.SPEECH)
        for nid, eid, sha, role, session in notes:
            rec = rt.engine.get_event_record(eid)
            assert nid == note_id_for(eid) and sha == rec.payload_sha256 and session == cfg.default_session
        roles = {r[0]: r[1] for r in rt.store.q("MATCH (n:Note) RETURN n.text, n.speaker_role")}
        assert roles["Sam is vegetarian."] == "unknown" and roles["Marta has a presentation on Thursday at 9 AM."] == "other"
        visits = rt.store.q("MATCH (v:Visit)-[:ON_DAY]->(d:Day) RETURN v.first_event_id, v.last_event_id, v.place_type, d.day_id")
        assert len(visits) == len(demo_data.SCHEDULE)
        assert all(rt.engine.get_event_record(f) and rt.engine.get_event_record(last) for f, last, _, _ in visits)
        assert {p for _, _, p, _ in visits} == {"home", "office", "supermarket"}
        assert _count(g, "Frame") == 0                       # raw frames stay in SQLite

        # D. the agent's existing behaviours, fed from CE
        delivered = [d for o in outcomes for d in o.decisions if d["status"] == "done" and d["action"] != "NO_ACTION"]
        conflict = [d for d in delivered if d["action"] == "NOTIFY" and d["priority"] == "high"]
        assert len(conflict) == 1 and "Dentist" in conflict[0]["message"]
        alex = next(s for s in demo_data.speech() if s["text"].startswith("I'll meet Alex"))
        alex_event = f"asr:{alex['recording_id']}:{alex['segment_id']}"
        assert conflict[0]["trigger_event_id"] == alex_event
        assert sum("Reminder: buy milk" in (d["message"] or "") for d in delivered) == 1
        assert rt.store.q("MATCH (n:Note {kind: 'reminder'}) RETURN n.status") == [["done"]]
        # commitment provenance is the exact utterance event, not a time-window guess
        assert rt.store.q("MATCH (c:Commitment {title: 'Meet Alex'})-[:SUPPORTED_BY]->(n:Note) RETURN n.source_event_id") == [[alex_event]]
        assert rt.store.q("MATCH (l:AgentLog {action: 'NOTIFY', priority: 'high'}) RETURN l.trigger_event_id") == [[alex_event]]
        # an unknown speaker's "I like ..." is not the user's preference
        assert [r[0] for r in rt.store.q("MATCH (f:Fact {kind: 'preference'}) RETURN f.statement")] == [
            "I really like the noodles at the food court"]


# ------------------------------------------------------------ E, F, G. shared memory, Dreamer, retrieval
def test_dreamer_consolidates_into_the_same_graph_with_real_evidence(cfg):
    with VixyRuntime(cfg) as rt:
        _calendar(rt)
        _feed(rt)
        rt.flush_episodes()
        g = rt.graph
        dcfg = dreamer_config(cfg)
        assert dcfg.graph_name == cfg.graph_name == g.name and dcfg.unified           # E. one graph, one schema
        before = _snapshot(g)
        ops = rt.store.q("MATCH (n) WHERE n:Commitment OR n:Question OR n:AgentLog OR n:Note OR n:Visit "
                         "RETURN labels(n)[0], n.id, n.status ORDER BY n.id")

        if cfg.extra["backend"] == "server":
            # E. the Dreamer as a separate process (its own CLI) on the shared server graph
            out = subprocess.run([sys.executable, "-m", "prod_dreamer_model.cli", "--layout", "unified", "--user",
                                  cfg.user_id, "--out-dir", str(Path(cfg.events_db).parent / "dream"), "dream",
                                  "--offline", "--history-db", cfg.events_db], capture_output=True, text=True,
                                 env={**os.environ, "FALKORDB_URL": SERVER, "MODEL_API_KEY": ""}, timeout=300)
            assert out.returncode == 0, out.stderr
            assert "fallback wrote" in out.stdout
        else:
            result = run_dream(cfg, g, rt.engine)
            assert result["used_fallback"] and result["fallback"]["notes_unverified"] == 0
            assert result["evidence"]["inferred"] == 0

        # F. operational and observed records untouched; beliefs added; no frames; nothing evidence-less is active
        assert rt.store.q("MATCH (n) WHERE n:Commitment OR n:Question OR n:AgentLog OR n:Note OR n:Visit "
                          "RETURN labels(n)[0], n.id, n.status ORDER BY n.id") == ops
        after = _snapshot(g)
        assert after.get("LongTermMemory", 0) >= 4 and after.get("Pattern", 0) >= 3 and after.get("Frame", 0) == 0
        assert all(after.get(k) == v for k, v in before.items() if k in ("Commitment", "Question", "AgentLog", "Note", "Visit"))
        assert rt.store.q("MATCH (b) WHERE (b:LongTermMemory OR b:Pattern) AND b.status = 'active' "
                          "AND NOT (b)-[:SUPPORTED_BY]->() RETURN count(b)") == [[0]]
        assert rt.store.q("MATCH (b)-[:SUPPORTED_BY]->(e) WHERE b:LongTermMemory AND NOT (e:Note OR e:Visit) "
                          "RETURN count(e)") == [[0]]

        # G. deterministic retrieval of the memory and its original supporting utterance
        found = rt.memory.long_term_memories(about="Marta")
        assert [m["statement"] for m in found] == ["Marta has a presentation on Thursday at 9 AM."]
        explained = rt.memory.explain(found[0]["id"])
        assert explained["layer"] == "inferred" and explained["basis"] == "reported_speech"
        (ev,) = explained["evidence"]
        original = rt.engine.get_event_record(ev["source_event_id"])
        assert ev["label"] == "Note" and ev["layer"] == "observed" and ev["speaker_role"] == "other"
        assert (original.event["text"], original.payload_sha256) == (ev["text"], ev["payload_sha256"])
        assert rt.store.q("MATCH (p:Person {name: 'Marta'})<-[:ABOUT]-(:LongTermMemory)-[:SUPPORTED_BY]->(n:Note) "
                          "RETURN n.source_event_id") == [[original.event_id]]
        routine = rt.memory.long_term_memories(about="supermarket")
        assert routine and {e["label"] for e in rt.memory.evidence(routine[0]["id"])} == {"Visit"}
        # the agent reads the Dreamer's memories through the same interface
        assert [m["id"] for m in rt.agent_memory.long_term_memories(about="marta")] == [found[0]["id"]]
        assert any(m["kind"] == "preference" for m in rt.memory.long_term_memories())
        # unknown speaker's statement is kept as reported speech, with lower confidence
        sam = rt.memory.long_term_memories(about="Sam")
        assert sam and sam[0]["confidence"] < found[0]["confidence"]


# ------------------------------------------------------------------------------- H. idempotency
def test_replaying_events_and_dreams_creates_no_duplicates(cfg):
    with VixyRuntime(cfg) as rt:
        _calendar(rt)
        _feed(rt)
        rt.flush_episodes()
        run_dream(cfg, rt.graph, rt.engine)
        before, events = _snapshot(rt.graph), rt.engine.store.count()
        again = _feed(rt)
        assert {o.status for o in again} == {"duplicate"} and not any(o.decisions for o in again)
        run_dream(cfg, rt.graph, rt.engine)
        rt.flush_episodes()
        assert _snapshot(rt.graph) == before and rt.engine.store.count() == events
        # a re-sent event id with different content is a conflict, not an overwrite
        kind, raw = next(i for i in TIMELINE if i[0] == "audio")
        conflict = rt.ingest_transcription({**raw, "text": "something else"})
        assert conflict.status == "conflict" and _snapshot(rt.graph) == before
        # an event for a session this user does not own never reaches CE or the graph
        foreign = adapters.audio_from_transcription(raw, session_id="someone-else", user_id="someone-else")
        assert rt.ingest_audio(foreign).status == "rejected" and rt.engine.store.count() == events


# ------------------------------------------------------------------------- I. restart & recovery
_PHASE = """
import json, sys
from vixy import demo_data
from vixy.config import VixyConfig
from vixy.runtime import VixyRuntime
cfg = VixyConfig(**json.loads(sys.argv[1] if len(sys.argv) > 1 else {cfg!r}))
items = demo_data.timeline()
lo, hi = {lo}, {hi}
with VixyRuntime(cfg) as rt:
    if lo == 0:
        for c in demo_data.calendar_ms():
            rt.agent_memory.record_commitment(title=c["title"], start_ms=c["start_ms"], end_ms=c["end_ms"],
                                              status="confirmed", source="app", now_ms=c["start_ms"] - 86400000)
    outs = [rt.ingest_perception_frame(r) if k == "video" else rt.ingest_transcription(r) for k, r in items[lo:hi]]
    {finish}
    print(json.dumps({{"statuses": sorted({{o.status for o in outs}}), "startup": rt.startup,
                      "open": rt.writer.open_frames}}))
"""


def _phase(cfg: VixyConfig, lo: int, hi: int, finish: str = "pass") -> dict:
    raw = {"user_id": cfg.user_id, "falkordb_url": cfg.falkordb_url, "db_path": cfg.db_path,
           "events_db": cfg.events_db, "runtime_db": cfg.runtime_db}
    return json.loads(_run_py(_PHASE.format(cfg=json.dumps(raw), lo=lo, hi=hi, finish=finish)).strip().splitlines()[-1])


@pytest.mark.parametrize("backend", BACKENDS)
def test_restart_mid_visit_gives_the_same_graph_as_an_uninterrupted_run(backend, tmp_path):
    split = next(i for i, (k, r) in enumerate(TIMELINE)
                 if k == "video" and r["frame_id"].startswith("d1v6f0030"))          # inside Tuesday's supermarket visit
    a = _config(backend, tmp_path / "a")
    b = _config(backend, tmp_path / "b")
    try:
        _phase(a, 0, len(TIMELINE), "rt.flush_episodes()")                           # uninterrupted
        first = _phase(b, 0, split)                                                   # process 1 stops mid-visit
        assert first["open"] > 0
        second = _phase(b, split, len(TIMELINE), "rt.flush_episodes()")              # process 2 (new process)
        assert second["startup"]["rehydrate"]["frames_replayed"] == first["open"]
        assert second["statuses"] == ["processed"]
        ga, gb = connect_graph(a), connect_graph(b)
        q = "MATCH (v:Visit) RETURN v.start_ms, v.end_ms, v.place_type, v.frames ORDER BY v.start_ms"
        assert ga.query(q).result_set == gb.query(q).result_set
        q = "MATCH (n:Note) RETURN n.source_event_id, n.kind, n.status ORDER BY n.ts_ms"
        assert ga.query(q).result_set == gb.query(q).result_set
        q = "MATCH (c:Commitment) RETURN c.title, c.status ORDER BY c.title"
        assert ga.query(q).result_set == gb.query(q).result_set
    finally:
        for c in (a, b):
            try:
                connect_graph(c).delete()
            except Exception:
                pass


@pytest.mark.parametrize("backend", BACKENDS)
def test_memories_survive_process_restart(backend, tmp_path):
    cfg = _config(backend, tmp_path)
    try:
        _run_py(f"""
            import json
            from vixy import demo_data
            from vixy.config import VixyConfig
            from vixy.dream_job import run_dream
            from vixy.runtime import VixyRuntime
            cfg = VixyConfig(**json.loads({json.dumps(json.dumps({"user_id": cfg.user_id, "falkordb_url": cfg.falkordb_url, "db_path": cfg.db_path, "events_db": cfg.events_db, "runtime_db": cfg.runtime_db}))}))
            with VixyRuntime(cfg) as rt:
                for k, r in demo_data.timeline():
                    rt.ingest_perception_frame(r) if k == "video" else rt.ingest_transcription(r)
                rt.flush_episodes()
                run_dream(cfg, rt.graph, rt.engine)
        """)
        with VixyRuntime(cfg) as rt:                                                   # new process state, same storage
            assert rt.startup["recovered"] == [] and rt.startup["rehydrate"]["frames_replayed"] == 0
            (m,) = rt.memory.long_term_memories(about="Marta")
            (ev,) = rt.memory.evidence(m["id"])
            assert rt.engine.get_event_record(ev["source_event_id"]).payload_sha256 == ev["payload_sha256"]
            assert rt.ledger.counts() == {"done": len(TIMELINE)}
            assert {o.status for o in _feed(rt, TIMELINE[:50])} == {"duplicate"}
    finally:
        try:
            connect_graph(cfg).delete()
        except Exception:
            pass


class _Flaky:
    """A real graph that can be switched off, like a lost FalkorDB connection."""

    def __init__(self, g):
        self.g, self.down, self.name = g, False, g.name

    def query(self, *a, **k):
        if self.down:
            raise ConnectionError("FalkorDB unreachable (simulated)")
        return self.g.query(*a, **k)

    def ro_query(self, *a, **k):
        if self.down:
            raise ConnectionError("FalkorDB unreachable (simulated)")
        return self.g.ro_query(*a, **k)

    def delete(self):
        return self.g.delete()


def test_graph_outage_is_reported_and_recovered_without_duplicates(cfg):
    flaky = _Flaky(connect_graph(cfg))
    with VixyRuntime(cfg, graph=flaky) as rt:
        _calendar(rt)
        half = len(TIMELINE) // 2
        _feed(rt, TIMELINE[:half])
        flaky.down = True
        during = _feed(rt, TIMELINE[half:half + 400])
        failed = [o for o in during if o.status == "failed"]
        assert failed and all(o.stage in ("episode", "agent") for o in failed)
        assert all("unreachable" in o.error or "unavailable" in o.error for o in failed)
        assert rt.engine.store.count() == half + 400                  # CE kept every observation
        flaky.down = False
        recovered = rt.recover()
        assert recovered and all(o.status == "recovered" for o in recovered)
        assert rt.ledger.counts() == {"done": half + 400}
        _feed(rt, TIMELINE[half + 400:])
        rt.flush_episodes()
        g = rt.graph
        assert _count(g, "Note") == len(demo_data.SPEECH)
        assert _count(g, "Visit") == len(demo_data.SCHEDULE)
        assert rt.store.q("MATCH (c:Commitment) RETURN c.title ORDER BY c.title") == [["Dentist appointment"], ["Meet Alex"]]


def test_event_committed_before_a_crash_is_finished_at_startup(cfg):
    kind, raw = next(i for i in TIMELINE if i[0] == "audio" and "Marta" in i[1]["text"])
    seg = adapters.audio_from_transcription(raw, session_id=cfg.default_session, user_id=cfg.user_id)
    engine = ContextEngine(cfg.events_db)               # "the runtime died right after CE committed"
    assert engine.ingest_audio(seg).status == "accepted"
    engine.close()
    with VixyRuntime(cfg) as rt:
        assert rt.startup["recovered"] == [seg.event_id]
        assert rt.store.q("MATCH (n:Note {source_event_id: $e}) RETURN count(n)", {"e": seg.event_id}) == [[1]]
        assert rt.ingest_audio(seg).status == "duplicate"
        assert rt.recover() == []


# ------------------------------------------------------------------- agent gets CE context
class _RecordingLLM:
    def __init__(self):
        self.prompts = []

    def complete(self, system, user, max_tokens=200):
        self.prompts.append(user)
        return '{"intent": "commitment", "strength": "proposal", "title": "Dinner with Sam", "people": ["Sam"]}'


def test_agent_llm_path_receives_the_context_engine_snapshot(cfg):
    from agent.interpret import HybridInterpreter

    llm = _RecordingLLM()
    with VixyRuntime(cfg, interpreter=HybridInterpreter(llm)) as rt:
        base = {"recording_id": "r", "speaker_is_wearer": True}
        rt.ingest_transcription({**base, "segment_id": "1", "start_time": "2026-11-02T12:00:00+08:00",
                                 "end_time": "2026-11-02T12:00:03+08:00", "text": "Sam just got back from Lisbon."})
        rt.ingest_transcription({**base, "segment_id": "2", "start_time": "2026-11-02T12:00:10+08:00",
                                 "end_time": "2026-11-02T12:00:13+08:00", "text": "We should get dinner with Sam sometime"})
    assert len(llm.prompts) == 1                         # rules first; the LLM only for the uncertain one
    prompt = llm.prompts[0]
    assert "<recent_context>" in prompt and "Sam just got back from Lisbon." in prompt and "id=asr:r:1" in prompt


# --------------------------------------------------------------------------------- schema
def test_schema_is_versioned_and_owned_per_user(cfg):
    g = connect_graph(cfg)
    assert bootstrap(g, cfg.user_id)["created"] and not bootstrap(g, cfg.user_id)["created"]
    with pytest.raises(SchemaMismatch):
        bootstrap(g, "someone-else")
    g.query("MATCH (m:VixyMeta) SET m.schema_version = 'vixy.graph.v0'")
    with pytest.raises(SchemaMismatch):
        bootstrap(g, cfg.user_id)


def test_schema_document_covers_every_indexed_label():
    from vixy.schema import INDEXES
    doc = (ROOT / "docs" / "GRAPH_SCHEMA.md").read_text()
    for label, prop in INDEXES:
        assert f"(:{label}" in doc, label
