"""The shared-graph (unified) layout against a real embedded FalkorDB and context-engine.

The observed nodes below are written the way the Vixy runtime's Episode Writer
writes them (see master-repo docs/GRAPH_SCHEMA.md); the dreamer only reads them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prod_dreamer_model.bridge import EventBridge, EventHistory, bundles_from_records, load_export_jsonl
from prod_dreamer_model.config import DreamerConfig, unified_graph_name
from prod_dreamer_model.consolidate import person_statements
from prod_dreamer_model.dreamer import Dreamer, ResponsesMuseClient
from prod_dreamer_model.graph import ProdGraph
from prod_dreamer_model.tools import GraphToolBox

pytest.importorskip("redislite.falkordb_client")
ce = pytest.importorskip("context_engine")

T0 = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)      # Mon 09:00 Asia/Singapore


@pytest.fixture
def unified(tmp_path):
    from redislite.falkordb_client import FalkorDB

    config = DreamerConfig(graph_name=unified_graph_name("tester"), graph_layout="unified",
                           tz="Asia/Singapore", embedded_path=str(tmp_path / "g.rdb"))
    client = FalkorDB(str(tmp_path / "g.rdb"))
    g = client.select_graph(config.graph_name)
    engine = ce.ContextEngine(tmp_path / "events.sqlite3")
    yield ProdGraph(g, config), g, engine
    engine.close()
    g.delete()


def _note(g, engine, event_id, text, at, role="other", tamper=False):
    rec = engine.ingest_audio({"event_id": event_id, "session_id": "tester", "start_time": at.isoformat(),
                               "end_time": (at + timedelta(seconds=3)).isoformat(), "text": text})
    assert rec.status == "accepted"
    sha = engine.get_event_record(event_id).payload_sha256
    g.query("""
        MERGE (n:Note {id: $id})
        SET n.note_id = $eid, n.source_event_id = $eid, n.text = $text, n.ts_ms = $ts,
            n.local_date = $day, n.kind = 'note', n.speaker_role = $role, n.payload_sha256 = $sha
    """, {"id": f"note_{event_id}", "eid": event_id, "text": text + (" (edited)" if tamper else ""),
          "ts": int(at.timestamp() * 1000), "day": at.date().isoformat(), "role": role, "sha": sha})


def _visit(g, vid, day_offset, start_min, place_type="supermarket", tags=("supermarket", "shopping cart")):
    start = T0 + timedelta(days=day_offset, minutes=start_min - 9 * 60)
    g.query("""
        CREATE (:Visit {id: $id, kind: 'stay', start_ms: $s, end_ms: $s + 600000, local_date: $day,
                        dow: $dow, start_min: $m, place_type: $pt, activity: 'shopping', tags: $tags,
                        sensitive: false, frames: 120, first_event_id: $id + '-f0'})
    """, {"id": vid, "s": int(start.timestamp() * 1000), "day": (T0 + timedelta(days=day_offset)).date().isoformat(),
          "dow": (T0 + timedelta(days=day_offset)).weekday(), "m": start_min, "pt": place_type, "tags": list(tags)})


def _seed(graph, g, engine):
    g.query("CREATE (:VixyMeta {key: 'graph', schema_version: 'vixy.graph.v1', user_id: 'tester'})")
    g.query("CREATE (:Commitment {id: 'cmt_1', title: 'Meet Alex', status: 'tentative', start_ms: 1})")
    g.query("CREATE (:AgentLog {id: 'dec_1', action: 'NOTIFY', status: 'done'})")
    for day, minute in [(0, 17 * 60 + 30), (1, 17 * 60 + 40), (2, 17 * 60 + 35)]:
        _visit(g, f"vis_{day}", day, minute)
    _visit(g, "vis_clinic", 1, 600, place_type="healthcare", tags=("clinic",))
    _note(g, engine, "asr:1", "Marta has a presentation on Thursday at 9 AM.", T0)
    _note(g, engine, "asr:2", "Did you know Jon is vegetarian?", T0 + timedelta(minutes=5))      # question
    _note(g, engine, "asr:3", "Tom works at the hospital.", T0 + timedelta(minutes=9), tamper=True)
    _note(g, engine, "asr:4", "I'll meet Alex tomorrow at 6pm", T0 + timedelta(minutes=12), role="wearer")


def _dream(graph, engine):
    toolbox = GraphToolBox(graph, graph.config)
    graph.bootstrap()
    return Dreamer(toolbox, graph.config, client=None, log=lambda *_: None,
                   history=EventHistory.from_engine(engine)).dream()


def _count(g, label):
    return g.query(f"MATCH (n:{label}) RETURN count(n)").result_set[0][0]


def test_cli_key_alone_never_enables_paid_inference(unified, monkeypatch):
    from prod_dreamer_model import cli
    graph, g, engine = unified
    _seed(graph, g, engine)
    monkeypatch.setattr(cli, "open_graph", lambda config: graph)
    def reject(*args, **kwargs):
        pytest.fail("a key alone enabled paid inference")
    monkeypatch.setattr(cli, "ResponsesMuseClient", reject)
    args = cli.build_parser().parse_args(["dream", "--history-db", engine.store.db_path])
    assert not args.online and not args.offline
    assert cli.cmd_dream(args, graph.config.with_overrides(model_api_key="synthetic-test-key")) == 0
    assert cli.build_parser().parse_args(["dream", "--online"]).online


def test_unified_offline_dream_writes_evidence_backed_memories(unified):
    graph, g, engine = unified
    _seed(graph, g, engine)
    result = _dream(graph, engine)
    assert result.used_fallback and result.fallback["notes_unverified"] == 1    # the tampered note

    rows = g.query("""
        MATCH (p:Person)<-[:ABOUT]-(m:LongTermMemory)-[:SUPPORTED_BY]->(n:Note)
        RETURN p.name, m.statement, m.status, n.source_event_id
    """).result_set
    assert rows == [["Marta", "Marta has a presentation on Thursday at 9 AM.", "active", "asr:1"]]

    routine = g.query("""
        MATCH (m:LongTermMemory {memory_id: 'ltm-routine-supermarket'})-[:SUPPORTED_BY]->(v:Visit)
        RETURN m.status, m.statement, collect(v.id)
    """).result_set[0]
    assert routine[0] == "active" and "17:35" in routine[1] and sorted(routine[2]) == ["vis_0", "vis_1", "vis_2"]
    assert g.query("MATCH (m:LongTermMemory)-[:DERIVED_FROM]->(:Pattern {pattern_id: 'routine-supermarket'}) "
                   "RETURN count(m)").result_set[0][0] == 1
    # sensitive places never become routines; no frames; no evidence-less active belief
    assert not g.query("MATCH (m:LongTermMemory) WHERE m.statement CONTAINS 'healthcare' RETURN m").result_set
    assert _count(g, "Frame") == 0 and _count(g, "Class") == 0
    assert g.query("MATCH (b) WHERE (b:LongTermMemory OR b:Pattern) AND b.status = 'active' "
                   "AND NOT (b)-[:SUPPORTED_BY]->() RETURN count(b)").result_set[0][0] == 0


def test_unified_dream_is_idempotent_and_leaves_operational_records_alone(unified):
    graph, g, engine = unified
    _seed(graph, g, engine)
    _dream(graph, engine)
    snapshot = {label: _count(g, label) for label in
                ("LongTermMemory", "Pattern", "Person", "DreamRun", "Note", "Visit", "Commitment", "AgentLog")}
    edges = g.query("MATCH ()-[r:SUPPORTED_BY]->() RETURN count(r)").result_set[0][0]
    _dream(graph, engine)
    assert {label: _count(g, label) for label in snapshot} == snapshot
    assert g.query("MATCH ()-[r:SUPPORTED_BY]->() RETURN count(r)").result_set[0][0] == edges
    assert g.query("MATCH (c:Commitment {id: 'cmt_1'}) RETURN c.title, c.status").result_set == [["Meet Alex", "tentative"]]
    with pytest.raises(RuntimeError):
        graph.reset()
    with pytest.raises(RuntimeError):
        graph.load_bundle(None)


def test_without_history_notes_are_not_consolidated(unified):
    graph, g, engine = unified
    _seed(graph, g, engine)
    toolbox = GraphToolBox(graph, graph.config)
    Dreamer(toolbox, graph.config, client=None, log=lambda *_: None, history=None).dream()
    assert not g.query("MATCH (m:LongTermMemory)-[:ABOUT]->(:Person) RETURN m").result_set


def test_person_statement_rule_is_generic():
    assert person_statements("Alex is allergic to peanuts.") == [("Alex", "Alex is allergic to peanuts.")]
    assert person_statements("okay so Priya's sister lives in Lisbon") == [
        ("Priya", "Okay so Priya's sister lives in Lisbon.")]
    for text in ["I'll meet Alex tomorrow at 6pm", "Is Sam coming?", "Thursday is busy.",
                 "remind me to call Sam", "Vixy is listening."]:
        assert person_statements(text) == [], text


def test_context_engine_jsonl_export_loads_into_the_legacy_graph(tmp_path):
    from zoneinfo import ZoneInfo

    from prod_dreamer_model.mock_data import synthetic_days

    day = synthetic_days("2026-10-26", days=1, seed=7)[0]
    with EventBridge(str(tmp_path / "e.sqlite3"), "tester", "Asia/Singapore") as bridge:
        bridge.ingest_mock_day(day)
        n = bridge.export_jsonl(tmp_path / "export.jsonl", day.date, day.date + timedelta(days=1))
    records = load_export_jsonl(tmp_path / "export.jsonl")
    assert len(records) == n
    bundles = bundles_from_records(records, ZoneInfo("Asia/Singapore"))
    assert len(bundles[0].frames) == len(day.frames) and len(bundles[0].notes) == len(day.notes)
    history = EventHistory.from_jsonl(tmp_path / "export.jsonl")
    assert history.get(records[0].event_id).payload_sha256 == records[0].payload_sha256


def test_model_policy_refuses_contributor_models_by_default(monkeypatch):
    monkeypatch.delenv("MUSE_MODEL", raising=False)
    assert not DreamerConfig.from_env().muse_model.endswith("-contributor")
    risky = DreamerConfig(model_api_key="k", muse_model="muse-spark-1.3-contributor")
    with pytest.raises(ValueError, match="contributor"):
        ResponsesMuseClient(risky)
    ResponsesMuseClient(risky.with_overrides(allow_contributor_model=True))     # explicit opt-in only


def test_unified_graph_name_from_env(monkeypatch):
    monkeypatch.setenv("DREAMER_GRAPH_LAYOUT", "unified")
    monkeypatch.setenv("VIXY_USER_ID", "demo")
    monkeypatch.delenv("DREAMER_GRAPH_NAME", raising=False)
    monkeypatch.delenv("DREAMER_USER_ID", raising=False)
    cfg = DreamerConfig.from_env()
    assert cfg.graph_name == "vixy_demo" and cfg.unified and not cfg.allow_inferred_evidence
