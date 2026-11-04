"""Test doubles shared by the test modules (no network, no FalkorDB)."""

from __future__ import annotations

import json
from typing import Any

from prod_dreamer_model.graph import GraphStats, safe_cypher


class FakeGraph:
    """In-memory stand-in for ProdGraph with the subset the dreamer uses."""

    def __init__(self, frames: list[dict] | None = None, notes: list[dict] | None = None):
        self.frames = frames or []
        self.notes = notes or []
        self.patterns: dict[str, dict] = {}
        self.memories: dict[str, dict] = {}
        self.reads: list[str] = []

    # -- reads -------------------------------------------------------------

    def stats(self) -> GraphStats:
        return GraphStats(
            labels={"Frame": len(self.frames), "Note": len(self.notes),
                    "Pattern": len(self.patterns), "LongTermMemory": len(self.memories)},
            relationships={},
            frames=len(self.frames),
            days=len({f["local_date"] for f in self.frames}),
            notes=len(self.notes),
            classes=len({c for f in self.frames for c in f.get("classes", [])}),
            patterns=len(self.patterns),
            memories=len(self.memories),
            first_ms=min((f["ts_ms"] for f in self.frames), default=None),
            last_ms=max((f["ts_ms"] for f in self.frames), default=None),
            gps_frames=sum(1 for f in self.frames if f.get("lat") is not None),
        )

    def fetch_patterns(self) -> list[dict]:
        return [
            {**p, "evidence_count": 0, "about": p.get("about", [])}
            for p in self.patterns.values()
        ]

    def fetch_memories(self) -> list[dict]:
        return [
            {**m, "evidence_count": 0, "about": m.get("about", []),
             "from_patterns": m.get("from_patterns", [])}
            for m in self.memories.values()
        ]

    def fetch_notes(self, limit: int | None = None) -> list[dict]:
        return self.notes[:limit] if limit else list(self.notes)

    def fetch_frame_projection(
        self, day_id: str | None = None, limit: int | None = None
    ) -> list[dict]:
        frames = self.frames
        if day_id:
            frames = [f for f in frames if f.get("local_date") == day_id]
        return frames[:limit] if limit else list(frames)

    def fetch_evidence(self, node_id: str) -> list[dict]:
        return []

    def run_read(self, cypher: str, params: dict | None = None) -> dict[str, Any]:
        ok, reason = safe_cypher(cypher)
        if not ok:
            return {"ok": False, "error": reason}
        self.reads.append(cypher)
        return {"ok": True, "row_count": 0, "truncated": False, "rows": []}

    # -- writes ------------------------------------------------------------

    def write_pattern(self, **kwargs: Any) -> dict[str, Any]:
        pid = kwargs["pattern_id"]
        self.patterns[pid] = {**kwargs, "pattern_id": pid}
        return {"ok": True, "pattern_id": pid, "evidence_linked": len(kwargs.get("evidence") or [])}

    def write_memory(self, **kwargs: Any) -> dict[str, Any]:
        mid = kwargs["memory_id"]
        self.memories[mid] = {**kwargs, "memory_id": mid}
        return {"ok": True, "memory_id": mid, "evidence_linked": len(kwargs.get("evidence") or [])}

    # -- evidence hygiene --------------------------------------------------

    def propagate_evidence(self) -> int:
        return 0

    def backfill_evidence(self, per_node: int = 5) -> int:
        return 0

    def enforce_evidence(self) -> dict[str, int]:
        return {"unconfirmed": 0, "confirmed": 0}


class ScriptedClient:
    """Replays a fixed list of Responses-API shaped payloads."""

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def create(self, input_items: list[dict], previous_response_id: str | None = None) -> dict:
        self.calls.append(input_items)
        if not self.responses:
            return {"id": "resp-empty", "output": []}
        return self.responses.pop(0)


def call(name: str, args: dict, call_id: str = "c1") -> dict:
    return {
        "type": "function_call",
        "name": name,
        "arguments": json.dumps(args),
        "call_id": call_id,
    }


def week_frames_data() -> list[dict]:
    """Two weekday mornings of bus + classroom, enough for a routine."""
    frames: list[dict] = []
    for day_index, day in enumerate(["2026-10-26", "2026-10-27"]):
        for minute, classes in [(492, ["bus interior", "bus"]), (512, ["bus interior"]),
                                (522, ["classroom", "projector"])]:
            frames.append({
                "frame_id": f"{day}-{minute}",
                "ts_ms": 1_793_000_000_000 + day_index * 86_400_000 + minute * 60_000,
                "local_date": day,
                "local_hour": minute // 60,
                "local_min": minute,
                "dow": day_index,
                "lat": 1.34,
                "lon": 103.68,
                "classes": classes,
            })
    return frames
