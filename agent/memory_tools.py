"""The Active Agent's only door into FalkorDB.

Reads are small, targeted queries (one day of commitments, reminders for one place
type), never the whole graph. Writes are a fixed allow-list of operations with
validated arguments, MERGE-based ids (safe to retry) and an audit trail. No caller,
and certainly no LLM, can run arbitrary Cypher through this class.

Adds three labels next to the memory schema (see memory/schema.cypher, section 4):
  (:Commitment)  something the user plans to do at a time
  (:Question)    a clarification the agent (or the Dreamer) wants to ask
  (:AgentLog)    one row per non-trivial agent decision, for audit and for the Dreamer
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta

from memory.store import TZ, MemoryStore

AGENT_INDEXES = [
    ("Commitment", "id"), ("Commitment", "start_ms"),
    ("Question", "id"), ("Question", "status"),
    ("AgentLog", "id"), ("AgentLog", "dedup_key"),
]
COMMITMENT_STATUS = {"tentative", "confirmed", "cancelled"}
COMMITMENT_SOURCE = {"user_speech", "user_command", "user_answer", "app"}
QUESTION_KINDS = {"confirm_commitment", "clarify_time", "confirm_correction", "complete_reminder", "dreamer"}


class MemoryUnavailable(RuntimeError):
    """FalkorDB could not be reached or a query failed. Callers must not guess."""


class InvalidWrite(ValueError):
    pass


def commitment_id(title: str, start_ms: int) -> str:
    return "cmt_" + hashlib.sha1(f"{title.lower().strip()}:{start_ms}".encode()).hexdigest()[:12]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}       # FalkorDB has no null properties


class AgentMemory:
    def __init__(self, store: MemoryStore):
        self.store = store
        self.queries = 0
        self.query_ms: list[float] = []

    def setup(self) -> None:
        self.store.setup()
        for label, prop in AGENT_INDEXES:
            try:
                self.store.g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except Exception as e:
                if "already indexed" not in str(e):
                    raise

    def q(self, cypher: str, params: dict | None = None) -> list[list]:
        t0 = time.perf_counter()
        try:
            return self.store.q(cypher, params)
        except Exception as e:
            raise MemoryUnavailable(str(e)) from e
        finally:
            self.queries += 1
            self.query_ms.append((time.perf_counter() - t0) * 1000)

    # ================================================================== reads
    _CMT = ["id", "title", "start_ms", "end_ms", "status", "source", "people", "place_text", "time_ambiguous"]
    _CMT_RET = "c.id, c.title, c.start_ms, c.end_ms, c.status, c.source, c.people, c.place_text, c.time_ambiguous"

    def _rows(self, keys, rows):
        return [{k: v for k, v in zip(keys, r) if v is not None} for r in rows]

    def commitments_between(self, start_ms: int, end_ms: int) -> list[dict]:
        """Live commitments that start in [start_ms - 12h, end_ms): enough to catch
        anything that could overlap the window, nothing more."""
        rows = self.q(f"""
            MATCH (c:Commitment) WHERE c.start_ms >= $lo AND c.start_ms < $hi AND c.status <> 'cancelled'
            RETURN {self._CMT_RET} ORDER BY c.start_ms
        """, {"lo": start_ms - 12 * 3_600_000, "hi": end_ms})
        return self._rows(self._CMT, rows)

    def commitments_on(self, day) -> list[dict]:
        start = datetime.combine(day, datetime.min.time(), TZ)
        return self.commitments_between(int(start.timestamp() * 1000),
                                        int((start + timedelta(days=1)).timestamp() * 1000))

    def get_commitment(self, cid: str) -> dict | None:
        rows = self.q(f"MATCH (c:Commitment {{id: $id}}) RETURN {self._CMT_RET}", {"id": cid})
        return self._rows(self._CMT, rows)[0] if rows else None

    def upcoming_commitments(self, now_ms: int, horizon_ms: int) -> list[dict]:
        rows = self.q(f"""
            MATCH (c:Commitment) WHERE c.start_ms > $now AND c.start_ms <= $hi AND c.status <> 'cancelled'
            RETURN {self._CMT_RET} ORDER BY c.start_ms
        """, {"now": now_ms, "hi": now_ms + horizon_ms})
        return self._rows(self._CMT, rows)

    def open_reminders(self, place_type: str | None = None) -> list[dict]:
        rows = self.q("""
            MATCH (n:Note {kind: 'reminder', status: 'open'})
            WHERE $pt IS NULL OR n.trigger_place_type = $pt
            RETURN n.id, n.text, n.ts_ms, n.trigger_place_type ORDER BY n.ts_ms
        """, {"pt": place_type})
        return self._rows(["id", "text", "ts_ms", "trigger_place_type"], rows)

    def place_near(self, lat: float, lon: float, radius_m: float = 75) -> dict | None:
        rows = self.q("""
            MATCH (p:Place) WHERE distance(p.geo, point({latitude: $lat, longitude: $lon})) < $r
            RETURN p.id, p.name, p.place_type, p.sensitive,
                   distance(p.geo, point({latitude: $lat, longitude: $lon})) AS d
            ORDER BY d LIMIT 1
        """, {"lat": lat, "lon": lon, "r": radius_m})
        return self._rows(["id", "name", "place_type", "sensitive", "distance_m"], rows)[0] if rows else None

    def preference_statements(self) -> list[str]:
        rows = self.q("MATCH (f:Fact {kind: 'preference', status: 'active'}) RETURN f.statement LIMIT 50")
        return [r[0] for r in rows if r[0]]

    _Q = ["id", "kind", "text", "status", "source", "priority", "created_ms", "not_before_ms",
          "expires_ms", "asked_ms", "payload", "dedup_key", "about"]
    _Q_RET = ("q.id, q.kind, q.text, q.status, q.source, q.priority, q.created_ms, q.not_before_ms, "
              "q.expires_ms, q.asked_ms, q.payload, q.dedup_key, q.about")

    def _questions(self, rows) -> list[dict]:
        out = self._rows(self._Q, rows)
        for q in out:
            q["payload"] = json.loads(q.get("payload") or "{}")
        return out

    def get_question(self, qid: str) -> dict | None:
        rows = self.q(f"MATCH (q:Question {{id: $id}}) RETURN {self._Q_RET}", {"id": qid})
        return self._questions(rows)[0] if rows else None

    def outstanding_question(self, now_ms: int) -> dict | None:
        """The question the user was last asked and hasn't answered (if still valid)."""
        rows = self.q(f"""
            MATCH (q:Question {{status: 'asked'}}) WHERE q.expires_ms > $now
            RETURN {self._Q_RET} ORDER BY q.asked_ms DESC LIMIT 1
        """, {"now": now_ms})
        return self._questions(rows)[0] if rows else None

    def due_questions(self, now_ms: int, limit: int = 3) -> list[dict]:
        """Pending questions (agent's or the Dreamer's) that may be asked now."""
        rows = self.q(f"""
            MATCH (q:Question {{status: 'pending'}})
            WHERE q.expires_ms > $now AND coalesce(q.not_before_ms, 0) <= $now
            RETURN {self._Q_RET} ORDER BY q.created_ms LIMIT $n
        """, {"now": now_ms, "n": limit})
        return self._questions(rows)

    def latest_log_ms(self) -> int | None:
        rows = self.q("MATCH (l:AgentLog) RETURN max(l.ts_ms)")
        return rows[0][0] if rows and rows[0][0] is not None else None

    # --- consolidated memory (written by the Dreamer; read-only here) ------------
    def long_term_memories(self, about: str | None = None, limit: int = 20) -> list[dict]:
        """Active LongTermMemory nodes, optionally about a person / tag / word."""
        return self._shared().long_term_memories(about=about, limit=limit)

    def memory_evidence(self, memory_id: str) -> list[dict]:
        """The observed records (Notes/Visits) a memory is SUPPORTED_BY."""
        return self._shared().evidence(memory_id)

    def _shared(self):
        from vixy.memory_api import SharedMemory         # same graph, read-only queries
        return SharedMemory(_Counted(self))

    def retrieve(self, cues, **kw) -> dict:
        """Bounded contextual retrieval (vixy.retrieval) on the same graph, read-only."""
        from vixy.retrieval import MemoryRetriever
        return MemoryRetriever(_Counted(self)).retrieve(cues, **kw)

    def recall_index(self) -> tuple[set[str], set[str]]:
        """(lower-case names of known people, keywords of the wearer's own caution memories):
        the cheap gate that decides whether an utterance is worth a retrieval at all."""
        names = {str(n).lower() for r in self.q("MATCH (p:Person) RETURN p.name, p.first_name LIMIT 2000")
                 for n in r if n}
        own = {str(k) for r in self.q("""
            MATCH (m:LongTermMemory {subject_id: 'user', status: 'active'}) WHERE m.caution = true
            RETURN m.keywords LIMIT 200""") for k in (r[0] or [])}
        return names, own

    def recent_dedup_keys(self, since_ms: int) -> dict[str, int]:
        rows = self.q("""
            MATCH (l:AgentLog) WHERE l.ts_ms >= $since AND l.status = 'done' AND l.dedup_key IS NOT NULL
            RETURN l.dedup_key, max(l.ts_ms)
        """, {"since": since_ms})
        return {k: ts for k, ts in rows}

    # ============================================================ safe writes
    def record_commitment(self, *, title: str, start_ms: int, end_ms: int | None, status: str,
                          source: str, people: list[str] | None = None, place_text: str | None = None,
                          time_ambiguous: bool = False, evidence_segment: str | None = None,
                          evidence_text: str | None = None, evidence_ts_ms: int | None = None,
                          now_ms: int | None = None) -> str:
        """Create or update one commitment. Idempotent: same title + start -> same node.
        Never downgrades a confirmed commitment to tentative."""
        if status not in COMMITMENT_STATUS - {"cancelled"} or source not in COMMITMENT_SOURCE:
            raise InvalidWrite(f"bad status/source {status}/{source}")
        if not title or len(title) > 80 or not isinstance(start_ms, int):
            raise InvalidWrite("bad title/start")
        if end_ms is not None and end_ms <= start_ms:
            raise InvalidWrite("end before start")
        now_ms = now_ms or _now_ms()
        cid = commitment_id(title, start_ms)
        local = datetime.fromtimestamp(start_ms / 1000, TZ)
        props = _clean({"title": title, "start_ms": start_ms, "end_ms": end_ms, "source": source,
                        "local_date": local.date().isoformat(), "people": people or [],
                        "place_text": place_text, "time_ambiguous": time_ambiguous,
                        "evidence_segment": evidence_segment, "updated_ms": now_ms})
        self.q("""
            MERGE (c:Commitment {id: $id})
            ON CREATE SET c.created_ms = $now, c.status = $status
            SET c += $props
            WITH c WHERE $status = 'confirmed' SET c.status = 'confirmed'
        """, {"id": cid, "props": props, "status": status, "now": now_ms})
        if evidence_segment or evidence_text:
            # Exact provenance: the Note the Episode Writer stored for this very event
            # (source_event_id). Legacy notes without an event id (MemoryStore.add_note
            # from the simulator) are matched on the same transcript text around the same
            # time; that rule never applies to notes that carry a source_event_id.
            self.q("""
                MATCH (c:Commitment {id: $id})
                MATCH (n:Note)
                WHERE ($seg IS NOT NULL AND n.source_event_id = $seg)
                   OR (n.source_event_id IS NULL AND $text IS NOT NULL AND n.text = $text
                       AND n.ts_ms >= $lo AND n.ts_ms <= $hi)
                MERGE (c)-[:SUPPORTED_BY]->(n)
            """, {"id": cid, "seg": evidence_segment, "text": evidence_text,
                  "lo": (evidence_ts_ms or now_ms) - 120_000, "hi": (evidence_ts_ms or now_ms) + 120_000})
        return cid

    def update_commitment_time(self, cid: str, start_ms: int, end_ms: int | None, now_ms: int) -> None:
        """User-confirmed time change. The previous time is kept on the node (reversible)."""
        if end_ms is not None and end_ms <= start_ms:
            raise InvalidWrite("end before start")
        old = self.get_commitment(cid)
        if not old:
            raise InvalidWrite(f"no commitment {cid}")
        if end_ms is None and old.get("end_ms"):
            end_ms = start_ms + (old["end_ms"] - old["start_ms"])      # moved, same length
        props = _clean({"prev_start_ms": old["start_ms"], "prev_end_ms": old.get("end_ms"),
                        "start_ms": start_ms, "end_ms": end_ms, "updated_ms": now_ms, "status": "confirmed",
                        "local_date": datetime.fromtimestamp(start_ms / 1000, TZ).date().isoformat()})
        self.q("MATCH (c:Commitment {id: $id}) SET c += $p", {"id": cid, "p": props})

    def complete_reminder(self, note_id: str, now_ms: int) -> None:
        found = self.q("""
            MATCH (n:Note {id: $id, kind: 'reminder'}) SET n.status = 'done', n.done_ms = $now RETURN n.id
        """, {"id": note_id, "now": now_ms})
        if not found:
            raise InvalidWrite(f"no reminder {note_id}")

    def save_question(self, *, kind: str, text: str, dedup_key: str, now_ms: int, expires_ms: int,
                      priority: str = "low", payload: dict | None = None, about: str | None = None,
                      not_before_ms: int | None = None, source: str = "agent") -> tuple[str, bool]:
        """Returns (question_id, created). Same dedup_key -> same question, never asked twice."""
        if kind not in QUESTION_KINDS or priority not in ("low", "normal", "high"):
            raise InvalidWrite(f"bad question kind/priority {kind}/{priority}")
        qid = "qst_" + hashlib.sha1(dedup_key.encode()).hexdigest()[:12]
        if self.q("MATCH (q:Question {id: $id}) RETURN q.id", {"id": qid}):
            return qid, False
        self.q("""
            MERGE (q:Question {id: $id})
            ON CREATE SET q += $props, q.status = 'pending', q.created_ms = $now
        """, {"id": qid, "now": now_ms, "props": _clean({
            "kind": kind, "text": text[:300], "dedup_key": dedup_key, "expires_ms": expires_ms,
            "priority": priority, "payload": json.dumps(payload or {}), "about": about,
            "not_before_ms": not_before_ms, "source": source})})
        if about:
            self.q("""
                MATCH (q:Question {id: $id}) MATCH (x) WHERE (x:Commitment OR x:Note) AND x.id = $about
                MERGE (q)-[:ABOUT]->(x)
            """, {"id": qid, "about": about})
        return qid, True

    def mark_question(self, qid: str, status: str, now_ms: int, answer: str | None = None) -> None:
        if status not in ("asked", "answered", "expired", "pending"):
            raise InvalidWrite(f"bad question status {status}")
        props = {"status": status}
        if status == "asked":
            props["asked_ms"] = now_ms
        if status == "answered":
            props |= {"answered_ms": now_ms, "answer": answer or ""}
        self.q("MATCH (q:Question {id: $id}) SET q += $p", {"id": qid, "p": props})

    def expire_questions(self, now_ms: int) -> int:
        return self.q("""
            MATCH (q:Question) WHERE q.status IN ['pending', 'asked'] AND q.expires_ms <= $now
            SET q.status = 'expired' RETURN count(q)
        """, {"now": now_ms})[0][0]

    def log(self, d) -> None:
        """Audit trail. Stores the decision, never the raw transcript."""
        self.q("""
            MERGE (l:AgentLog {id: $id}) SET l += $props
            WITH l UNWIND $related AS rid
            MATCH (x) WHERE (x:Commitment OR x:Note OR x:Question OR x:Place OR x:Fact OR x:Visit
                             OR x:LongTermMemory OR x:Pattern) AND x.id = rid
            MERGE (l)-[:CONCERNS]->(x)
        """, {"id": d.decision_id, "related": d.related_memory_ids[:10], "props": _clean({
            "ts_ms": d.ts_ms, "action": d.action.value, "priority": d.priority.value,
            "status": d.status, "trigger": d.trigger, "reason": d.reason[:300],
            "message": d.message, "dedup_key": d.deduplication_key,
            "trigger_event_id": getattr(d, "trigger_event_id", None),
            "related": d.related_memory_ids[:10], "confidence": d.confidence,
            "reasoning": getattr(d, "reasoning", None),
            "supporting_memory_ids": [m["id"] for m in getattr(d, "supporting_memories", [])][:5] or None,
            # FalkorDB stores no nested maps: the explanation is kept as JSON text
            "supporting_json": json.dumps(getattr(d, "supporting_memories", [])[:5]) if getattr(
                d, "supporting_memories", None) else None})})

    # Operations a MEMORY_UPDATE decision may name. Anything else is rejected.
    def apply(self, op: str, args: dict) -> str | None:
        ops = {"record_commitment": self.record_commitment,
               "update_commitment_time": self.update_commitment_time,
               "complete_reminder": self.complete_reminder}
        if op not in ops:
            raise InvalidWrite(f"operation not allowed: {op}")
        return ops[op](**args)


class _Counted:
    """Gives SharedMemory the agent's graph, with the agent's query accounting and
    MemoryUnavailable semantics (a read failure is never silently empty)."""

    def __init__(self, mem: AgentMemory):
        self.mem, self.g = mem, mem.store.g

    def ro_query(self, cypher: str, params: dict | None = None, **_):
        t0 = time.perf_counter()
        try:
            ro = getattr(self.g, "ro_query", None)
            return ro(cypher, params or {}) if callable(ro) else self.g.query(cypher, params or {})
        except Exception as e:
            raise MemoryUnavailable(str(e)) from e
        finally:
            self.mem.queries += 1
            self.mem.query_ms.append((time.perf_counter() - t0) * 1000)
