"""Episode Writer: the single writer of observed episodes in the unified graph.

  CE audio event  -> (:Note)   one per utterance, via MemoryStore.add_note (Samu's reminder /
                               preference classification), BEFORE the agent sees the utterance
  CE video events -> (:Visit)  Samu's Segmenter groups frames into stays/transits; each closed
                               visit is stored via MemoryStore.add_visit (Place, Tag, NEXT)
  both            -> (:Day)    the local day, shared with the Dreamer's Day concept

Every node keeps its provenance: the context-engine event id(s), session, user, UTC
timestamps and the CE payload hash. Ids derive from event ids, so replays MERGE into
the same nodes. Raw frames are not stored in the graph; a Visit cites its first/last
frame event ids and a sample, and the full frame list stays in context-engine SQLite.

Restart safety for visits: the Segmenter's open visit lives in memory. After every frame
the writer persists (in the runtime ledger) the ingest_seq range it would need to
rebuild that state; on start-up ``rehydrate`` replays exactly those frames from
context-engine, so an open visit is neither lost nor duplicated.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from memory.segmenter import Segmenter, SegmenterConfig, Visit
from memory.store import TZ, MemoryStore

from .adapters import memory_frame
from .contracts import GRAPH_SCHEMA_VERSION, UNKNOWN, speaker_role
from .ledger import Ledger

SAMPLE_FRAMES = 5


def note_id_for(event_id: str) -> str:
    return "note_" + hashlib.sha1(event_id.encode()).hexdigest()[:16]


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _sample(ids: list[str], n: int = SAMPLE_FRAMES) -> list[str]:
    if len(ids) <= n:
        return list(ids)
    step = (len(ids) - 1) / (n - 1)
    return [ids[round(i * step)] for i in range(n)]


class EpisodeWriter:
    def __init__(self, store: MemoryStore, user_id: str, session_id: str, ledger: Ledger,
                 segmenter_config: SegmenterConfig | None = None):
        self.store, self.user_id, self.session_id, self.ledger = store, user_id, session_id, ledger
        self.seg = Segmenter(user_id, segmenter_config)
        self._unwritten: list[Visit] = []          # closed, not yet stored (kept across failures)
        self._seq: dict[str, int] = {}             # frame event_id -> ingest_seq (open + unwritten)
        state = ledger.get_kv(self._key, {}) or {}
        self.last_fed_seq: int = state.get("last_fed_seq", 0)

    @property
    def _key(self) -> str:
        return f"segmenter:{self.session_id}"

    # ------------------------------------------------------------------ audio
    def write_note(self, record) -> dict:
        """Persist one utterance. Must run before the agent handles it."""
        ev = record.event
        start_ms, end_ms = _ms(ev["start_time"]), _ms(ev["end_time"])
        role = speaker_role(ev.get("source"))
        loc = ev.get("location") or {}
        nid = note_id_for(record.event_id)
        note = self.store.add_note(ev["text"], start_ms, source="voice", note_id=nid,
                                   attribute_to_user=role == "wearer", props={
            "note_id": record.event_id,               # Dreamer convention: note_id == CE event_id
            "source_event_id": record.event_id,
            "session_id": record.session_id, "user_id": self.user_id,
            "start_ms": start_ms, "end_ms": end_ms, "timestamp": _utc(start_ms),
            "speaker_id": ev.get("speaker_id") or UNKNOWN, "speaker_role": role,
            "confidence": ev.get("confidence"),
            "lat": loc.get("latitude"), "lon": loc.get("longitude"),
            "payload_sha256": record.payload_sha256, "ingest_seq": record.ingest_seq,
            "event_schema": ev.get("schema_version"), "schema_version": GRAPH_SCHEMA_VERSION,
            "layer": "observed",
        })
        self._on_day("Note", nid, note["local_date"])
        return note

    # ------------------------------------------------------------------ video
    def observe_frame(self, record) -> list[str]:
        """Feed one frame to the segmenter; store any visit it closes. Returns visit ids written.

        A frame already fed (ingest_seq <= last_fed_seq, e.g. its visit write failed and
        the event is being retried) is not fed again: only the pending writes are retried.
        """
        if record.ingest_seq > self.last_fed_seq:
            frame = memory_frame(record.event, record.ingest_seq)
            self._seq[frame.frame_id] = record.ingest_seq
            self._unwritten += self.seg.add(frame)
            self.last_fed_seq = record.ingest_seq
            self._save_checkpoint()
        return self._write_pending()

    def flush(self) -> list[str]:
        """Close the open visit (end of a recording / day). Do not call merely on shutdown:
        an open visit is restored by ``rehydrate`` and may continue after a restart."""
        self._unwritten += self.seg.flush()
        self._save_checkpoint()
        return self._write_pending()

    def rehydrate(self, engine) -> dict:
        """Rebuild the open visit from context-engine after a restart, then retry writes."""
        state = self.ledger.get_kv(self._key, {}) or {}
        start, last = state.get("from_seq"), state.get("last_fed_seq", 0)
        replayed = 0
        if start is not None and last:
            for rec in engine.records_after(self.session_id, start - 1):
                if rec.ingest_seq > last:
                    break
                if rec.kind != "video":
                    continue
                frame = memory_frame(rec.event, rec.ingest_seq)
                self._seq[frame.frame_id] = rec.ingest_seq
                self._unwritten += self.seg.add(frame)
                replayed += 1
        self.last_fed_seq = last
        written = self._write_pending()
        return {"frames_replayed": replayed, "visits_written": len(written)}

    @property
    def open_frames(self) -> int:
        return len(self.seg.open_frames())

    def _save_checkpoint(self) -> None:
        firsts = [self._seq[f.frame_id] for f in self.seg.open_frames()[:1] if f.frame_id in self._seq]
        firsts += [self._seq[v.first_frame_id] for v in self._unwritten if v.first_frame_id in self._seq]
        keep = set(f.frame_id for f in self.seg.open_frames())
        keep |= {fid for v in self._unwritten for fid in v.frame_ids}
        self._seq = {k: s for k, s in self._seq.items() if k in keep}
        self.ledger.set_kv(self._key, {"from_seq": min(firsts) if firsts else None,
                                       "last_fed_seq": self.last_fed_seq})

    def _write_pending(self) -> list[str]:
        written = []
        while self._unwritten:
            v = self._unwritten[0]
            self._write_visit(v)                  # raises on graph failure; v stays queued
            self._unwritten.pop(0)
            written.append(v.id)
        if written:
            self._save_checkpoint()
        return written

    def _write_visit(self, v: Visit) -> None:
        self.store.add_visit(v)
        local_date = datetime.fromtimestamp(v.start_ms / 1000, TZ).date().isoformat()
        self.store.q("""
            MATCH (v:Visit {id: $id}) SET v += $props
            WITH v
            MATCH (n:Note) WHERE n.session_id = $session AND n.ts_ms >= v.start_ms
              AND n.ts_ms <= v.end_ms AND NOT (n)-[:DURING]->(:Visit)
            MERGE (n)-[:DURING]->(v)
        """, {"id": v.id, "session": self.session_id, "props": {
            "session_id": self.session_id, "user_id": self.user_id,
            "first_event_id": v.first_frame_id, "last_event_id": v.last_frame_id,
            "sample_event_ids": _sample(v.frame_ids), "start_time": _utc(v.start_ms), "end_time": _utc(v.end_ms),
            "day_id": local_date, "schema_version": GRAPH_SCHEMA_VERSION, "layer": "observed",
        }})
        self._on_day("Visit", v.id, local_date)

    def _on_day(self, label: str, node_id: str, local_date: str) -> None:
        self.store.q(f"""
            MERGE (d:Day {{day_id: $day}})
            ON CREATE SET d.date = $day, d.user_id = $user, d.created_ms = timestamp()
            WITH d MATCH (x:{label} {{id: $id}}) MERGE (x)-[:ON_DAY]->(d)
        """, {"day": local_date, "user": self.user_id, "id": node_id})
