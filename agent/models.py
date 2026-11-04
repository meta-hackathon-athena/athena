"""Typed inputs (vision frames, audio transcripts) and outputs (decisions).

Inputs are validated and normalised here, so the rest of the agent can trust
their shape. Unknown extra fields are kept in `extra` and never break parsing.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum

from memory.frames import _parse_classes, parse_ts


class InvalidObservation(ValueError):
    pass


def _ts(raw: dict, *keys: str) -> int:
    for k in keys:
        if raw.get(k) is not None:
            try:
                return parse_ts(raw[k])
            except (TypeError, ValueError) as e:
                raise InvalidObservation(f"bad timestamp {k}={raw[k]!r}") from e
    raise InvalidObservation(f"missing timestamp ({' / '.join(keys)})")


def _coord(raw: dict, *keys: str) -> float | None:
    for k in keys:
        v = raw.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


@dataclass
class VisionObservation:
    frame_id: str
    ts_ms: int
    classes: dict[str, float]            # class -> confidence
    counts: dict[str, int] = field(default_factory=dict)
    lat: float | None = None
    lon: float | None = None
    extra: dict = field(default_factory=dict)

    KNOWN = {"frame_id", "timestamp", "ts", "classes", "confidences", "scores", "count",
             "latitude", "longitude", "lat", "lon", "image"}

    @classmethod
    def parse(cls, raw: dict) -> "VisionObservation":
        if not isinstance(raw, dict):
            raise InvalidObservation("vision observation must be an object")
        ts = _ts(raw, "timestamp", "ts")
        conf, counts = _parse_classes(raw)
        lat, lon = _coord(raw, "latitude", "lat"), _coord(raw, "longitude", "lon")
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            lat = lon = None                 # GPS is optional; bad GPS == no GPS
        fid = raw.get("frame_id") or "frm_" + hashlib.sha1(f"{ts}:{sorted(conf)}".encode()).hexdigest()[:12]
        return cls(str(fid), ts, conf, counts, lat, lon,
                   {k: v for k, v in raw.items() if k not in cls.KNOWN})

    @property
    def has_gps(self) -> bool:
        return self.lat is not None


@dataclass
class AudioObservation:
    segment_id: str
    start_ms: int
    end_ms: int
    transcript: str
    language: str = "en"
    speaker_id: str | None = None
    # True = the wearer said it, False = someone else, None = unknown (the common case)
    speaker_is_user: bool | None = None
    confidence: float | None = None
    lat: float | None = None
    lon: float | None = None
    extra: dict = field(default_factory=dict)

    KNOWN = {"segment_id", "timestamp_start", "timestamp_end", "timestamp", "transcript",
             "language", "speaker_id", "speaker_is_user", "confidence", "latitude", "longitude"}

    @classmethod
    def parse(cls, raw: dict) -> "AudioObservation":
        if not isinstance(raw, dict):
            raise InvalidObservation("audio observation must be an object")
        text = " ".join(str(raw.get("transcript") or "").split())
        if not text:
            raise InvalidObservation("empty transcript")
        start = _ts(raw, "timestamp_start", "timestamp")
        end = _ts(raw, "timestamp_end") if raw.get("timestamp_end") else start
        if end < start:
            start, end = end, start
        sid = raw.get("segment_id") or "aud_" + hashlib.sha1(f"{start}:{text}".encode()).hexdigest()[:12]
        siu = raw.get("speaker_is_user")
        conf = raw.get("confidence")
        lat, lon = _coord(raw, "latitude"), _coord(raw, "longitude")
        return cls(str(sid), start, end, text, raw.get("language") or "en", raw.get("speaker_id"),
                   siu if isinstance(siu, bool) else None,
                   float(conf) if isinstance(conf, (int, float)) else None,
                   lat if lon is not None else None, lon if lat is not None else None,
                   {k: v for k, v in raw.items() if k not in cls.KNOWN})

    @property
    def ts_ms(self) -> int:
        return self.end_ms

    @property
    def content_key(self) -> str:
        """Same words, same moment -> same key (catches re-sent segments with new ids)."""
        norm = "".join(ch for ch in self.transcript.lower() if ch.isalnum() or ch == " ")
        return hashlib.sha1(f"{self.start_ms // 5000}:{norm}".encode()).hexdigest()[:16]


class Action(str, Enum):
    NO_ACTION = "NO_ACTION"
    NOTIFY = "NOTIFY"
    ASK_USER = "ASK_USER"
    MEMORY_UPDATE = "MEMORY_UPDATE"


class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


# When one evaluation yields several decisions they run in this order:
# write first (so a notification can reference what was written), then tell, then ask.
ACTION_ORDER = {Action.MEMORY_UPDATE: 0, Action.NOTIFY: 1, Action.ASK_USER: 2, Action.NO_ACTION: 3}


@dataclass
class Decision:
    action: Action
    reason: str
    ts_ms: int
    priority: Priority = Priority.LOW
    confidence: float = 1.0
    message: str | None = None
    related_memory_ids: list[str] = field(default_factory=list)
    deduplication_key: str | None = None
    proposed_memory_update: dict | None = None
    question_id: str | None = None
    expires_ms: int | None = None
    trigger: str | None = None
    # the observation (context-engine event_id) whose handling produced this decision
    trigger_event_id: str | None = None
    # filled in after execution: 'done' | 'suppressed' | 'failed' | 'deferred'
    status: str = "pending"
    decision_id: str = ""
    # memory-aware decisions: the remembered facts it relies on (id, statement, epistemic
    # status, evidence ids) and who decided ("rules:..." or "model:<id>")
    supporting_memories: list[dict] = field(default_factory=list)
    reasoning: str | None = None

    def __post_init__(self):
        if not self.decision_id:
            basis = self.deduplication_key or f"{self.action.value}:{self.ts_ms}:{self.reason}"
            self.decision_id = "dec_" + hashlib.sha1(f"{basis}:{self.ts_ms}".encode()).hexdigest()[:12]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["action"], d["priority"] = self.action.value, self.priority.value
        return d


def no_action(reason: str, ts_ms: int, trigger: str | None = None) -> Decision:
    return Decision(Action.NO_ACTION, reason, ts_ms, trigger=trigger, status="done")
