"""Memory graph in FalkorDB (one graph per user).

EPISODIC layer: what happened, when.
  (:Visit)   a stay or transit built from frames
  (:Note)    something the user said (audio transcript) or typed
  (Visit)-[:NEXT]->(Visit), (Note)-[:DURING]->(Visit)

SEMANTIC layer: what we believe, timeless-ish.
  (:Place)   a location (needs GPS)
  (:Tag)     a concept the camera recognises ("laptop", "food court")
  (:Fact)    a belief: frequent place, usual activity, stated preference

Bridges: (Visit)-[:AT]->(Place), (Visit)-[:SAW]->(Tag),
         (Fact)-[:ABOUT]->(Place|Tag), (Fact)-[:SUPPORTED_BY]->(Visit|Note)

Every Fact points at the episodes behind it, so "why do you think that?"
is a graph query. Facts are never invented from a single observation.

FalkorDB notes: no null properties (we omit them), times are epoch-ms ints
(temporal types aren't range-indexable), each logical write is kept to as
few single-query (atomic) statements as possible, and writes are MERGE-based
so re-ingesting the same frames is idempotent.
"""

from __future__ import annotations

import hashlib
import os
import re
import statistics
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .rules import PLACE_CUES, SENSITIVE_PLACE_TYPES
from .segmenter import Visit

TZ = ZoneInfo(os.getenv("VIXY_TZ") or os.getenv("VEYRA_TZ") or "Asia/Singapore")
DAY_MS = 86_400_000

INDEXES = [
    ("Visit", "id"), ("Visit", "start_ms"), ("Visit", "local_date"),
    ("Note", "id"), ("Note", "ts_ms"),
    ("Place", "id"), ("Place", "geo"),
    ("Tag", "name"),
    ("Fact", "id"), ("Fact", "status"),
]

# Facts need this much independent evidence (distinct days, not frames).
MIN_DAYS_FREQUENT = 3
WINDOW_DAYS = 28
PLACE_MATCH_RADIUS_M = 75


def _local(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, TZ)


def _now_ms() -> int:
    return int(time.time() * 1000)


class MemoryStore:
    def __init__(self, graph):
        self.g = graph

    # ------------------------------------------------------------------ setup
    def setup(self) -> None:
        for label, prop in INDEXES:
            try:
                self.g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except Exception as e:  # already indexed
                if "already indexed" not in str(e):
                    raise

    def q(self, cypher: str, params: dict | None = None) -> list[list]:
        return self.g.query(cypher, params or {}).result_set

    # --------------------------------------------------------- episodic write
    def add_visit(self, v: Visit) -> str:
        """Store one visit, link it to a Place (if GPS) and to the previous visit."""
        local = _local(v.start_ms)
        sensitive = v.place_type in SENSITIVE_PLACE_TYPES
        props = {
            "kind": v.kind, "start_ms": v.start_ms, "end_ms": v.end_ms,
            "local_date": local.date().isoformat(), "dow": local.weekday(),
            "start_min": local.hour * 60 + local.minute,
            "frames": v.frames, "place_type": v.place_type, "activity": v.activity,
            "tags": sorted(v.tags, key=v.tags.get, reverse=True), "sensitive": sensitive,
        }
        if v.lat is not None:
            props |= {"lat": v.lat, "lon": v.lon}
        if "person" in v.tag_max_count:      # anonymous headcount only, never identities
            props["people_max"] = v.tag_max_count["person"]
        tags = [{"name": t, "ratio": round(r, 3), "conf": round(v.tag_conf.get(t, 1.0), 3),
                 "max_count": v.tag_max_count.get(t, 0)} for t, r in v.tags.items()]
        self.q("""
            MERGE (v:Visit {id: $id}) SET v += $props
            WITH v UNWIND $tags AS t
            MERGE (g:Tag {name: t.name})
            MERGE (v)-[s:SAW]->(g) SET s.ratio = t.ratio, s.conf = t.conf, s.max_count = t.max_count
        """, {"id": v.id, "props": props, "tags": tags})

        if v.lat is not None and v.kind == "stay":
            place_id = self._resolve_place(v)
            self.q("""
                MATCH (v:Visit {id: $vid}), (p:Place {id: $pid})
                MERGE (v)-[:AT]->(p)
                WITH p MATCH (p)<-[:AT]-(x:Visit)
                WITH p, avg(x.lat) AS la, avg(x.lon) AS lo, count(x) AS n
                SET p.lat = la, p.lon = lo, p.geo = point({latitude: la, longitude: lo}), p.visits = n
            """, {"vid": v.id, "pid": place_id})

        self.q("""
            MATCH (v:Visit {id: $id})
            MATCH (prev:Visit) WHERE prev.end_ms <= v.start_ms AND prev.end_ms > v.start_ms - 10800000
            WITH v, prev ORDER BY prev.end_ms DESC LIMIT 1
            MERGE (prev)-[n:NEXT]->(v) SET n.gap_min = (v.start_ms - prev.end_ms) / 60000
        """, {"id": v.id})
        return v.id

    def _resolve_place(self, v: Visit) -> str:
        """Match a nearby existing Place, or create one. Never matches on name alone."""
        existing = self.q("MATCH (:Visit {id: $id})-[:AT]->(p:Place) RETURN p.id", {"id": v.id})
        if existing:
            return existing[0][0]
        rows = self.q("""
            MATCH (p:Place)
            WHERE distance(p.geo, point({latitude: $lat, longitude: $lon})) < $r
            RETURN p.id, p.place_type, distance(p.geo, point({latitude: $lat, longitude: $lon})) AS d
            ORDER BY d
        """, {"lat": v.lat, "lon": v.lon, "r": PLACE_MATCH_RADIUS_M})
        for pid, ptype, _ in rows:
            # a café next to a food court is a different place, even if close
            if ptype == v.place_type or "unknown" in (ptype, v.place_type):
                if ptype == "unknown" and v.place_type != "unknown":
                    self.q("MATCH (p:Place {id: $id}) SET p.place_type = $t, p.sensitive = $s",
                           {"id": pid, "t": v.place_type, "s": v.place_type in SENSITIVE_PLACE_TYPES})
                return pid
        pid = f"plc_{uuid.uuid4().hex[:12]}"
        self.q("""
            CREATE (p:Place {id: $id, place_type: $t, sensitive: $s, lat: $lat, lon: $lon,
                             geo: point({latitude: $lat, longitude: $lon}), visits: 0, created_ms: $now})
        """, {"id": pid, "t": v.place_type, "s": v.place_type in SENSITIVE_PLACE_TYPES,
              "lat": v.lat, "lon": v.lon, "now": _now_ms()})
        return pid

    def add_note(self, text: str, ts_ms: int | None = None, source: str = "voice", *,
                 note_id: str | None = None, attribute_to_user: bool = True,
                 props: dict | None = None) -> dict:
        """Something the user said. 'remind me ...' becomes an open reminder;
        'I like/love/prefer/hate ...' becomes a user-stated preference Fact.

        note_id: a stable id (the Episode Writer derives it from the context-engine
        event_id, so a replayed event updates the same node). attribute_to_user=False
        (someone else, or an unknown speaker, said it) stores a plain note: a reminder
        or preference is never assigned to the user on someone else's words; the
        cue is kept as intent_hint. props: extra provenance properties (no None values).
        A re-write never reopens a reminder that was already completed."""
        ts_ms = ts_ms or _now_ms()
        text = text.strip()
        nid = note_id or "note_" + hashlib.sha1(f"{ts_ms}:{text}".encode()).hexdigest()[:12]
        low = text.lower()
        props = {**{k: v for k, v in (props or {}).items() if v is not None},
                 "text": text, "ts_ms": ts_ms, "local_date": _local(ts_ms).date().isoformat(),
                 "source": source, "kind": "note"}
        status = None
        if low.startswith(("remind me", "don't forget", "dont forget")):
            if attribute_to_user:
                props["kind"], status = "reminder", "open"
                trigger = _place_type_in_text(low)
                if trigger:
                    props["trigger_place_type"] = trigger
            else:
                props["intent_hint"] = "reminder"
        self.q("""
            MERGE (n:Note {id: $id}) SET n += $props
            WITH n WHERE $status IS NOT NULL AND n.status IS NULL SET n.status = $status
        """, {"id": nid, "props": props, "status": status})
        if status:
            props["status"] = status
        # attach to the visit happening at that time (if any)
        self.q("""
            MATCH (n:Note {id: $id})
            MATCH (v:Visit) WHERE v.start_ms <= $ts AND v.end_ms + 600000 >= $ts
            WITH n, v ORDER BY v.start_ms DESC LIMIT 1
            MERGE (n)-[:DURING]->(v)
        """, {"id": nid, "ts": ts_ms})

        m = re.match(r"^i (really )?(like|love|prefer|enjoy|hate|dislike|don't like|do not like) (.+)$", low)
        if m and not attribute_to_user:
            self.q("MATCH (n:Note {id: $id}) SET n.intent_hint = 'preference'", {"id": nid})
            m = None
        if m:
            positive = m.group(2) in ("like", "love", "prefer", "enjoy")
            fid = "fact_pref_" + hashlib.sha1(m.group(3).encode()).hexdigest()[:10]
            self.q("""
                MATCH (n:Note {id: $nid})
                MERGE (f:Fact {id: $fid})
                SET f.kind = 'preference', f.source = 'user', f.status = 'active',
                    f.statement = $stmt, f.positive = $pos, f.confidence = 0.98,
                    f.updated_ms = $ts
                MERGE (f)-[:SUPPORTED_BY]->(n)
            """, {"nid": nid, "fid": fid, "stmt": text, "pos": positive, "ts": ts_ms})
        return {"id": nid, **props}

    def complete_reminder(self, note_id: str) -> None:
        self.q("MATCH (n:Note {id: $id, kind: 'reminder'}) SET n.status = 'done'", {"id": note_id})

    def name_place(self, place_id: str, name: str) -> None:
        """User-provided names win: the camera can't read most place names reliably."""
        self.q("MATCH (p:Place {id: $id}) SET p.name = $name, p.name_source = 'user'",
               {"id": place_id, "name": name})

    def forget_visit(self, visit_id: str) -> None:
        """Real deletion (not hiding): visit, its tag links; then facts are recomputed."""
        self.q("MATCH (v:Visit {id: $id}) DELETE v", {"id": visit_id})
        self.q("MATCH (p:Place) WHERE NOT (p)<-[:AT]-(:Visit) DELETE p")
        self.consolidate()

    # ------------------------------------------- episodic -> semantic (nightly)
    def consolidate(self, now_ms: int | None = None) -> dict:
        """Turn repeated episodes into Facts. Evidence = distinct days, not frames.
        Facts that lose support become 'inactive' (kept for history), not deleted."""
        now_ms = now_ms or _now_ms()
        since = now_ms - WINDOW_DAYS * DAY_MS
        rows = self.q("""
            MATCH (v:Visit)-[:AT]->(p:Place)
            WHERE v.start_ms >= $since AND p.sensitive = false
            RETURN p.id, p.place_type, collect(v.id), collect(v.local_date),
                   collect(v.start_min), collect(v.end_ms), collect(v.dow), collect(v.activity)
        """, {"since": since})

        active_ids: list[str] = []
        for pid, ptype, vids, dates, mins, ends, dows, acts in rows:
            days = sorted(set(dates))
            if len(days) < MIN_DAYS_FREQUENT:
                continue
            # one representative visit per day keeps evidence independent
            per_day: dict[str, str] = {}
            for vid, d in zip(vids, dates):
                per_day.setdefault(d, vid)
            support = list(per_day.values())
            day_list = sorted(set(dows))
            fid = f"fact_freq_{pid}"
            conf = round(len(days) / (len(days) + 2), 3)   # simple, explainable: n/(n+2)
            self._upsert_fact(fid, pid, support, {
                "kind": "frequent_place", "source": "inferred", "status": "active",
                "support_days": len(days), "first_date": days[0], "last_date": days[-1],
                "usual_arrival_min": int(statistics.median(mins)),
                "dows": day_list, "confidence": conf, "updated_ms": now_ms,
            })
            active_ids.append(fid)

            # usual activity at this place (needs >= 60% of days to agree)
            act_days: dict[str, set] = {}
            for a, d in zip(acts, dates):
                if a != "unknown":
                    act_days.setdefault(a, set()).add(d)
            if act_days:
                act, adays = max(act_days.items(), key=lambda kv: len(kv[1]))
                if len(adays) >= MIN_DAYS_FREQUENT and len(adays) / len(days) >= 0.6:
                    afid = f"fact_act_{pid}"
                    self._upsert_fact(afid, pid, [per_day[d] for d in sorted(adays)], {
                        "kind": "usual_activity", "activity": act, "source": "inferred",
                        "status": "active", "support_days": len(adays),
                        "confidence": round(len(adays) / len(days), 3), "updated_ms": now_ms,
                    })
                    active_ids.append(afid)

        retired = self.q("""
            MATCH (f:Fact) WHERE f.source = 'inferred' AND f.status = 'active' AND NOT f.id IN $ids
            SET f.status = 'inactive', f.updated_ms = $now
            RETURN count(f)
        """, {"ids": active_ids, "now": now_ms})[0][0]
        return {"active_inferred_facts": len(active_ids), "retired": retired}

    def _upsert_fact(self, fid: str, about_id: str, support_visit_ids: list[str], props: dict) -> None:
        self.q("""
            MERGE (f:Fact {id: $fid}) SET f += $props
            WITH f MATCH (p:Place {id: $about}) MERGE (f)-[:ABOUT]->(p)
            WITH f OPTIONAL MATCH (f)-[old:SUPPORTED_BY]->(:Visit) DELETE old
        """, {"fid": fid, "props": props, "about": about_id})
        self.q("""
            MATCH (f:Fact {id: $fid})
            UNWIND $vids AS vid MATCH (v:Visit {id: vid}) MERGE (f)-[:SUPPORTED_BY]->(v)
        """, {"fid": fid, "vids": support_visit_ids[-30:]})

    # ------------------------------------------------------------------ reads
    def day(self, date: str) -> list[dict]:
        """'Where did I go on <date>?' (YYYY-MM-DD, local time)."""
        rows = self.q("""
            MATCH (v:Visit {local_date: $d})
            OPTIONAL MATCH (v)-[:AT]->(p:Place)
            RETURN v.id, v.kind, v.start_ms, v.end_ms, v.place_type, v.activity, v.tags[0..6],
                   p.id, p.name
            ORDER BY v.start_ms
        """, {"d": date})
        return [dict(zip(["visit", "kind", "start_ms", "end_ms", "place_type", "activity",
                          "tags", "place_id", "place_name"], r)) for r in rows]

    def last_seen(self, tag: str) -> dict | None:
        """'When did I last see a laptop?'"""
        rows = self.q("""
            MATCH (v:Visit)-[s:SAW]->(:Tag {name: $t})
            OPTIONAL MATCH (v)-[:AT]->(p:Place)
            RETURN v.id, v.start_ms, v.place_type, p.id, p.name, s.ratio
            ORDER BY v.start_ms DESC LIMIT 1
        """, {"t": tag.lower()})
        if not rows:
            return None
        return dict(zip(["visit", "start_ms", "place_type", "place_id", "place_name", "ratio"], rows[0]))

    def place_history(self, place_id: str) -> dict:
        """'Have I been here before? When last?'"""
        r = self.q("""
            MATCH (p:Place {id: $id})<-[:AT]-(v:Visit)
            RETURN p.name, p.place_type, count(v), min(v.start_ms), max(v.start_ms),
                   collect(DISTINCT v.local_date)
        """, {"id": place_id})
        if not r:
            return {"place_id": place_id, "visits": 0}
        name, ptype, n, first, last, days = r[0]
        return {"place_id": place_id, "name": name, "place_type": ptype, "visits": n,
                "distinct_days": len(days), "first_ms": first, "last_ms": last}

    def facts(self, include_inactive: bool = False) -> list[dict]:
        rows = self.q("""
            MATCH (f:Fact) WHERE $all OR f.status = 'active'
            OPTIONAL MATCH (f)-[:ABOUT]->(p:Place)
            RETURN f.id, f.kind, f.source, f.status, f.confidence, f.support_days,
                   f.statement, f.activity, f.usual_arrival_min, p.id, p.name, p.place_type
            ORDER BY f.confidence DESC
        """, {"all": include_inactive})
        keys = ["id", "kind", "source", "status", "confidence", "support_days", "statement",
                "activity", "usual_arrival_min", "place_id", "place_name", "place_type"]
        return [{k: v for k, v in zip(keys, r) if v is not None} for r in rows]

    def explain(self, fact_id: str) -> dict:
        """'Why do you believe that?' -> the episodes behind the fact."""
        r = self.q("""
            MATCH (f:Fact {id: $id})
            OPTIONAL MATCH (f)-[:SUPPORTED_BY]->(v:Visit)
            OPTIONAL MATCH (f)-[:SUPPORTED_BY]->(n:Note)
            RETURN f.kind, f.source, f.confidence, f.support_days,
                   collect(DISTINCT v.local_date), collect(DISTINCT n.text)
        """, {"id": fact_id})
        if not r:
            return {}
        kind, source, conf, days, dates, notes = r[0]
        dates = sorted(d for d in dates if d)
        if source == "user":
            why = f"You told me: \"{notes[0]}\"" if notes else "You told me."
        else:
            why = (f"Seen on {len(dates)} separate days"
                   + (f" ({dates[0]} to {dates[-1]})" if dates else "") + ".")
        return {"fact": fact_id, "kind": kind, "source": source, "confidence": conf,
                "evidence_days": dates, "evidence_notes": [n for n in notes if n], "why": why}

    def open_reminders(self) -> list[dict]:
        rows = self.q("""
            MATCH (n:Note {kind: 'reminder', status: 'open'})
            RETURN n.id, n.text, n.ts_ms, n.trigger_place_type ORDER BY n.ts_ms
        """)
        return [{k: v for k, v in zip(["id", "text", "ts_ms", "trigger_place_type"], r) if v is not None}
                for r in rows]

    def context(self, lat: float | None = None, lon: float | None = None,
                place_type: str | None = None) -> dict:
        """What the planner gets right now: where am I, what do I know about it,
        which reminders apply here. Small and relevant, not the whole graph."""
        out: dict = {"place": None, "history": None, "facts": [], "reminders_here": [],
                     "other_open_reminders": []}
        if lat is not None and lon is not None:
            r = self.q("""
                MATCH (p:Place)
                WHERE distance(p.geo, point({latitude: $lat, longitude: $lon})) < $r
                RETURN p.id, p.name, p.place_type,
                       distance(p.geo, point({latitude: $lat, longitude: $lon})) AS d
                ORDER BY d LIMIT 1
            """, {"lat": lat, "lon": lon, "r": PLACE_MATCH_RADIUS_M})
            if r:
                pid, name, ptype, d = r[0]
                out["place"] = {"id": pid, "name": name, "place_type": ptype, "distance_m": round(d)}
                out["history"] = self.place_history(pid)
                place_type = place_type or ptype
                out["facts"] = [f for f in self.facts() if f.get("place_id") == pid]
        for rem in self.open_reminders():
            trig = rem.get("trigger_place_type")
            key = "reminders_here" if trig and trig == place_type else "other_open_reminders"
            out[key].append(rem)
        return out

    def stats(self) -> dict:
        r = self.q("""
            OPTIONAL MATCH (v:Visit) WITH count(v) AS visits
            OPTIONAL MATCH (p:Place) WITH visits, count(p) AS places
            OPTIONAL MATCH (f:Fact) WITH visits, places, count(f) AS facts
            OPTIONAL MATCH (n:Note) RETURN visits, places, facts, count(n) AS notes
        """)
        return dict(zip(["visits", "places", "facts", "notes"], r[0]))


def _place_type_in_text(low: str) -> str | None:
    words = {ptype: {ptype.replace("_", " ")} | scenes for ptype, (scenes, _) in PLACE_CUES.items()}
    words["supermarket"] |= {"grocery", "groceries", "fairprice", "cold storage"}
    words["gym"] |= {"workout"}
    for ptype, ws in words.items():
        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in ws):
            return ptype
    return None
