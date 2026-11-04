"""Vision-frame parsing.

The wearable's vision model emits one record per sampled frame::

    {"frame_id": "99b232d8abc9",
     "timestamp": "2026-10-26T09:38:46.744729+00:00",
     "classes": [{"class": "person", "count": 2, "confidence": 0.94},
                 {"class": "cup", "count": 4, "confidence": 0.72}]}

That is the *raw node memory of a day*. This module parses it (also tolerating
the older ``classes: ["person"]`` shape) and turns a frame into a
``vixy.video_observation.v1`` event for context-engine. ``count`` survives the
round trip inside the observation's free-form ``source`` metadata, because the
v1 contract's ``Detection`` carries a label and a confidence but no count.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class ClassObservation:
    name: str
    count: int
    confidence: float


@dataclass(frozen=True)
class VisionFrame:
    frame_id: str
    ts_ms: int
    classes: tuple[ClassObservation, ...]
    lat: float | None = None
    lon: float | None = None
    accuracy_m: float | None = None

    @property
    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.ts_ms / 1000, tz=timezone.utc)

    @property
    def has_gps(self) -> bool:
        return self.lat is not None and self.lon is not None

    def class_map(self) -> dict[str, tuple[int, float]]:
        return {c.name: (c.count, c.confidence) for c in self.classes}


def parse_ts(value: str | int | float) -> int:
    """ISO-8601 string or epoch (seconds or milliseconds) -> epoch milliseconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value if value > 1e12 else value * 1000)
    text = str(value).strip()
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"naive timestamp is not accepted: {value!r}")
    return int(dt.timestamp() * 1000)


def _parse_classes(raw: Any) -> tuple[ClassObservation, ...]:
    """Accepts the current shape, plus ``["person"]`` and ``{"label","score"}``."""
    if not raw:
        return ()
    scores = raw.get("confidences") if isinstance(raw, dict) else None
    merged: dict[str, tuple[int, float]] = {}
    items = raw.get("classes") if isinstance(raw, dict) else raw
    for entry in items or []:
        if isinstance(entry, dict):
            name = entry.get("class") or entry.get("label") or entry.get("name")
            conf = entry.get("confidence", entry.get("score", 1.0))
            count = entry.get("count", 1)
        else:
            name, conf, count = entry, 1.0, 1
            if scores:
                conf = scores.get(entry, 1.0)
        if not name:
            continue
        name = str(name).strip().lower()
        if not name:
            continue
        prev_count, prev_conf = merged.get(name, (0, 0.0))
        merged[name] = (prev_count + max(int(count or 1), 0), max(float(conf), prev_conf))
    return tuple(
        ClassObservation(name=n, count=c, confidence=round(conf, 4))
        for n, (c, conf) in sorted(merged.items())
    )


def parse_frame(raw: dict[str, Any]) -> VisionFrame:
    """Parse one vision-model record. Raises on a missing timestamp."""
    loc = raw.get("location") or raw.get("gps") or {}
    lat = raw.get("lat", loc.get("lat", loc.get("latitude")))
    lon = raw.get("lon", loc.get("lon", loc.get("longitude")))
    acc = raw.get("accuracy_m", loc.get("accuracy_m"))
    return VisionFrame(
        frame_id=str(raw.get("frame_id") or raw["timestamp"]),
        ts_ms=parse_ts(raw["timestamp"]),
        classes=_parse_classes(raw),
        lat=float(lat) if lat is not None else None,
        lon=float(lon) if lon is not None else None,
        accuracy_m=float(acc) if acc is not None else None,
    )


def load_frames(path: str | Path) -> list[VisionFrame]:
    """Read a JSON array, a single JSON object, or JSON Lines of frames."""
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text[0] == "[":
        records = json.loads(text)
    elif text[0] == "{" and "\n" not in text:
        records = [json.loads(text)]
    else:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [parse_frame(r) for r in records]


def describe_frame(frame: VisionFrame) -> str:
    """Human-readable scene summary used as the observation description."""
    parts = []
    for c in sorted(frame.classes, key=lambda c: (-c.confidence, c.name)):
        parts.append(f"{c.name} x{c.count}" if c.count > 1 else c.name)
    return ", ".join(parts) or "empty scene"


def frame_counts(frame: VisionFrame) -> dict[str, int]:
    return {c.name: c.count for c in frame.classes}


def frame_confidences(frame: VisionFrame) -> dict[str, float]:
    return {c.name: c.confidence for c in frame.classes}


def to_video_observation(
    frame: VisionFrame,
    session_id: str,
    stream: str = "cam0",
    pipeline: str = "mock-wearable-vision",
) -> dict[str, Any]:
    """Map a vision frame onto context-engine's ``vixy.video_observation.v1``.

    ``count`` is preserved in ``source["counts"]`` so the dreamer sees the whole
    original signal after the context-engine round trip. ``stream`` names the
    camera; the event id ``<stream>:<frame_id>`` is stable across retries.
    """
    observation: dict[str, Any] = {
        "event_id": f"{stream}:{frame.frame_id}",
        "session_id": session_id,
        "timestamp": frame.timestamp.isoformat(),
        "description": describe_frame(frame),
        "detections": [
            {"label": c.name, "confidence": c.confidence} for c in frame.classes
        ],
        "frame_ref": f"{stream}/{frame.frame_id}",
        "source": {
            "pipeline": pipeline,
            "stream": stream,
            "frame_id": frame.frame_id,
            "counts": frame_counts(frame),
        },
    }
    if frame.has_gps:
        observation["location"] = {
            "latitude": frame.lat,
            "longitude": frame.lon,
            **({"accuracy_m": frame.accuracy_m} if frame.accuracy_m is not None else {}),
        }
    return observation


def observations_from_frames(
    frames: Iterable[VisionFrame], session_id: str
) -> list[dict[str, Any]]:
    return [to_video_observation(f, session_id) for f in frames]
