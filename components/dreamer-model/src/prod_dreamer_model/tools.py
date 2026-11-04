"""The dreamer's toolbox: read-only Cypher plus typed belief writers.

The model in the loop may only:
  * ``run_cypher``   — read the observed graph (guard in graph.safe_cypher), and
  * ``write_pattern`` / ``write_memory`` — store beliefs with evidence links,
  * ``finish``       — end the round.

Everything the model writes is validated here, so a hallucinated label or a
missing statement is a tool error it can recover from, never a corrupt graph.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .config import DreamerConfig
from .graph import MEMORY_KINDS, PATTERN_KINDS, ProdGraph

SCHEMA_BRIEF = """\
GRAPH SCHEMA (FalkorDB, openCypher)

OBSERVED — read-only for you:
  (:Day   {day_id, date, frame_count, note_count, first_ms, last_ms})
  (:Frame {frame_id, ts_ms, timestamp, local_date, local_hour, local_min, dow,
           description, lat, lon})-[:ON_DAY]->(:Day)
      local_hour 0-23, local_min minutes after local midnight,
      dow 0=Mon..6=Sun (Asia/Singapore local time)
  (:Frame)-[:SAW {count, confidence}]->(:Class {name})
      count = how many instances of that class were in that single frame
  (:Note  {note_id, ts_ms, timestamp, text, kind})-[:ON_DAY]->(:Day)
      kind = note | reminder | preference

BELIEVED — you own these (write only through the write_* tools):
  (:Pattern {pattern_id, statement, kind, confidence, status, time_window, days})
      kind one of: routine, schedule, commute, location, activity,
                   preference, social, anomaly, other
      time_window "HH:MM-HH:MM", days = [dow ints]
  (:LongTermMemory {memory_id, statement, kind, confidence, status})
      kind one of: fact, routine, preference, reminder, relationship,
                   place, schedule, other
  links: (:Pattern|:LongTermMemory)-[:SUPPORTED_BY]->(:Frame|:Note|:Day)
         (:Pattern|:LongTermMemory)-[:ABOUT]->(:Class)
         (:LongTermMemory)-[:DERIVED_FROM]->(:Pattern)

USEFUL QUERIES
  -- what classes exist and how often
  MATCH (f:Frame)-[s:SAW]->(c:Class)
  RETURN c.name AS class, count(f) AS frames, sum(s.count) AS instances
  ORDER BY frames DESC LIMIT 20

  -- when does a class appear? (routine detection)
  MATCH (f:Frame)-[:SAW]->(c:Class {name: 'bus interior'})
  RETURN f.dow AS dow, f.local_hour AS hour, count(*) AS frames,
         min(f.local_min) AS first_min
  ORDER BY dow, hour

  -- which days show a class, with a first and last sighting
  MATCH (f:Frame)-[:SAW]->(c:Class {name: 'food court'})
  RETURN f.local_date AS date, min(f.local_min) AS first_min,
         max(f.local_min) AS last_min, count(*) AS frames
  ORDER BY date

  -- co-occurring classes in one frame (what happens with what)
  MATCH (f:Frame)-[:SAW]->(a:Class {name: 'laptop'})
  MATCH (f)-[:SAW]->(b:Class)
  RETURN b.name AS with_class, count(*) AS frames
  ORDER BY frames DESC LIMIT 15

  -- walks / routes: consecutive GPS frames far apart in time but close in space
  MATCH (f:Frame) WHERE f.lat IS NOT NULL
  RETURN f.local_date AS date, f.local_min AS min, f.lat AS lat, f.lon AS lon
  ORDER BY f.ts_ms

  -- what the user said
  MATCH (n:Note) RETURN n.local_date, n.kind, n.text ORDER BY n.ts_ms

  -- beliefs already stored (update these, do not duplicate)
  MATCH (p:Pattern) RETURN p.pattern_id, p.statement, p.kind, p.confidence
  MATCH (m:LongTermMemory) RETURN m.memory_id, m.statement, m.kind, m.confidence
"""

SCHEMA_BRIEF_UNIFIED = """\
GRAPH SCHEMA — shared Vixy graph vixy.graph.v2 (FalkorDB, openCypher)

OBSERVED — written by the runtime's Episode Writer; read-only for you:
  (:Visit {id, kind, start_ms, end_ms, local_date, dow, start_min, place_type,
           activity, tags, sensitive, frames, session_id, first_event_id,
           last_event_id})-[:ON_DAY]->(:Day {day_id, date})
      one stay/transit episode segmented from camera frames; start_min =
      minutes after local midnight, dow 0=Mon..6=Sun. Raw frames are NOT in the
      graph (they stay in the context-engine event store).
  (:Visit)-[:SAW {ratio, conf, max_count}]->(:Tag {name})
  (:Visit)-[:AT]->(:Place {id, place_type, name, lat, lon})
  (:Note {id, note_id, source_event_id, text, ts_ms, local_date, kind,
          speaker_id, speaker_role})-[:ON_DAY]->(:Day)
      one transcribed utterance; source_event_id = the context-engine event id;
      speaker_role is wearer | other | unknown (never assume unknown = wearer)
  (:Note)-[:DURING]->(:Visit)
  (:Fact {id, kind, source, status, statement, confidence}) — rule-derived
      structured beliefs (frequent_place, usual_activity, user preference)

OPERATIONAL — owned by the Active Agent; never write, never contradict:
  (:Commitment), (:Question), (:AgentLog)

BELIEVED — you own these (write only through the write_* tools):
  (:Pattern {pattern_id, statement, kind, confidence, status, time_window, days})
  (:LongTermMemory {memory_id, statement, kind, confidence, status})
  links: (:Pattern|:LongTermMemory)-[:SUPPORTED_BY]->(:Visit|:Note)
         (:Pattern|:LongTermMemory)-[:ABOUT]->(:Tag|:Person)
         (:LongTermMemory)-[:DERIVED_FROM]->(:Pattern|:Fact)
  evidence = Visit.id or Note.id / Note.note_id values you actually saw;
  people = names of the people a belief is about (creates (:Person)).
  Structured claims (written by the deterministic consolidation, read them first):
  (:LongTermMemory {subject, predicate, object, polarity, epistemic_status,
                    claim_key, contested, event_start_ms?, valid_until_ms?})
      epistemic_status: reported (someone said it, unverified) | self_reported |
      hypothesis (hedged) | inferred (from episodes). status 'superseded' = history.
  (:LongTermMemory)-[:SUPERSEDES|CONFLICTS_WITH]->(:LongTermMemory)
  (:Person {id, name, status: resolved|provisional|ambiguous})-[:POSSIBLY_SAME_AS]->(:Person)

USEFUL QUERIES
  MATCH (v:Visit) RETURN v.local_date, v.start_min, v.place_type, v.activity, v.id
  ORDER BY v.start_ms
  MATCH (n:Note) RETURN n.id, n.local_date, n.speaker_role, n.text ORDER BY n.ts_ms
  MATCH (f:Fact {status: 'active'}) RETURN f.id, f.kind, f.statement
  MATCH (m:LongTermMemory) RETURN m.memory_id, m.statement, m.status
"""


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "run_cypher",
        "description": (
            "Run a READ-ONLY openCypher query against the memory graph and get rows "
            "back. Writes are rejected; use write_pattern / write_memory instead. "
            "Query freely, as many times as you need."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cypher": {"type": "string", "description": "Read-only Cypher query."},
                "params": {
                    "type": "object",
                    "description": "Optional query parameters, referenced as $name.",
                },
            },
            "required": ["cypher"],
        },
    },
    {
        "type": "function",
        "name": "write_pattern",
        "description": (
            "Create or update a :Pattern — a recurring behaviour you inferred, e.g. "
            "'commutes to campus by bus around 08:12 on weekdays'. Idempotent on "
            "pattern_id: reuse the same id to refine a pattern across rounds. "
            "Always link the frames/notes/days that support it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern_id": {"type": "string", "description": "Stable slug, e.g. 'bus-morning-commute'."},
                "statement": {"type": "string", "description": "One plain sentence about the user."},
                "kind": {"type": "string", "enum": sorted(PATTERN_KINDS)},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "frame_id / note_id / day_id values from the graph.",
                },
                "about": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Class names this pattern is about, e.g. ['bus interior'].",
                },
                "time_window": {"type": "string", "description": "e.g. '08:00-09:00'."},
                "people": {"type": "array", "items": {"type": "string"},
                           "description": "Names of people this pattern is about (unified graph)."},
                "days": {"type": "array", "items": {"type": "integer"}},
                "status": {"type": "string", "enum": ["active", "inactive"]},
            },
            "required": ["pattern_id", "statement", "evidence"],
        },
    },
    {
        "type": "function",
        "name": "write_memory",
        "description": (
            "Create or update a :LongTermMemory — a durable fact Vixy should remember "
            "about the user, e.g. 'takes the bus to university around 08:12 on "
            "weekdays'. Idempotent on memory_id. Consolidate patterns rather than "
            "duplicating them, and link the evidence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Stable slug, e.g. 'ltm-morning-bus'."},
                "statement": {"type": "string"},
                "kind": {"type": "string", "enum": sorted(MEMORY_KINDS)},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "about": {"type": "array", "items": {"type": "string"}},
                "from_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "pattern_id values this memory was distilled from.",
                },
                "people": {"type": "array", "items": {"type": "string"},
                           "description": "Names of people this memory is about (unified graph)."},
                "status": {"type": "string", "enum": ["active", "inactive"]},
            },
            "required": ["memory_id", "statement", "evidence"],
        },
    },
    {
        "type": "function",
        "name": "finish",
        "description": "End this dream round and report what you stored.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "What you concluded this round."},
                "pattern_ids": {"type": "array", "items": {"type": "string"}},
                "memory_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary"],
        },
    },
]

_SLUG = re.compile(r"[^a-z0-9]+")


def _people_kw(kwargs: dict[str, Any]) -> dict[str, Any]:
    people = kwargs.get("people")
    if not isinstance(people, list):
        return {}
    return {"people": [str(p)[:60] for p in people if isinstance(p, str) and p.strip()][:10]}


def slug(value: str, fallback: str) -> str:
    s = _SLUG.sub("-", str(value or "").strip().lower()).strip("-")
    return s[:80] or fallback


@dataclass
class ToolUsage:
    cypher_calls: int = 0
    cypher_errors: int = 0
    patterns_written: int = 0
    memories_written: int = 0
    finished: int = 0
    evidence_backfilled: int = 0
    calls_by_name: dict[str, int] = field(default_factory=dict)

    def note(self, name: str) -> None:
        self.calls_by_name[name] = self.calls_by_name.get(name, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "cypher_calls": self.cypher_calls,
            "cypher_errors": self.cypher_errors,
            "patterns_written": self.patterns_written,
            "memories_written": self.memories_written,
            "finished": self.finished,
            "evidence_backfilled": self.evidence_backfilled,
            "calls_by_name": self.calls_by_name,
        }


class GraphToolBox:
    """Validated bridge between the model's tool calls and ``ProdGraph``."""

    name = "falkordb"

    def __init__(self, graph: ProdGraph, config: DreamerConfig) -> None:
        self.graph = graph
        self.config = config
        self.usage = ToolUsage()

    # -- introspection passed to the model ---------------------------------

    @property
    def unified(self) -> bool:
        return self.config.unified

    @property
    def schema_brief(self) -> str:
        return SCHEMA_BRIEF_UNIFIED if self.unified else SCHEMA_BRIEF

    def stats(self):
        return self.graph.stats()

    def briefing(self, focus: str = "") -> str:
        if self.unified:
            return self._unified_briefing(focus)
        stats = self.graph.stats()
        patterns = self.graph.fetch_patterns()
        memories = self.graph.fetch_memories()
        notes = self.graph.fetch_notes()
        peak = self.graph.fetch_frame_projection(limit=3)

        def stub(items: list[dict], key: str) -> list[dict]:
            return [
                {
                    key: i[key],
                    "statement": i["statement"],
                    "kind": i["kind"],
                    "confidence": round(float(i["confidence"] or 0), 2),
                }
                for i in items
            ]

        brief: dict[str, Any] = {
            "focus": focus,
            "coverage": {
                "days": stats.days,
                "frames": stats.frames,
                "notes": stats.notes,
                "distinct_classes": stats.classes,
                "gps_frames": stats.gps_frames,
                "first_ms": stats.first_ms,
                "last_ms": stats.last_ms,
            },
            "existing_patterns": stub(patterns, "pattern_id"),
            "existing_long_term_memories": stub(memories, "memory_id"),
            "spoken_notes": [
                {"note_id": n["note_id"], "kind": n["kind"], "text": n["text"]}
                for n in notes
            ],
            "first_frames_sample": peak,
        }
        return json.dumps(brief, indent=2, default=str)

    def _unified_briefing(self, focus: str) -> str:
        visits = self.graph.fetch_visits()
        brief = {
            "focus": focus,
            "coverage": {
                "days": sorted({v["local_date"] for v in visits if v.get("local_date")}),
                "visits": len(visits),
                "notes": len(self.graph.fetch_notes()),
            },
            "existing_patterns": [
                {"pattern_id": p["pattern_id"], "statement": p["statement"], "status": p["status"]}
                for p in self.graph.fetch_patterns()
            ],
            "existing_long_term_memories": [
                {"memory_id": m["memory_id"], "statement": m["statement"], "status": m["status"]}
                for m in self.graph.fetch_memories()
            ],
            "spoken_notes": [
                {"id": n["id"], "note_id": n["note_id"], "speaker_role": n.get("speaker_role"),
                 "local_date": n.get("local_date"), "text": n["text"]}
                for n in self.graph.fetch_notes()
            ],
            "visits_sample": visits[:20],
        }
        return json.dumps(brief, indent=2, default=str)

    # -- tool dispatch -----------------------------------------------------

    def run_cypher(self, cypher: str, params: dict | None = None) -> dict[str, Any]:
        self.usage.cypher_calls += 1
        result = self.graph.run_read(cypher, params)
        if not result.get("ok"):
            self.usage.cypher_errors += 1
        return result

    def write_pattern(self, **kwargs: Any) -> dict[str, Any]:
        statement = str(kwargs.get("statement") or "").strip()
        if not statement:
            return {"ok": False, "error": "statement must not be empty"}
        pid = slug(kwargs.get("pattern_id") or statement, "pattern")
        result = self.graph.write_pattern(
            pattern_id=pid,
            statement=statement,
            kind=str(kwargs.get("kind") or "routine"),
            confidence=kwargs.get("confidence", 0.5),
            evidence=kwargs.get("evidence"),
            about=kwargs.get("about"),
            time_window=kwargs.get("time_window"),
            days=kwargs.get("days"),
            status=str(kwargs.get("status") or "active"),
            **_people_kw(kwargs),
        )
        self.usage.patterns_written += 1
        return {**result, "statement": statement, "kind": str(kwargs.get("kind") or "routine")}

    def write_memory(self, **kwargs: Any) -> dict[str, Any]:
        statement = str(kwargs.get("statement") or "").strip()
        if not statement:
            return {"ok": False, "error": "statement must not be empty"}
        mid = slug(kwargs.get("memory_id") or statement, "memory")
        result = self.graph.write_memory(
            memory_id=mid,
            statement=statement,
            kind=str(kwargs.get("kind") or "fact"),
            confidence=kwargs.get("confidence", 0.5),
            evidence=kwargs.get("evidence"),
            about=kwargs.get("about"),
            from_patterns=kwargs.get("from_patterns"),
            status=str(kwargs.get("status") or "active"),
            **_people_kw(kwargs),
        )
        self.usage.memories_written += 1
        return {**result, "statement": statement, "kind": str(kwargs.get("kind") or "fact")}

    def backfill_evidence(self) -> int:
        """Close provenance gaps: inherit from patterns, then from own claims."""
        propagated = self.graph.propagate_evidence()
        linked = self.graph.backfill_evidence()
        self.usage.evidence_backfilled = propagated + linked
        return self.usage.evidence_backfilled

    def finalize_evidence(self) -> dict[str, int]:
        """Run after every dream, whoever wrote the beliefs (model or fallback).

        1. A memory DERIVED_FROM a pattern inherits that pattern's recorded
           evidence (genuine: the pattern's links point at real records).
        2. Window-matched backfill (inferred provenance) only if explicitly
           allowed by ``config.allow_inferred_evidence``.
        3. Beliefs still without any evidence are marked "unconfirmed".
        """
        propagated = self.graph.propagate_evidence()
        inferred = self.graph.backfill_evidence() if self.config.allow_inferred_evidence else 0
        self.usage.evidence_backfilled = propagated + inferred
        status = self.graph.enforce_evidence()
        return {"propagated": propagated, "inferred": inferred, **status}

    def dispatch(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Execute one tool call. Returns (result, finished)."""
        self.usage.note(name)
        if name == "run_cypher":
            return self.run_cypher(args.get("cypher", ""), args.get("params")), False
        if name == "write_pattern":
            return self.write_pattern(**args), False
        if name == "write_memory":
            return self.write_memory(**args), False
        if name == "finish":
            self.usage.finished += 1
            return {
                "ok": True,
                "summary": args.get("summary", ""),
                "pattern_ids": args.get("pattern_ids", []),
                "memory_ids": args.get("memory_ids", []),
            }, True
        return {"ok": False, "error": f"unknown tool {name!r}"}, False
