"""Milestone 1 changes to the agent and memory packages (unit level)."""

from datetime import datetime

import pytest

from agent.interpret import RuleInterpreter
from agent.models import AudioObservation
from memory.db import graph_name
from memory.frames import Frame, parse_ts
from memory.segmenter import Segmenter
from memory.store import TZ

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=TZ)


def _said(text, user):
    return AudioObservation.parse({"timestamp_start": NOW.isoformat(), "transcript": text, "speaker_is_user": user})


def test_vixy_is_the_wake_word_and_veyra_still_works():
    rules = RuleInterpreter()
    assert rules.interpret(_said("Vixy, add dentist tomorrow at 5pm", True), NOW).command == "add_commitment"
    assert rules.interpret(_said("hey veyra, add dentist tomorrow at 5pm", True), NOW).command == "add_commitment"
    assert rules.interpret(_said("Vixy, add dentist tomorrow at 5pm", None), NOW).intent == "chatter"


def test_naive_timestamps_are_rejected_like_context_engine():
    assert parse_ts("2026-11-02T01:00:00Z") == parse_ts("2026-11-02T09:00:00+08:00")
    with pytest.raises(ValueError):
        parse_ts("2026-11-02T09:00:00")


def test_graph_naming(monkeypatch):
    monkeypatch.delenv("VIXY_GRAPH_PREFIX", raising=False)
    assert graph_name("demo") == "vixy_demo" and graph_name("a/b") == "vixy_ab"
    assert graph_name("demo", prefix="veyra") == "veyra_demo"           # legacy graphs stay reachable


def test_add_note_attribution_and_replay(make_store):
    s = make_store("notes")
    ts = int(NOW.timestamp() * 1000)
    other = s.add_note("Remind me to call Sam.", ts, note_id="note_x", attribute_to_user=False)
    assert other["kind"] == "note" and s.open_reminders() == []
    s.add_note("I like spicy food", ts + 1, note_id="note_y", attribute_to_user=False)
    assert s.facts() == []                                       # not the user's preference
    assert s.q("MATCH (n:Note {id: 'note_y'}) RETURN n.intent_hint") == [["preference"]]

    mine = s.add_note("Remind me to buy milk at the supermarket.", ts + 2, note_id="note_z",
                      props={"source_event_id": "asr:r:1", "speaker_role": "wearer", "lat": None})
    assert mine["kind"] == "reminder" and mine["trigger_place_type"] == "supermarket"
    s.complete_reminder("note_z")
    s.add_note("Remind me to buy milk at the supermarket.", ts + 2, note_id="note_z",
               props={"source_event_id": "asr:r:1"})              # replay of the same event
    assert s.q("MATCH (n:Note {id: 'note_z'}) RETURN n.status, n.source_event_id") == [["done", "asr:r:1"]]
    assert s.stats()["notes"] == 3


def test_segmenter_exposes_open_frames_and_visit_frame_ids():
    seg = Segmenter("u")
    t0 = int(NOW.timestamp() * 1000)
    closed = []
    for i in range(10):
        closed += seg.add(Frame(f"f{i}", t0 + i * 10_000, {"supermarket": .9, "shopping cart": .8}))
    assert closed == [] and [f.frame_id for f in seg.open_frames()] == [f"f{i}" for i in range(10)]
    (v,) = seg.flush()
    assert (v.first_frame_id, v.last_frame_id, len(v.frame_ids)) == ("f0", "f9", 10)
    assert seg.open_frames() == []
