"""FalkorDB graph layer for the production dreamer.

One graph per user (``vixy_dreamer`` by default). Two layers:

OBSERVED (written by the loader from context-engine's export; the dreamer may
only read it):

    (:Day  {day_id, date, session_id, frame_count, note_count, first_ms, last_ms})
    (:Frame {frame_id, ts_ms, timestamp, local_date, local_hour, local_min, dow,
             description, session_id, day_id, lat?, lon?})-[:ON_DAY]->(:Day)
    (:Frame)-[:SAW {count, confidence}]->(:Class {name})
    (:Note  {note_id, ts_ms, timestamp, text, kind, session_id, day_id})
            -[:ON_DAY]->(:Day)

BELIEVED (written by the dreamer, always linked to evidence):

    (:Pattern {pattern_id, statement, kind, confidence, status, time_window?,
               days?, created_ms, updated_ms})
    (:LongTermMemory {memory_id, statement, kind, confidence, status,
                      created_ms, updated_ms})
    (:Pattern|:LongTermMemory)-[:SUPPORTED_BY]->(:Frame|:Note|:Day)
    (:Pattern|:LongTermMemory)-[:ABOUT]->(:Class)
    (:LongTermMemory)-[:DERIVED_FROM]->(:Pattern)

FalkorDB conventions honoured here: epoch-ms integers for time (temporal types
cannot be range-indexed), never-null properties (optional ones are omitted),
and MERGE-based idempotent writes.

UNIFIED layout (``config.graph_layout == "unified"``): the same belief layer
lives in the shared per-user Vixy graph ``vixy_<user_id>`` (schema
``vixy.graph.v1``, documented in master-repo ``docs/GRAPH_SCHEMA.md``). There
the observed layer is written by the runtime's Episode Writer, not by this
module, so the dreamer reads ``(:Visit)``/``(:Note)`` instead of ``(:Frame)``,
``ABOUT`` targets ``(:Tag)``/``(:Person)``/``(:Place)`` instead of ``(:Class)``,
evidence resolves to ``Visit.id``/``Note.id``/``Note.source_event_id``, and
``reset()``/``load_bundle()`` refuse to run.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .bridge import DayBundle
from .config import DreamerConfig

UNIFIED_SCHEMA_VERSION = "vixy.graph.v2"
# Indexes the dreamer needs on the unified graph (the runtime creates the rest).
UNIFIED_INDEXES: list[tuple[str, str]] = [
    ("Pattern", "pattern_id"), ("Pattern", "id"), ("Pattern", "status"),
    ("LongTermMemory", "memory_id"), ("LongTermMemory", "id"), ("LongTermMemory", "status"),
    ("Person", "id"), ("Person", "name"), ("DreamRun", "id"),
    # vixy.graph.v2: structured claims, person resolution, model-extraction cache
    ("LongTermMemory", "claim_key"), ("LongTermMemory", "subject_id"), ("LongTermMemory", "predicate"),
    ("Person", "first_name"), ("Extraction", "id"),
]
# superseded: replaced by a newer statement of a change (kept, with valid_until_ms, for history)
BELIEF_STATUSES = {"active", "inactive", "unconfirmed", "superseded"}

INDEXES: list[tuple[str, str]] = [
    ("Day", "day_id"),
    ("Day", "date"),
    ("Frame", "frame_id"),
    ("Frame", "ts_ms"),
    ("Frame", "local_date"),
    ("Frame", "local_hour"),
    ("Frame", "day_id"),
    ("Class", "name"),
    ("Note", "note_id"),
    ("Note", "ts_ms"),
    ("Pattern", "pattern_id"),
    ("Pattern", "kind"),
    ("Pattern", "status"),
    ("LongTermMemory", "memory_id"),
    ("LongTermMemory", "kind"),
    ("LongTermMemory", "status"),
]

PATTERN_KINDS = {
    "routine", "schedule", "commute", "location", "activity",
    "preference", "social", "anomaly", "other",
}
MEMORY_KINDS = {
    "fact", "routine", "preference", "reminder", "relationship",
    "place", "schedule", "other",
}

_WRITE_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|SET|DELETE|DETACH|DROP|REMOVE|FOREACH|GRANT|REVOKE)\b"
    r"|\bLOAD\s+CSV\b"
    r"|\bCALL\s*\{"
    r"|\b(db|dbms)\s*\.\s*[A-Za-z_.]*(create|drop|delete|set|remove|merge)",
    re.IGNORECASE,
)
_READ_STARTS = ("MATCH", "OPTIONAL", "WITH", "UNWIND", "RETURN", "CALL", "SHOW", "EXPLAIN", "PROFILE")
_WINDOW = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")


def scrub(cypher: str) -> str:
    """Remove comments and string literals so keyword checks see only code."""
    text = re.sub(r"/\*.*?\*/", " ", cypher, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", " ", text)
    text = re.sub(r"'(?:[^'\\]|\\.)*'", "''", text)
    text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
    return text


def safe_cypher(cypher: str) -> tuple[bool, str]:
    """Read-only guard for ``run_cypher``. Writes go through the typed tools."""
    if not cypher or not cypher.strip():
        return False, "empty query"
    body = scrub(cypher).strip().rstrip(";").strip()
    if not body:
        return False, "empty query"
    if ";" in body:
        return False, "multiple statements are not allowed in one query"
    first = body.split(None, 1)[0].upper()
    if first not in _READ_STARTS:
        return False, (
            f"query must start with one of {', '.join(_READ_STARTS)} "
            "(this tool is read-only)"
        )
    match = _WRITE_KEYWORDS.search(body)
    if match:
        return False, (
            f"read-only tool: '{match.group(0)}' is not allowed. "
            "Use write_pattern / write_memory to store beliefs."
        )
    return True, ""


def rows(res: Any) -> list[dict[str, Any]]:
    """FalkorDB result -> list of dicts (header may be [type, alias] pairs)."""
    result_set = getattr(res, "result_set", None) or []
    header = getattr(res, "header", None) or []
    keys = [h[1] if isinstance(h, (list, tuple)) and len(h) > 1 else h for h in header]
    return [dict(zip(keys, r)) for r in result_set]


@dataclass
class GraphStats:
    labels: dict[str, int]
    relationships: dict[str, int]
    frames: int
    days: int
    notes: int
    classes: int
    patterns: int
    memories: int
    first_ms: int | None
    last_ms: int | None
    gps_frames: int
    visits: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "labels": self.labels,
            "relationships": self.relationships,
            "frames": self.frames,
            "days": self.days,
            "notes": self.notes,
            "classes": self.classes,
            "patterns": self.patterns,
            "memories": self.memories,
            "first_ms": self.first_ms,
            "last_ms": self.last_ms,
            "gps_frames": self.gps_frames,
            "visits": self.visits,
        }


class ProdGraph:
    """Thin, typed wrapper over one FalkorDB graph."""

    def __init__(self, graph: Any, config: DreamerConfig) -> None:
        self.g = graph
        self.config = config
        self.tz = ZoneInfo(config.tz)
        self.unified = config.unified
        # what a belief is ABOUT: detector classes are (:Class) in the legacy
        # graph and (:Tag) in the unified one (same concept, one label per graph)
        self.about_label = "Tag" if self.unified else "Class"

    # -- low level ---------------------------------------------------------

    def query(self, cypher: str, params: dict | None = None, timeout: int | None = None) -> Any:
        return self.g.query(cypher, params or {}, timeout=timeout or self.config.query_timeout_ms)

    def rows(self, cypher: str, params: dict | None = None, timeout: int | None = None):
        return rows(self.query(cypher, params, timeout))

    # -- setup -------------------------------------------------------------

    def bootstrap(self) -> None:
        for label, prop in (UNIFIED_INDEXES if self.unified else INDEXES):
            try:
                self.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except Exception as exc:  # already indexed
                if "already indexed" not in str(exc).lower():
                    raise

    def is_unified_graph(self) -> bool:
        res = self.rows("MATCH (m:VixyMeta {key: 'graph'}) RETURN m.schema_version AS v LIMIT 1")
        return bool(res and res[0].get("v"))

    def reset(self, force: bool = False) -> None:
        """Remove the whole graph contents (only ever called explicitly).

        Refused on a unified Vixy graph: it also holds the runtime's episodes
        and the agent's operational records, which the dreamer does not own.
        """
        if (self.unified or self.is_unified_graph()) and not force:
            raise RuntimeError(
                f"refusing to reset {self.config.graph_name}: it is a shared Vixy graph "
                "(episodes, notes, commitments, agent logs). Pass force=True only for "
                "a deliberate demo-data wipe."
            )
        self.query("MATCH ()-[r]->() DELETE r")
        self.query("MATCH (n) DELETE n")

    # -- load observed data -------------------------------------------------

    def load_bundle(self, bundle: DayBundle) -> dict[str, int]:
        if self.unified:
            raise RuntimeError(
                "load_bundle writes one :Frame per frame (legacy layout). In the unified "
                "graph the Episode Writer owns observed nodes; raw frames stay in SQLite."
            )
        session = bundle.session_id
        self.query(
            """
            MERGE (d:Day {day_id: $day})
            SET d.date = $date, d.session_id = $session,
                d.frame_count = $frame_count, d.note_count = $note_count,
                d.first_ms = $first_ms, d.last_ms = $last_ms
            """,
            {
                "day": bundle.day_id,
                "date": bundle.date,
                "session": session,
                "frame_count": len(bundle.frames),
                "note_count": len(bundle.notes),
                "first_ms": bundle.first_ms,
                "last_ms": bundle.last_ms,
            },
        )

        if bundle.frames:
            frame_rows = []
            for f in bundle.frames:
                local_dt = datetime.fromtimestamp(f.ts_ms / 1000, tz=self.tz)
                frame_rows.append(
                    {
                        "frame_id": f.frame_id,
                        "ts_ms": f.ts_ms,
                        "timestamp": f.timestamp,
                        "description": f.description,
                        "local_date": local_dt.date().isoformat(),
                        "local_hour": local_dt.hour,
                        "local_min": local_dt.hour * 60 + local_dt.minute,
                        "dow": local_dt.weekday(),
                        "day_id": bundle.day_id,
                    }
                )
            self.query(
                """
                UNWIND $rows AS r
                MERGE (f:Frame {frame_id: r.frame_id})
                SET f.ts_ms = r.ts_ms, f.timestamp = r.timestamp,
                    f.description = r.description, f.session_id = $session,
                    f.local_date = r.local_date, f.local_hour = r.local_hour,
                    f.local_min = r.local_min, f.dow = r.dow, f.day_id = r.day_id
                """,
                {"rows": frame_rows, "session": session},
            )
            self.query(
                """
                UNWIND $ids AS fid
                MATCH (f:Frame {frame_id: fid}), (d:Day {day_id: $day})
                MERGE (f)-[:ON_DAY]->(d)
                """,
                {"ids": [f.frame_id for f in bundle.frames], "day": bundle.day_id},
            )

            gps = [
                {"frame_id": f.frame_id, "lat": f.lat, "lon": f.lon}
                for f in bundle.frames
                if f.lat is not None and f.lon is not None
            ]
            if gps:
                self.query(
                    """
                    UNWIND $rows AS r
                    MATCH (f:Frame {frame_id: r.frame_id})
                    SET f.lat = r.lat, f.lon = r.lon
                    """,
                    {"rows": gps},
                )

            obs = [
                {
                    "frame_id": f.frame_id,
                    "name": c.name,
                    "count": c.count,
                    "confidence": c.confidence,
                }
                for f in bundle.frames
                for c in f.classes
            ]
            if obs:
                self.query(
                    """
                    UNWIND $rows AS r
                    MATCH (f:Frame {frame_id: r.frame_id})
                    MERGE (c:Class {name: r.name})
                    MERGE (f)-[s:SAW]->(c)
                    SET s.count = r.count, s.confidence = r.confidence
                    """,
                    {"rows": obs},
                )

        if bundle.notes:
            self.query(
                """
                UNWIND $rows AS r
                MERGE (n:Note {note_id: r.note_id})
                SET n.ts_ms = r.ts_ms, n.timestamp = r.timestamp, n.text = r.text,
                    n.kind = r.kind, n.session_id = $session, n.day_id = r.day_id
                """,
                {
                    "rows": [
                        {
                            "note_id": n.note_id,
                            "ts_ms": n.ts_ms,
                            "timestamp": n.timestamp,
                            "text": n.text,
                            "kind": n.kind,
                            "day_id": bundle.day_id,
                        }
                        for n in bundle.notes
                    ],
                    "session": session,
                },
            )
            self.query(
                """
                UNWIND $ids AS nid
                MATCH (n:Note {note_id: nid}), (d:Day {day_id: $day})
                MERGE (n)-[:ON_DAY]->(d)
                """,
                {"ids": [n.note_id for n in bundle.notes], "day": bundle.day_id},
            )

        return {"frames": len(bundle.frames), "notes": len(bundle.notes)}

    def load_bundles(self, bundles: Iterable[DayBundle]) -> dict[str, int]:
        totals = {"frames": 0, "notes": 0, "days": 0}
        for bundle in bundles:
            counts = self.load_bundle(bundle)
            totals["frames"] += counts["frames"]
            totals["notes"] += counts["notes"]
            totals["days"] += 1
        return totals

    # -- stats -------------------------------------------------------------

    def stats(self) -> GraphStats:
        labels = {r["label"] or "?": r["n"] for r in self.rows(
            "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n"
        )}
        rels = {r["rel"]: r["n"] for r in self.rows(
            "MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS n"
        )}
        span = self.rows(
            "MATCH (v:Visit) RETURN count(v) AS frames, min(v.start_ms) AS first_ms, "
            "max(v.end_ms) AS last_ms"
            if self.unified else
            "MATCH (f:Frame) RETURN count(f) AS frames, min(f.ts_ms) AS first_ms, "
            "max(f.ts_ms) AS last_ms"
        )
        first_ms = span[0]["first_ms"] if span else None
        last_ms = span[0]["last_ms"] if span else None
        gps = self.rows(
            "MATCH (f:Frame) WHERE f.lat IS NOT NULL RETURN count(f) AS n"
        )
        return GraphStats(
            labels=labels,
            relationships=rels,
            frames=int(labels.get("Frame", 0)),
            days=int(labels.get("Day", 0)),
            notes=int(labels.get("Note", 0)),
            classes=int(labels.get(self.about_label, 0)),
            patterns=int(labels.get("Pattern", 0)),
            memories=int(labels.get("LongTermMemory", 0)),
            first_ms=int(first_ms) if first_ms is not None else None,
            last_ms=int(last_ms) if last_ms is not None else None,
            gps_frames=int(gps[0]["n"]) if gps else 0,
            visits=int(labels.get("Visit", 0)),
        )

    # -- reads used by the report / heuristic ------------------------------

    def fetch_patterns(self) -> list[dict[str, Any]]:
        return self.rows(
            """
            MATCH (p:Pattern)
            OPTIONAL MATCH (p)-[:SUPPORTED_BY]->(e)
            OPTIONAL MATCH (p)-[:ABOUT]->(c:{about})
            RETURN p.pattern_id AS pattern_id, p.statement AS statement,
                   p.kind AS kind, p.confidence AS confidence, p.status AS status,
                   p.time_window AS time_window, p.days AS days,
                   p.created_ms AS created_ms, p.updated_ms AS updated_ms,
                   count(DISTINCT e) AS evidence_count,
                   collect(DISTINCT c.name) AS about
            ORDER BY p.kind, p.confidence DESC
            """.replace("{about}", self.about_label)
        )

    def fetch_memories(self) -> list[dict[str, Any]]:
        return self.rows(
            """
            MATCH (m:LongTermMemory)
            OPTIONAL MATCH (m)-[:SUPPORTED_BY]->(e)
            OPTIONAL MATCH (m)-[:ABOUT]->(c:{about})
            OPTIONAL MATCH (m)-[:ABOUT]->(per:Person)
            OPTIONAL MATCH (m)-[:DERIVED_FROM]->(dp:Pattern)
            RETURN m.memory_id AS memory_id, m.statement AS statement,
                   m.kind AS kind, m.confidence AS confidence, m.status AS status,
                   m.created_ms AS created_ms, m.updated_ms AS updated_ms,
                   count(DISTINCT e) AS evidence_count,
                   collect(DISTINCT c.name) AS about,
                   collect(DISTINCT dp.pattern_id) AS from_patterns,
                   collect(DISTINCT per.name) AS people
            ORDER BY m.kind, m.confidence DESC
            """.replace("{about}", self.about_label)
        )

    def fetch_evidence(self, node_id: str) -> list[dict[str, Any]]:
        return self.rows(
            """
            MATCH (b)-[:SUPPORTED_BY]->(e)
            WHERE b.pattern_id = $id OR b.memory_id = $id
            RETURN labels(e)[0] AS label, e.frame_id AS frame_id,
                   e.note_id AS note_id, e.day_id AS day_id,
                   e.local_date AS local_date, e.text AS text,
                   e.id AS id, e.source_event_id AS source_event_id,
                   e.first_event_id AS first_event_id, e.place_type AS place_type,
                   coalesce(e.start_ms, e.ts_ms) AS ts_ms
            ORDER BY local_date, ts_ms
            """,
            {"id": node_id},
        )

    def fetch_notes(self, limit: int | None = None) -> list[dict[str, Any]]:
        cypher = (
            "MATCH (n:Note) RETURN n.note_id AS note_id, n.ts_ms AS ts_ms, "
            "n.timestamp AS timestamp, n.text AS text, n.kind AS kind, "
            "n.day_id AS day_id ORDER BY n.ts_ms"
        )
        if self.unified:
            cypher = (
                "MATCH (n:Note) RETURN n.id AS id, n.note_id AS note_id, "
                "n.source_event_id AS source_event_id, n.session_id AS session_id, "
                "n.payload_sha256 AS payload_sha256, n.ts_ms AS ts_ms, "
                "n.timestamp AS timestamp, n.text AS text, n.kind AS kind, "
                "n.local_date AS local_date, n.day_id AS day_id, "
                "n.speaker_id AS speaker_id, n.speaker_role AS speaker_role "
                "ORDER BY n.ts_ms, n.id"
            )
        if limit:
            cypher += f" LIMIT {int(limit)}"
        return self.rows(cypher)

    def fetch_visits(self) -> list[dict[str, Any]]:
        """Unified layout: the Episode Writer's visits, one row each."""
        return self.rows(
            """
            MATCH (v:Visit)
            OPTIONAL MATCH (v)-[:AT]->(p:Place)
            RETURN v.id AS id, v.kind AS kind, v.start_ms AS start_ms, v.end_ms AS end_ms,
                   v.local_date AS local_date, v.dow AS dow, v.start_min AS start_min,
                   v.place_type AS place_type, v.activity AS activity, v.tags AS tags,
                   v.sensitive AS sensitive, v.frames AS frames,
                   v.first_event_id AS first_event_id, v.session_id AS session_id,
                   p.id AS place_id
            ORDER BY v.start_ms, v.id
            """
        )

    def fetch_facts(self, status: str = "active") -> list[dict[str, Any]]:
        """Unified layout: structured Facts with the ids of the records behind them."""
        return self.rows(
            """
            MATCH (f:Fact {status: $status})
            OPTIONAL MATCH (f)-[:ABOUT]->(p:Place)
            // pattern comprehension, not OPTIONAL MATCH ... WHERE + collect(DISTINCT):
            // that shape crashes the FalkorDB 6.0.3 server (reply.rs unwrap panic)
            RETURN f.id AS id, f.kind AS kind, f.source AS source, f.statement AS statement,
                   f.confidence AS confidence, f.support_days AS support_days,
                   f.activity AS activity, f.usual_arrival_min AS usual_arrival_min,
                   f.dows AS dows, p.id AS place_id, p.place_type AS place_type,
                   p.name AS place_name,
                   [(f)-[:SUPPORTED_BY]->(e) WHERE e:Visit OR e:Note | e.id] AS evidence
            ORDER BY f.id
            """,
            {"status": status},
        )

    def fetch_frame_projection(
        self, day_id: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """One compact row per frame: identity, local time, class names."""
        where = "WHERE f.day_id = $day" if day_id else ""
        limit_clause = f"LIMIT {int(limit)}" if limit else ""
        return self.rows(
            f"""
            MATCH (f:Frame)
            {where}
            OPTIONAL MATCH (f)-[:SAW]->(c:Class)
            RETURN f.frame_id AS frame_id, f.ts_ms AS ts_ms,
                   f.local_date AS local_date, f.local_hour AS local_hour,
                   f.local_min AS local_min, f.dow AS dow,
                   f.lat AS lat, f.lon AS lon,
                   collect(c.name) AS classes
            ORDER BY f.ts_ms
            {limit_clause}
            """,
            {"day": day_id} if day_id else None,
        )

    def count_evidence(self, node_id: str) -> int:
        res = self.rows(
            "MATCH (b)-[:SUPPORTED_BY]->(e) "
            "WHERE b.pattern_id = $id OR b.memory_id = $id RETURN count(e) AS n",
            {"id": node_id},
        )
        return int(res[0]["n"]) if res else 0

    # -- read-only cypher tool --------------------------------------------

    def run_read(self, cypher: str, params: dict | None = None) -> dict[str, Any]:
        ok, reason = safe_cypher(cypher)
        if not ok:
            return {"ok": False, "error": reason}
        try:
            ro_query = getattr(self.g, "ro_query", None)
            if callable(ro_query):
                # GRAPH.RO_QUERY: the server itself rejects writes, so the regex
                # guard above is defence in depth rather than the only barrier
                result = ro_query(cypher, params or {}, timeout=self.config.query_timeout_ms)
            else:
                result = self.query(cypher, params)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        data = rows(result)
        cap = self.config.row_cap
        truncated = len(data) > cap
        return {
            "ok": True,
            "row_count": len(data),
            "truncated": truncated,
            "rows": data[:cap],
        }

    # -- belief writes -----------------------------------------------------

    def write_pattern(
        self,
        pattern_id: str,
        statement: str,
        kind: str = "routine",
        confidence: float = 0.5,
        evidence: list[str] | None = None,
        about: list[str] | None = None,
        time_window: str | None = None,
        days: list[int] | None = None,
        status: str = "active",
        people: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = int(time.time() * 1000)
        props: dict[str, Any] = {
            **_clean_extra(extra),
            "id": pattern_id,
            "layer": "believed",
            "statement": statement.strip(),
            "kind": kind if kind in PATTERN_KINDS else "other",
            "confidence": _clamp(confidence),
            "status": status if status in BELIEF_STATUSES else "active",
            "updated_ms": now,
        }
        if time_window:
            props["time_window"] = str(time_window)
        if days:
            props["days"] = sorted({int(d) for d in days if 0 <= int(d) <= 6})

        self.query(
            "MERGE (p:Pattern {pattern_id: $id}) ON CREATE SET p.created_ms = $now",
            {"id": pattern_id, "now": now},
        )
        set_clause = ", ".join(f"p.{k} = ${k}" for k in props)
        self.query(
            f"MATCH (p:Pattern {{pattern_id: $id}}) SET {set_clause}",
            {**props, "id": pattern_id},
        )
        linked = self._link_evidence("Pattern", "pattern_id", pattern_id, evidence)
        self._link_about("Pattern", "pattern_id", pattern_id, about)
        self._link_people("Pattern", "pattern_id", pattern_id, people)
        return {"ok": True, "pattern_id": pattern_id, "evidence_linked": linked}

    def write_memory(
        self,
        memory_id: str,
        statement: str,
        kind: str = "fact",
        confidence: float = 0.5,
        evidence: list[str] | None = None,
        about: list[str] | None = None,
        from_patterns: list[str] | None = None,
        status: str = "active",
        people: list[str] | None = None,
        from_facts: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = int(time.time() * 1000)
        props: dict[str, Any] = {
            **_clean_extra(extra),
            "id": memory_id,
            "layer": "believed",
            "statement": statement.strip(),
            "kind": kind if kind in MEMORY_KINDS else "other",
            "confidence": _clamp(confidence),
            "status": status if status in BELIEF_STATUSES else "active",
            "updated_ms": now,
        }
        self.query(
            "MERGE (m:LongTermMemory {memory_id: $id}) ON CREATE SET m.created_ms = $now",
            {"id": memory_id, "now": now},
        )
        set_clause = ", ".join(f"m.{k} = ${k}" for k in props)
        self.query(
            f"MATCH (m:LongTermMemory {{memory_id: $id}}) SET {set_clause}",
            {**props, "id": memory_id},
        )
        linked = self._link_evidence("LongTermMemory", "memory_id", memory_id, evidence)
        self._link_about("LongTermMemory", "memory_id", memory_id, about)
        self._link_people("LongTermMemory", "memory_id", memory_id, people)
        self.link_facts(memory_id, from_facts)

        patterns = [p for p in (from_patterns or []) if p]
        if patterns:
            self.query(
                """
                MATCH (m:LongTermMemory {memory_id: $id})
                UNWIND $patterns AS pid
                MATCH (p:Pattern {pattern_id: pid})
                MERGE (m)-[:DERIVED_FROM]->(p)
                """,
                {"id": memory_id, "patterns": patterns},
            )
        return {"ok": True, "memory_id": memory_id, "evidence_linked": linked}

    def link_facts(self, memory_id: str, fact_ids: list[str] | None) -> None:
        """(:LongTermMemory)-[:DERIVED_FROM]->(:Fact) for structured facts it consolidates."""
        facts = [f for f in (fact_ids or []) if f]
        if facts:
            self.query(
                """
                MATCH (m:LongTermMemory {memory_id: $id})
                UNWIND $facts AS fid
                MATCH (f:Fact {id: fid})
                MERGE (m)-[:DERIVED_FROM]->(f)
                """,
                {"id": memory_id, "facts": facts},
            )

    def _link_evidence(
        self, label: str, key: str, node_id: str, evidence: list[str] | None
    ) -> int:
        ids = [str(e) for e in (evidence or []) if e]
        if not ids:
            return 0
        # Resolve each id against exactly one label so a day_id links the Day
        # node, not every frame that happened to be on that day. Only observed
        # records are valid evidence; an id that matches nothing links nothing.
        targets: tuple[tuple[str, str], ...] = (("Day", "day_id"), ("Frame", "frame_id"), ("Note", "note_id"))
        if self.unified:
            # Real episodes and utterances only (a Day is an aggregate, not an
            # observation). Note.note_id == Note.source_event_id == the
            # context-engine event_id, so a cited event id resolves too.
            targets = (("Visit", "id"), ("Note", "id"), ("Note", "note_id"))
        count = 0
        for target_label, target_key in targets:
            res = self.rows(
                f"""
                MATCH (b:{label} {{{key}: $id}})
                UNWIND $evidence AS eid
                MATCH (n:{target_label} {{{target_key}: eid}})
                MERGE (b)-[:SUPPORTED_BY]->(n)
                RETURN count(n) AS n
                """,
                {"id": node_id, "evidence": ids},
            )
            count += int(res[0]["n"]) if res else 0
        return count

    def _link_about(
        self, label: str, key: str, node_id: str, about: list[str] | None
    ) -> None:
        names = sorted({str(a).strip().lower() for a in (about or []) if str(a).strip()})
        if not names:
            return
        self.query(
            f"""
            MATCH (b:{label} {{{key}: $id}})
            UNWIND $names AS nm
            MERGE (c:{self.about_label} {{name: nm}})
            MERGE (b)-[:ABOUT]->(c)
            """,
            {"id": node_id, "names": names},
        )

    def _link_people(
        self, label: str, key: str, node_id: str, people: list[str] | None
    ) -> None:
        """(belief)-[:ABOUT]->(:Person). Identity = the name as heard (no resolution yet)."""
        names = sorted({str(p).strip() for p in (people or []) if str(p).strip()})
        if not names:
            return
        self.query(
            f"""
            MATCH (b:{label} {{{key}: $id}})
            UNWIND $people AS per
            MERGE (p:Person {{id: per.id}})
            ON CREATE SET p.name = per.name, p.created_ms = $now, p.source = 'dreamer'
            MERGE (b)-[:ABOUT]->(p)
            """,
            {"id": node_id, "now": int(time.time() * 1000),
             "people": [{"id": person_id(n), "name": n} for n in names]},
        )

    # -- entities and belief relations (unified layout) ---------------------

    def upsert_person(self, person_id: str, props: dict[str, Any]) -> None:
        self.query(
            """
            MERGE (p:Person {id: $id})
            ON CREATE SET p.created_ms = $now, p.source = 'dreamer'
            SET p += $props
            """,
            {"id": person_id, "props": _clean_extra(props), "now": int(time.time() * 1000)},
        )

    def link_persons(self, memory_id: str, person_ids: list[str]) -> None:
        """(belief)-[:ABOUT]->(:Person) for already-resolved Person ids."""
        if person_ids:
            self.query(
                """
                MATCH (m:LongTermMemory {memory_id: $id})
                UNWIND $pids AS pid
                MATCH (p:Person {id: pid})
                MERGE (m)-[:ABOUT]->(p)
                """,
                {"id": memory_id, "pids": sorted(set(person_ids))},
            )

    def relate(self, label: str, from_id: str, rel: str, to_id: str, props: dict[str, Any] | None = None) -> None:
        """Belief-to-belief or person-to-person edge, MERGEd (idempotent)."""
        if rel not in {"SUPERSEDES", "CONFLICTS_WITH", "POSSIBLY_SAME_AS"} or label not in {"LongTermMemory", "Person"}:
            raise ValueError(f"relation not allowed: {label} {rel}")
        self.query(
            f"""
            MATCH (a:{label} {{id: $a}}) MATCH (b:{label} {{id: $b}})
            MERGE (a)-[r:{rel}]->(b)
            SET r += $props
            """,
            {"a": from_id, "b": to_id, "props": _clean_extra(props)},
        )

    def cached_extractions(self, extractor: str, note_ids: list[str]) -> dict[str, list[dict]]:
        """Validated model output already stored for these notes (so it is never re-requested)."""
        import json

        if not note_ids:
            return {}
        res = self.rows(
            "MATCH (x:Extraction) WHERE x.extractor = $ex AND x.note_id IN $ids "
            "RETURN x.note_id AS note_id, x.claims AS claims",
            {"ex": extractor, "ids": list(note_ids)},
        )
        return {r["note_id"]: json.loads(r["claims"] or "[]") for r in res}

    def store_extraction(self, extractor: str, note_id: str, claims: list[dict]) -> None:
        import hashlib
        import json

        xid = "ext_" + hashlib.sha1(f"{extractor}|{note_id}".encode()).hexdigest()[:16]
        self.query(
            """
            MERGE (x:Extraction {id: $id})
            ON CREATE SET x.created_ms = $now
            SET x.extractor = $ex, x.note_id = $nid, x.claims = $claims, x.claim_count = $n
            WITH x MATCH (n:Note {id: $nid}) MERGE (x)-[:FROM]->(n)
            """,
            {"id": xid, "ex": extractor, "nid": note_id, "claims": json.dumps(claims), "n": len(claims),
             "now": int(time.time() * 1000)},
        )

    # -- evidence hygiene --------------------------------------------------

    def add_evidence(self, node_id: str, evidence: list[str]) -> int:
        """Link evidence to whichever belief node owns ``node_id``."""
        for label, key in (("Pattern", "pattern_id"), ("LongTermMemory", "memory_id")):
            found = self.rows(
                f"MATCH (b:{label} {{{key}: $id}}) RETURN b LIMIT 1", {"id": node_id}
            )
            if found:
                return self._link_evidence(label, key, node_id, evidence)
        return 0

    def enforce_evidence(self) -> dict[str, int]:
        """A belief with no SUPPORTED_BY edge is never presented as active.

        Active beliefs without evidence become "unconfirmed"; unconfirmed ones
        that have since gained real evidence become active again. "inactive"
        (deliberately retired) is left alone.
        """
        out = {"unconfirmed": 0, "confirmed": 0}
        for label in ("Pattern", "LongTermMemory"):
            res = self.rows(
                f"MATCH (b:{label}) WHERE b.status = 'active' AND NOT (b)-[:SUPPORTED_BY]->() "
                "SET b.status = 'unconfirmed' RETURN count(b) AS n"
            )
            out["unconfirmed"] += int(res[0]["n"]) if res else 0
            res = self.rows(
                f"MATCH (b:{label}) WHERE b.status = 'unconfirmed' AND (b)-[:SUPPORTED_BY]->() "
                "SET b.status = 'active' RETURN count(b) AS n"
            )
            out["confirmed"] += int(res[0]["n"]) if res else 0
        return out

    def record_run(self, run_id: str, props: dict[str, Any]) -> None:
        """One (:DreamRun) per consolidation run: what was read and written (audit)."""
        self.query(
            "MERGE (r:DreamRun {id: $id}) SET r += $props",
            {"id": run_id, "props": _clean_extra(props)},
        )

    def propagate_evidence(self) -> int:
        """A memory derived from a pattern inherits that pattern's evidence."""
        res = self.rows(
            """
            MATCH (m:LongTermMemory)-[:DERIVED_FROM]->(p:Pattern)-[:SUPPORTED_BY]->(e)
            MERGE (m)-[:SUPPORTED_BY]->(e)
            RETURN count(e) AS n
            """
        )
        return int(res[0]["n"]) if res else 0

    def backfill_evidence(self, per_node: int = 5) -> int:
        """Give evidence-less patterns provenance from their own window/days/about.

        The model is told to pass evidence, but a belief without provenance is
        not useful for "why do you think that?", so the loader closes the gap
        deterministically from the pattern's own claims.
        """
        linked = 0
        for pattern in self.fetch_patterns():
            if pattern.get("evidence_count"):
                continue
            candidates = self._candidate_evidence(
                pattern.get("days"),
                pattern.get("time_window"),
                pattern.get("about"),
                per_node,
            )
            if candidates:
                linked += self.add_evidence(pattern["pattern_id"], candidates)
        return linked

    def _candidate_evidence(
        self,
        days: Any,
        time_window: Any,
        about: Any,
        limit: int,
    ) -> list[str]:
        day_filter = [int(d) for d in (days or []) if str(d).strip() != ""]
        about_filter = [str(a).strip().lower() for a in (about or []) if str(a).strip()]
        match = _WINDOW.search(str(time_window or ""))
        lo = hi = -1
        if match:
            lo = int(match.group(1)) * 60 + int(match.group(2))
            hi = int(match.group(3)) * 60 + int(match.group(4))
        if not day_filter and not about_filter and lo < 0:
            return []

        params = {
            "days": day_filter,
            "about": about_filter,
            "f_days": bool(day_filter),
            "f_about": bool(about_filter),
            "f_window": lo >= 0,
            "lo": lo,
            "hi": hi,
        }
        where = """
            WHERE ($f_days = false OR f.dow IN $days)
              AND ($f_window = false OR (f.local_min >= $lo AND f.local_min <= $hi))
              AND ($f_about = false OR any(x IN $about WHERE x IN classes))
        """

        # Without an `about` class we cannot tell a relevant frame from an
        # unrelated one, so claim only day-level support — and all matching
        # days, not just those of the first few frames.
        if not about_filter:
            found = self.rows(
                f"""
                MATCH (f:Frame)
                OPTIONAL MATCH (f)-[:SAW]->(c:Class)
                WITH f, collect(c.name) AS classes
                {where}
                RETURN DISTINCT f.day_id AS day_id
                ORDER BY day_id
                """,
                params,
            )
            return [r["day_id"] for r in found if r.get("day_id")]

        found = self.rows(
            f"""
            MATCH (f:Frame)
            OPTIONAL MATCH (f)-[:SAW]->(c:Class)
            WITH f, collect(c.name) AS classes
            {where}
            RETURN f.frame_id AS frame_id, f.day_id AS day_id
            ORDER BY f.ts_ms
            LIMIT $limit
            """,
            {**params, "limit": int(limit)},
        )
        days_out = sorted({r["day_id"] for r in found if r.get("day_id")})
        return days_out + [r["frame_id"] for r in found if r.get("frame_id")]


def person_id(name: str) -> str:
    """Stable Person id from the name as heard: 'Marta' -> 'per_marta'."""
    return "per_" + (re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "unknown")


def _clean_extra(extra: dict[str, Any] | None) -> dict[str, Any]:
    # FalkorDB has no null properties; nested maps are not storable either
    return {k: v for k, v in (extra or {}).items() if v is not None and not isinstance(v, dict)}


def _clamp(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, f))


def connect(config: DreamerConfig):
    """Open the configured graph: a FalkorDB server if a URL is set, else embedded."""
    if config.falkordb_url:
        from falkordb import FalkorDB

        client = FalkorDB.from_url(config.falkordb_url, socket_timeout=30)
    else:
        from redislite.falkordb_client import FalkorDB as EmbeddedFalkorDB

        path = Path(config.embedded_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        client = EmbeddedFalkorDB(str(path))
    return client.select_graph(config.graph_name)


def open_graph(config: DreamerConfig) -> ProdGraph:
    return ProdGraph(connect(config), config)
