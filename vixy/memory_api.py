"""Shared, read-only memory access for the runtime, the Active Agent and (later) the app API.

Deterministic graph queries only; every query runs through GRAPH.RO_QUERY when the
client supports it, so even a mistake here cannot write. Nothing in this module takes
Cypher from a caller or a model. Each record says which layer it belongs to:

  observed     what the sensors captured (Note, Visit)           -> source_event_id(s)
  inferred     what the system concluded (LongTermMemory, Pattern, Fact) -> SUPPORTED_BY
  operational  what the agent tracks (Commitment, Question, reminders)

Milestone 2 builds on this: ``vixy.retrieval`` (contextual retrieval), ``vixy.ask``
(natural-language questions) and ``vixy.api`` (the versioned HTTP interface).
"""

from __future__ import annotations

import json
from typing import Any

LAYER = {"Note": "observed", "Visit": "observed", "Day": "observed", "Reminder": "operational",
         "LongTermMemory": "inferred", "Pattern": "inferred", "Fact": "inferred",
         "Commitment": "operational", "Question": "operational", "AgentLog": "operational",
         "Person": "entity", "Place": "entity", "Tag": "entity"}


def _clean(d: Any) -> Any:
    """Drop None values (FalkorDB returns them for absent properties), also inside nested maps."""
    if isinstance(d, dict):
        return {k: _clean(v) for k, v in d.items() if v is not None}
    if isinstance(d, list):
        return [_clean(x) for x in d]
    return d


class SharedMemory:
    def __init__(self, graph):
        self.g = graph

    def _ro(self, cypher: str, params: dict | None = None) -> list[dict]:
        ro = getattr(self.g, "ro_query", None)
        res = ro(cypher, params or {}) if callable(ro) else self.g.query(cypher, params or {})
        header = [h[1] if isinstance(h, (list, tuple)) else h for h in (res.header or [])]
        return [_clean(dict(zip(header, row))) for row in res.result_set]

    # ---------------------------------------------------------------- observed
    def notes_on(self, local_date: str) -> list[dict]:
        rows = self._ro("""
            MATCH (n:Note {local_date: $d})
            OPTIONAL MATCH (n)-[:DURING]->(v:Visit)
            RETURN n.id AS id, n.text AS text, n.ts_ms AS ts_ms, n.timestamp AS timestamp, n.kind AS kind,
                   n.status AS status, n.speaker_id AS speaker_id, n.speaker_role AS speaker_role,
                   n.source_event_id AS source_event_id, n.session_id AS session_id, v.id AS visit_id
            ORDER BY n.ts_ms, n.id
        """, {"d": local_date})
        return [{**r, "label": "Note", "layer": "observed"} for r in rows]

    def visits_on(self, local_date: str) -> list[dict]:
        rows = self._ro("""
            MATCH (v:Visit {local_date: $d})
            OPTIONAL MATCH (v)-[:AT]->(p:Place)
            RETURN v.id AS id, v.kind AS kind, v.start_ms AS start_ms, v.end_ms AS end_ms,
                   v.start_time AS start_time, v.end_time AS end_time, v.place_type AS place_type,
                   v.activity AS activity, v.tags AS tags, v.frames AS frames,
                   v.first_event_id AS first_event_id, v.last_event_id AS last_event_id,
                   v.session_id AS session_id, p.id AS place_id, p.name AS place_name
            ORDER BY v.start_ms, v.id
        """, {"d": local_date})
        return [{**r, "label": "Visit", "layer": "observed"} for r in rows]

    def day(self, local_date: str) -> list[dict]:
        """Timeline of one local day: visits and notes interleaved by time."""
        items = self.visits_on(local_date) + self.notes_on(local_date)
        return sorted(items, key=lambda r: (r.get("start_ms", r.get("ts_ms", 0)), r["id"]))

    def timeline(self, start_ms: int, end_ms: int, limit: int = 500) -> list[dict]:
        """Visits and notes in [start_ms, end_ms), by time (bounded)."""
        visits = self._ro("""
            MATCH (v:Visit) WHERE v.end_ms >= $lo AND v.start_ms < $hi
            OPTIONAL MATCH (v)-[:AT]->(p:Place)
            RETURN v.id AS id, v.kind AS kind, v.start_ms AS start_ms, v.end_ms AS end_ms,
                   v.start_time AS start_time, v.end_time AS end_time, v.place_type AS place_type,
                   v.activity AS activity, v.tags AS tags, v.local_date AS local_date,
                   v.first_event_id AS first_event_id, v.last_event_id AS last_event_id,
                   p.id AS place_id, p.name AS place_name, v.sensitive AS sensitive
            ORDER BY v.start_ms LIMIT $n
        """, {"lo": start_ms, "hi": end_ms, "n": limit})
        notes = self._ro("""
            MATCH (n:Note) WHERE n.ts_ms >= $lo AND n.ts_ms < $hi
            RETURN n.id AS id, n.text AS text, n.ts_ms AS ts_ms, n.timestamp AS timestamp, n.kind AS kind,
                   n.status AS status, n.speaker_id AS speaker_id, n.speaker_role AS speaker_role,
                   n.source_event_id AS source_event_id, n.local_date AS local_date
            ORDER BY n.ts_ms LIMIT $n
        """, {"lo": start_ms, "hi": end_ms, "n": limit})
        items = [{**v, "label": "Visit", "layer": "observed"} for v in visits] + \
                [{**n, "label": "Note", "layer": "observed"} for n in notes]
        return sorted(items, key=lambda r: (r.get("start_ms", r.get("ts_ms", 0)), r["id"]))[:limit]

    # ---------------------------------------------------------------- inferred
    def long_term_memories(self, about: str | None = None, status: tuple[str, ...] = ("active",),
                           limit: int = 20) -> list[dict]:
        """Consolidated memories, optionally about a person, tag or word (case-insensitive)."""
        rows = self._ro("""
            MATCH (m:LongTermMemory) WHERE m.status IN $status
            // pattern comprehensions: OPTIONAL MATCH ... WHERE followed by another OPTIONAL
            // MATCH and a DISTINCT aggregate crashes the FalkorDB 6.0.3 server
            WITH m, [(m)-[:ABOUT]->(a) WHERE a:Person OR a:Tag | a.name] AS about,
                 size([(m)-[:SUPPORTED_BY]->(e) | e]) AS evidence_count
            WHERE $q IS NULL OR any(x IN about WHERE toLower(x) = $q) OR toLower(m.statement) CONTAINS $q
            RETURN m.id AS id, m.statement AS statement, m.kind AS kind, m.status AS status,
                   m.confidence AS confidence, m.basis AS basis, m.subject AS subject,
                   m.predicate AS predicate, m.object AS object, m.epistemic_status AS epistemic_status,
                   m.contested AS contested, m.reported_by AS reported_by,
                   m.updated_ms AS updated_ms, about, evidence_count
            ORDER BY m.confidence DESC, m.id LIMIT $limit
        """, {"status": list(status), "q": about.lower() if about else None, "limit": limit})
        return [{**r, "label": "LongTermMemory", "layer": "inferred"} for r in rows]

    def patterns(self, status: tuple[str, ...] = ("active",)) -> list[dict]:
        rows = self._ro("""
            MATCH (p:Pattern) WHERE p.status IN $status
            OPTIONAL MATCH (p)-[:SUPPORTED_BY]->(e)
            RETURN p.id AS id, p.statement AS statement, p.kind AS kind, p.status AS status,
                   p.confidence AS confidence, p.time_window AS time_window, p.days AS days,
                   count(DISTINCT e) AS evidence_count
            ORDER BY p.id
        """, {"status": list(status)})
        return [{**r, "label": "Pattern", "layer": "inferred"} for r in rows]

    def evidence(self, memory_id: str) -> list[dict]:
        """The observed records a belief is SUPPORTED_BY, with their context-engine event ids."""
        rows = self._ro("""
            MATCH (b {id: $id})-[:SUPPORTED_BY]->(e)
            WHERE b:LongTermMemory OR b:Pattern OR b:Fact OR b:Commitment
            RETURN labels(e)[0] AS label, e.id AS id, e.text AS text, e.ts_ms AS ts_ms,
                   e.timestamp AS timestamp, e.speaker_id AS speaker_id, e.speaker_role AS speaker_role,
                   e.source_event_id AS source_event_id, e.payload_sha256 AS payload_sha256,
                   e.session_id AS session_id, e.place_type AS place_type, e.start_ms AS start_ms,
                   e.end_ms AS end_ms, e.first_event_id AS first_event_id, e.last_event_id AS last_event_id
            ORDER BY coalesce(e.start_ms, e.ts_ms), e.id
        """, {"id": memory_id})
        return [{**r, "layer": LAYER.get(r["label"], "unknown")} for r in rows]

    def explain(self, memory_id: str) -> dict:
        """A belief, what it was derived from, how it relates to other beliefs, and the
        observations behind it."""
        rows = self._ro("""
            MATCH (b {id: $id}) WHERE b:LongTermMemory OR b:Pattern
            RETURN labels(b)[0] AS label, b.statement AS statement, b.status AS status, b.kind AS kind,
                   b.confidence AS confidence, b.basis AS basis, b.subject AS subject, b.subject_id AS subject_id,
                   b.predicate AS predicate, b.object AS object, b.polarity AS polarity,
                   b.epistemic_status AS epistemic_status, b.reported_by AS reported_by, b.speakers AS speakers,
                   b.support_count AS support_count, b.first_stated_ms AS first_stated_ms,
                   b.last_stated_ms AS last_stated_ms, b.valid_from_ms AS valid_from_ms,
                   b.valid_until_ms AS valid_until_ms, b.event_start_ms AS event_start_ms,
                   b.event_has_time AS event_has_time, b.time_text AS time_text, b.contested AS contested,
                   b.extracted_by AS extracted_by, b.derived_by AS derived_by, b.created_ms AS created_ms,
                   b.updated_ms AS updated_ms,
                   [(b)-[:DERIVED_FROM]->(d) | {label: labels(d)[0], id: d.id}] AS derived_from,
                   [(b)-[:ABOUT]->(p:Person) | {id: p.id, name: p.name, status: p.status}] AS people,
                   [(b)-[:CONFLICTS_WITH]-(o:LongTermMemory) | {id: o.id, statement: o.statement, status: o.status}]
                       AS conflicts_with,
                   [(b)-[:SUPERSEDES]->(o:LongTermMemory) | {id: o.id, statement: o.statement}] AS supersedes,
                   [(o:LongTermMemory)-[:SUPERSEDES]->(b) | {id: o.id, statement: o.statement}] AS superseded_by
        """, {"id": memory_id})
        if not rows:
            return {}
        out = rows[0]
        out["derived_from"] = [d for d in out.get("derived_from") or [] if d.get("id")]
        out["layer"] = "inferred"
        out["evidence"] = self.evidence(memory_id)
        return {"id": memory_id, **out}

    def person(self, person_id: str) -> dict:
        rows = self._ro("""
            MATCH (p:Person {id: $id})
            RETURN p.id AS id, p.name AS name, p.status AS status, p.aliases AS aliases,
                   [(p)-[:POSSIBLY_SAME_AS]-(q:Person) | {id: q.id, name: q.name}] AS possibly_same_as,
                   [(m:LongTermMemory)-[:ABOUT]->(p) WHERE m.status IN ['active', 'unconfirmed'] | m.id] AS memories
        """, {"id": person_id})
        return {**rows[0], "label": "Person", "layer": "entity"} if rows else {}

    # ---------------------------------------------------------------- decisions
    _LOG = """
        l.id AS id, l.ts_ms AS ts_ms, l.action AS action, l.priority AS priority, l.status AS status,
        l.trigger AS trigger, l.reason AS reason, l.message AS message, l.dedup_key AS dedup_key,
        l.trigger_event_id AS trigger_event_id, l.confidence AS confidence, l.reasoning AS reasoning,
        l.supporting_memory_ids AS supporting_memory_ids, l.supporting_json AS supporting_json,
        [(l)-[:CONCERNS]->(x) | {label: labels(x)[0], id: x.id}] AS concerns
    """

    @staticmethod
    def _decision_row(r: dict) -> dict:
        raw = r.pop("supporting_json", None)
        r["supporting_memories"] = json.loads(raw) if raw else []
        return {**r, "label": "AgentLog", "layer": "operational"}

    def decisions(self, *, action: str | None = None, since_ms: int = 0, limit: int = 50,
                  delivered_only: bool = False) -> list[dict]:
        rows = self._ro(f"""
            MATCH (l:AgentLog) WHERE l.ts_ms >= $since AND ($action IS NULL OR l.action = $action)
              AND (NOT $delivered OR l.status = 'done')
            RETURN {self._LOG} ORDER BY l.ts_ms DESC, l.id LIMIT $n
        """, {"since": since_ms, "action": action, "delivered": delivered_only, "n": limit})
        return [self._decision_row(r) for r in rows]

    def decision(self, decision_id: str) -> dict:
        rows = self._ro(f"MATCH (l:AgentLog {{id: $id}}) RETURN {self._LOG}", {"id": decision_id})
        if not rows:
            return {}
        d = self._decision_row(rows[0])
        d["memories"] = [self.explain(mid) for mid in d.get("supporting_memory_ids") or []]
        return d

    # ------------------------------------------------------------- operational
    def commitments(self, start_ms: int = 0, end_ms: int = 2**62) -> list[dict]:
        rows = self._ro("""
            MATCH (c:Commitment) WHERE c.start_ms >= $lo AND c.start_ms < $hi
            OPTIONAL MATCH (c)-[:SUPPORTED_BY]->(n:Note)
            RETURN c.id AS id, c.title AS title, c.start_ms AS start_ms, c.end_ms AS end_ms,
                   c.status AS status, c.source AS source, c.people AS people,
                   collect(n.source_event_id) AS source_event_ids
            ORDER BY c.start_ms
        """, {"lo": start_ms, "hi": end_ms})
        return [{**r, "label": "Commitment", "layer": "operational"} for r in rows]

    def reminders(self, status: str | None = "open") -> list[dict]:
        rows = self._ro("""
            MATCH (n:Note {kind: 'reminder'}) WHERE $s IS NULL OR n.status = $s
            RETURN n.id AS id, n.text AS text, n.status AS status, n.trigger_place_type AS trigger_place_type,
                   n.ts_ms AS ts_ms, n.source_event_id AS source_event_id
            ORDER BY n.ts_ms
        """, {"s": status})
        return [{**r, "label": "Note", "layer": "operational"} for r in rows]

    def open_questions(self) -> list[dict]:
        rows = self._ro("""
            MATCH (q:Question) WHERE q.status IN ['pending', 'asked']
            RETURN q.id AS id, q.kind AS kind, q.text AS text, q.status AS status, q.source AS source
            ORDER BY q.created_ms
        """)
        return [{**r, "label": "Question", "layer": "operational"} for r in rows]

    def stats(self) -> dict[str, Any]:
        return {r["label"]: r["n"] for r in self._ro("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS n")}
