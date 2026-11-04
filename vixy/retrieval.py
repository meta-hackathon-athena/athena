"""Contextual memory retrieval over the unified graph (read-only, bounded, explainable).

Given *cues* (people as named, keywords, a time window), return the few records that
matter, each with the reasons it was picked and its provenance:

    LongTermMemory   consolidated claims and routines (Dreamer), ABOUT a Person
    Pattern          routines
    Commitment       the agent's plans, SUPPORTED_BY the exact Note
    Reminder         open reminder Notes
    Note             recent utterances that mention a cue (episodes)

No embeddings: graph structure (ABOUT -> Person, POSSIBLY_SAME_AS), structured fields
(subject / predicate / object / keywords) and plain word matches. Every query is a
fixed, parameterized Cypher run through GRAPH.RO_QUERY; nothing here accepts Cypher
from a caller or a model. Each query has a LIMIT, and the final list is capped, so a
caller (the agent, /ask, a model prompt) never gets "the whole graph".

Scores are sums of named reasons (entity +3, keyword +1 each up to 3, upcoming plan,
recency, confidence; contested / hypothesis / past plan penalties); the reasons are
returned with every item.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from prod_dreamer_model.semantic import NOT_A_NAME, stem

from .memory_api import SharedMemory

DAY_MS = 86_400_000
STOPWORDS = {
    "the", "a", "an", "to", "at", "on", "in", "my", "and", "of", "for", "some", "it", "me", "is", "are", "was",
    "were", "be", "been", "i", "you", "he", "she", "they", "we", "him", "her", "them", "us", "this", "that",
    "what", "when", "where", "who", "why", "how", "did", "do", "does", "with", "about", "from", "tell", "told",
    "say", "said", "remember", "know", "any", "there", "have", "has", "had", "will", "would", "can", "could",
    "just", "going", "get", "got", "im", "i'm", "it's", "its", "your", "our", "their", "his", "hers", "then",
    "so", "but", "or", "if", "not", "no", "yes", "okay", "ok", "really", "very", "like", "today", "yesterday",
    "tomorrow", "vixy", "veyra", "please", "also", "am", "now", "let", "lets", "let's", "go", "went", "want",
    "need", "think", "thing", "things", "something", "anything", "much", "more", "last", "next", "week", "day",
    "time", "morning", "evening", "afternoon", "night", "tonight",
}
WEEKDAY_WORDS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def keywords(text: str) -> list[str]:
    """Content words of ``text``, singularized, without stopwords (order kept, no duplicates)."""
    words = re.findall(r"[a-z][a-z'-]+", text.lower().replace("’", "'"))
    out = [stem(w.strip("'")) for w in words if w not in STOPWORDS and w not in WEEKDAY_WORDS and len(w) > 2]
    return list(dict.fromkeys(w for w in out if w not in STOPWORDS))


def names(text: str) -> list[str]:
    """Capitalized words that could be people's names (anywhere in the sentence)."""
    words = re.findall(r"\b([A-Z][a-z]{1,20})\b", text.replace("’", "'"))
    return list(dict.fromkeys(w for w in words if w not in NOT_A_NAME and not (w.endswith("ly") and len(w) > 4)))


@dataclass
class Cues:
    people: list[str] = field(default_factory=list)          # names as said ("Alex")
    keywords: list[str] = field(default_factory=list)        # stems ("peanut")
    since_ms: int | None = None                              # temporal filter (inclusive)
    until_ms: int | None = None
    now_ms: int | None = None                                # for "upcoming" vs "past" plans and recency

    @classmethod
    def from_text(cls, text: str, **kw: Any) -> "Cues":
        people = names(text)
        lowered = {p.lower() for p in people}
        return cls(people=people, keywords=[k for k in keywords(text) if k not in lowered], **kw)

    @property
    def empty(self) -> bool:
        return not self.people and not self.keywords


_MEMORY_RETURN = """
    m.id AS id, labels(m)[0] AS label, m.statement AS statement, m.kind AS kind, m.status AS status,
    m.confidence AS confidence, m.basis AS basis, m.subject AS subject, m.subject_id AS subject_id,
    m.predicate AS predicate, m.object AS object, m.object_text AS object_text, m.polarity AS polarity,
    m.category AS category, m.caution AS caution, m.epistemic_status AS epistemic_status,
    m.reported_by AS reported_by, m.speakers AS speakers, m.support_count AS support_count,
    m.first_stated_ms AS first_stated_ms, m.last_stated_ms AS last_stated_ms, m.valid_until_ms AS valid_until_ms,
    m.event_start_ms AS event_start_ms, m.event_has_time AS event_has_time, m.time_text AS time_text,
    m.contested AS contested, m.extracted_by AS extracted_by, m.keywords AS keywords, m.updated_ms AS updated_ms,
    m.place_type AS place_type, m.time_window AS time_window,
    [(m)-[:ABOUT]->(p:Person) | {id: p.id, name: p.name, status: p.status}] AS people,
    [(m)-[:ABOUT]->(t:Tag) | t.name] AS tags,
    [(m)-[:SUPPORTED_BY]->(e) | {id: e.id, label: labels(e)[0], source_event_id: e.source_event_id,
        first_event_id: e.first_event_id, ts_ms: coalesce(e.ts_ms, e.start_ms), speaker_role: e.speaker_role,
        speaker_id: e.speaker_id}] AS evidence,
    [(m)-[:CONFLICTS_WITH]-(o:LongTermMemory) | o.id] AS conflicts_with,
    [(m)-[:SUPERSEDES]->(o:LongTermMemory) | o.id] AS supersedes,
    [(o:LongTermMemory)-[:SUPERSEDES]->(m) | o.id] AS superseded_by
"""


class MemoryRetriever:
    def __init__(self, graph, *, per_query_limit: int = 40, max_evidence: int = 5):
        self.mem = SharedMemory(graph)
        self.per_query_limit = per_query_limit
        self.max_evidence = max_evidence
        self.queries = 0

    def _ro(self, cypher: str, params: dict | None = None) -> list[dict]:
        self.queries += 1
        return self.mem._ro(cypher, params)

    # ------------------------------------------------------------------ entities
    def resolve_people(self, people: list[str]) -> dict[str, list[dict]]:
        """name as said -> matching Person nodes (+ POSSIBLY_SAME_AS neighbours). Never merges."""
        if not people:
            return {}
        rows = self._ro("""
            MATCH (p:Person)
            WHERE toLower(p.name) IN $names OR toLower(coalesce(p.first_name, p.name)) IN $names
               OR any(a IN coalesce(p.aliases, []) WHERE a IN $names)
            RETURN p.id AS id, p.name AS name, coalesce(p.first_name, p.name) AS first_name,
                   coalesce(p.status, 'resolved') AS status,
                   [(p)-[:POSSIBLY_SAME_AS]-(q:Person) | q.id] AS possibly_same_as
            ORDER BY p.id LIMIT 50
        """, {"names": [n.lower() for n in people]})
        out: dict[str, list[dict]] = {}
        for n in people:
            out[n] = [r for r in rows if n.lower() in (r["name"].lower(), r["first_name"].lower())]
        return out

    # --------------------------------------------------------------------- main
    def retrieve(self, cues: Cues, *, statuses: tuple[str, ...] = ("active",), min_confidence: float = 0.0,
                 limit: int = 8, include: tuple[str, ...] = ("memory", "pattern", "commitment", "reminder", "note"),
                 exclude_event_ids: tuple[str, ...] = ()) -> dict[str, Any]:
        if cues.empty:
            return {"items": [], "people": {}, "ambiguous_people": [], "queries": 0, "candidates": 0}
        q0 = self.queries
        people = self.resolve_people(cues.people)
        pids = sorted({p["id"] for ps in people.values() for p in ps} |
                      {x for ps in people.values() for p in ps for x in p["possibly_same_as"]})
        ambiguous = sorted(n for n, ps in people.items()
                           if len(ps) > 1 or any(p["status"] != "resolved" for p in ps))
        kw = [k for k in cues.keywords if k]
        name_terms = [n.lower() for n in cues.people]
        lim = self.per_query_limit
        cands: dict[str, dict] = {}

        def add(rows: list[dict]) -> None:
            for r in rows:
                cands.setdefault(r["id"], r)

        if "memory" in include:
            if pids:
                add(self._ro(f"""
                    MATCH (m:LongTermMemory)-[:ABOUT]->(p:Person) WHERE p.id IN $pids AND m.status IN $st
                    WITH DISTINCT m RETURN {_MEMORY_RETURN} LIMIT $lim
                """, {"pids": pids, "st": list(statuses), "lim": lim}))
            if kw or name_terms:
                add(self._ro(f"""
                    MATCH (m:LongTermMemory) WHERE m.status IN $st
                      AND (any(k IN $kw WHERE k IN coalesce(m.keywords, []))
                           OR any(k IN $terms WHERE toLower(m.statement) CONTAINS k))
                    RETURN {_MEMORY_RETURN} LIMIT $lim
                """, {"st": list(statuses), "kw": kw, "terms": kw + name_terms, "lim": lim}))
        if "pattern" in include and kw:
            add([{**r, "label": "Pattern"} for r in self._ro("""
                MATCH (m:Pattern) WHERE m.status IN $st
                  AND (m.place_type IN $kw OR any(k IN $kw WHERE toLower(m.statement) CONTAINS k))
                RETURN m.id AS id, m.statement AS statement, m.kind AS kind, m.status AS status,
                       m.confidence AS confidence, m.time_window AS time_window, m.place_type AS place_type,
                       'inferred' AS epistemic_status,
                       [(m)-[:SUPPORTED_BY]->(e) | {id: e.id, label: labels(e)[0], first_event_id: e.first_event_id,
                           ts_ms: coalesce(e.ts_ms, e.start_ms)}] AS evidence
                LIMIT $lim
            """, {"st": list(statuses), "kw": kw, "lim": lim})])
        if "commitment" in include:
            add([{**r, "label": "Commitment", "epistemic_status": "operational"} for r in self._ro("""
                MATCH (c:Commitment) WHERE c.status <> 'cancelled'
                  AND (any(p IN coalesce(c.people, []) WHERE toLower(p) IN $names)
                       OR any(k IN $terms WHERE toLower(c.title) CONTAINS k))
                RETURN c.id AS id, c.title AS statement, c.title AS title, c.start_ms AS start_ms,
                       c.end_ms AS end_ms, c.status AS status, c.source AS source, c.people AS people_named,
                       [(c)-[:SUPPORTED_BY]->(n:Note) | {id: n.id, label: 'Note', source_event_id: n.source_event_id,
                           ts_ms: n.ts_ms, speaker_role: n.speaker_role}] AS evidence
                ORDER BY c.start_ms LIMIT $lim
            """, {"names": name_terms, "terms": kw + name_terms, "lim": lim})])
        if "reminder" in include and (kw or name_terms):
            add([{**r, "label": "Reminder", "epistemic_status": "self_reported"} for r in self._ro("""
                MATCH (n:Note {kind: 'reminder'}) WHERE n.status = 'open'
                  AND any(k IN $terms WHERE toLower(n.text) CONTAINS k)
                RETURN n.id AS id, n.text AS statement, n.status AS status, n.ts_ms AS ts_ms,
                       n.trigger_place_type AS trigger_place_type, n.source_event_id AS source_event_id
                LIMIT $lim
            """, {"terms": kw + name_terms, "lim": lim})])
        if "note" in include and (kw or name_terms):
            lo = cues.since_ms if cues.since_ms is not None else 0
            hi = cues.until_ms if cues.until_ms is not None else 2 ** 62
            add([{**r, "label": "Note", "epistemic_status": "observed"} for r in self._ro("""
                MATCH (n:Note) WHERE n.ts_ms >= $lo AND n.ts_ms <= $hi
                  AND any(k IN $terms WHERE toLower(n.text) CONTAINS k)
                RETURN n.id AS id, n.text AS statement, n.ts_ms AS ts_ms, n.timestamp AS timestamp,
                       n.speaker_id AS speaker_id, n.speaker_role AS speaker_role,
                       n.source_event_id AS source_event_id, n.payload_sha256 AS payload_sha256, n.kind AS kind
                ORDER BY n.ts_ms DESC LIMIT $lim
            """, {"lo": lo, "hi": hi, "terms": kw + name_terms, "lim": lim})])

        items = []
        for r in cands.values():
            evid = r.get("evidence") or []
            if exclude_event_ids and (r.get("source_event_id") in exclude_event_ids or
                                      any(e.get("source_event_id") in exclude_event_ids for e in evid)):
                continue                         # never "recall" the very utterance being handled
            if (r.get("confidence") is not None and r["confidence"] < min_confidence):
                continue
            if not self._in_window(r, cues):
                continue
            score, reasons = self._score(r, cues, pids)
            if not any(x.startswith(("person", "keyword", "named")) for x in reasons):
                continue                         # nothing in the cues points at it
            r["evidence"] = sorted(evid, key=lambda e: (e.get("ts_ms") or 0, e.get("id") or ""))[: self.max_evidence]
            r["score"], r["reasons"] = round(score, 3), reasons
            items.append(r)
        items.sort(key=lambda r: (-r["score"], r["id"]))
        return {"items": items[:limit], "people": people, "ambiguous_people": ambiguous,
                "queries": self.queries - q0, "candidates": len(cands)}

    @staticmethod
    def _in_window(r: dict, cues: Cues) -> bool:
        if cues.since_ms is None and cues.until_ms is None:
            return True
        lo, hi = cues.since_ms or 0, cues.until_ms or 2 ** 62
        times = [r.get(k) for k in ("ts_ms", "start_ms", "first_stated_ms", "last_stated_ms", "event_start_ms")]
        times += [e.get("ts_ms") for e in r.get("evidence") or []]
        return any(t is not None and lo <= t <= hi for t in times)

    @staticmethod
    def _score(r: dict, cues: Cues, pids: list[str]) -> tuple[float, list[str]]:
        score, reasons = 0.0, []
        about = {p["id"] for p in r.get("people") or []}
        if about & set(pids):
            score += 3.0
            reasons.append("person:" + ",".join(sorted(about & set(pids))))
        named = [n for n in cues.people if n.lower() in [str(x).lower() for x in r.get("people_named") or []]
                 or n.lower() in str(r.get("statement") or "").lower()]
        if named and not about & set(pids):
            score += 2.0
            reasons.append("named:" + ",".join(named))
        hay = set(r.get("keywords") or []) | set(keywords(str(r.get("statement") or "")))
        hay |= {stem(str(r.get("object") or ""))} if r.get("object") else set()
        hit = [k for k in cues.keywords if k in hay]
        if hit:
            score += min(3.0, float(len(hit)))
            reasons.append("keyword:" + ",".join(hit))
        if r.get("caution") and hit:
            score += 0.5
            reasons.append("caution")
        conf = r.get("confidence")
        if isinstance(conf, (int, float)):
            score += float(conf)
        now = cues.now_ms
        ev = r.get("event_start_ms") or r.get("start_ms")
        if now and ev:
            if ev >= now:
                if ev - now <= 7 * DAY_MS:
                    score += 0.5
                    reasons.append("upcoming")
            else:
                score -= 1.0
                reasons.append("past_event")
        seen = r.get("last_stated_ms") or r.get("ts_ms")
        if now and seen and seen <= now:
            score += 0.5 * math.exp(-(now - seen) / (14 * DAY_MS))
        if r.get("contested"):
            score -= 0.3
            reasons.append("contested")
        if r.get("epistemic_status") == "hypothesis":
            score -= 0.3
            reasons.append("hypothesis")
        return score, reasons
