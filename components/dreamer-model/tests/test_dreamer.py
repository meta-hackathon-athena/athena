"""The dream loop: tool dispatch, chaining and the deterministic fallback."""

from __future__ import annotations

from prod_dreamer_model.dreamer import Dreamer, parse_tool_args
from prod_dreamer_model.tools import GraphToolBox

from .helpers import FakeGraph, ScriptedClient, call


def _toolbox(config, frames=None, notes=None) -> GraphToolBox:
    return GraphToolBox(FakeGraph(frames, notes), config)


def test_one_round_of_tool_calls(config, week_frames):
    fake = FakeGraph(week_frames)
    toolbox = GraphToolBox(fake, config)
    client = ScriptedClient([
        {"id": "r1", "output": [call("run_cypher", {"cypher": "MATCH (f:Frame) RETURN f.frame_id LIMIT 5"}, "c1")]},
        {"id": "r2", "output": [
            call("write_pattern", {
                "pattern_id": "bus-morning", "statement": "Takes the bus to campus around 08:12 on weekdays.",
                "kind": "commute", "confidence": 0.8, "evidence": ["2026-10-26"],
                "about": ["bus interior"], "time_window": "08:00-08:30", "days": [0, 1],
            }, "c2"),
            call("write_memory", {
                "memory_id": "ltm-bus", "statement": "Takes the bus to campus around 08:12 on weekdays.",
                "kind": "routine", "confidence": 0.8, "from_patterns": ["bus-morning"],
            }, "c3"),
        ]},
        {"id": "r3", "output": [call("finish", {"summary": "found the commute", "pattern_ids": ["bus-morning"]}, "c4")]},
    ])

    result = Dreamer(toolbox, config, client=client, log=lambda *_: None).dream(rounds=1)

    assert result.rounds_run == 1
    assert result.used_fallback is False
    assert result.usage["cypher_calls"] == 1
    assert result.usage["patterns_written"] == 1
    assert result.usage["memories_written"] == 1
    assert "bus-morning" in fake.patterns
    assert fake.memories["ltm-bus"]["from_patterns"] == ["bus-morning"]
    assert result.round_summaries[0]["summary"] == "found the commute"
    # the read-only guard was applied to the model's query
    assert fake.reads == ["MATCH (f:Frame) RETURN f.frame_id LIMIT 5"]


def test_model_writing_nothing_triggers_the_fallback(config, week_frames):
    fake = FakeGraph(week_frames)
    toolbox = GraphToolBox(fake, config)
    client = ScriptedClient([
        {"id": "r1", "output": [{"type": "message", "phase": "commentary"}]},
        {"id": "r2", "output": [{"type": "message", "phase": "commentary"}]},
    ])

    result = Dreamer(toolbox, config, client=client, log=lambda *_: None).dream(rounds=1)

    assert result.used_fallback is True
    assert fake.patterns, "fallback should still store patterns"
    assert fake.memories, "fallback should still store long-term memories"
    statements = " ".join(p["statement"] for p in fake.patterns.values())
    assert "bus" in statements.lower()


def test_no_client_still_produces_the_end_product(config, week_frames):
    fake = FakeGraph(week_frames)
    toolbox = GraphToolBox(fake, config)

    result = Dreamer(toolbox, config, client=None, log=lambda *_: None).dream(rounds=4)

    assert result.rounds_run == 0
    assert result.used_fallback is True
    assert result.errors
    assert fake.memories


def test_bad_tool_arguments_are_reported_not_raised(config):
    fake = FakeGraph()
    toolbox = GraphToolBox(fake, config)
    client = ScriptedClient([
        {"id": "r1", "output": [call("write_pattern", {"pattern_id": "p", "statement": "   "}, "c1")]},
        {"id": "r2", "output": [call("run_cypher", {"cypher": "MATCH (n) DELETE n"}, "c2")]},
        {"id": "r3", "output": [call("finish", {"summary": "done"}, "c3")]},
    ])

    result = Dreamer(toolbox, config, client=client, log=lambda *_: None).dream(
        rounds=1, allow_fallback=False
    )

    assert result.rounds_run == 1
    assert not fake.patterns
    assert result.usage["cypher_errors"] == 1


def test_parse_tool_args_is_defensive():
    assert parse_tool_args("not json") == {}
    assert parse_tool_args(None) == {}
    assert parse_tool_args('{"a": 1}') == {"a": 1}
    assert parse_tool_args({"a": 1}) == {"a": 1}
