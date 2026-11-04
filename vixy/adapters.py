"""Adapters between perception output, Context Engine v1 events and the agent/memory types.

    perception frame  --video_from_perception-->  CE VideoObservation (reuses the Dreamer's
                                                  frames.parse_frame / to_video_observation)
    ASR segment       --audio_from_transcription->  CE AudioSegment
    CE event          --agent_observation------>  agent Vision/AudioObservation input dict
    CE video event    --memory_frame----------->  memory.frames.Frame for the Segmenter

Nothing is invented on the way: a missing count stays missing, a missing speaker flag
stays unknown, a missing GPS fix stays absent. The video description is a synthetic
caption listing the detected classes ("person x2, cup x4"), never a scene guess.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from context_engine import AudioSegment, VideoObservation
from context_engine.adapters import audio_from_relative_segment
from memory.frames import Frame, parse_ts
from prod_dreamer_model.frames import parse_frame, to_video_observation

from .contracts import source_dict, speaker_is_wearer

VIDEO_ADAPTER = "vixy.perception_frame.v1"
AUDIO_ADAPTER = "vixy.asr_segment.v1"


def _location(raw: Mapping[str, Any]) -> dict[str, float] | None:
    loc = raw.get("location") or raw.get("gps") or {}
    lat = raw.get("latitude", raw.get("lat", loc.get("latitude", loc.get("lat"))))
    lon = raw.get("longitude", raw.get("lon", loc.get("longitude", loc.get("lon"))))
    if lat is None or lon is None:
        return None
    out = {"latitude": float(lat), "longitude": float(lon)}
    acc = raw.get("accuracy_m", loc.get("accuracy_m"))
    if acc is not None:
        out["accuracy_m"] = float(acc)
    return out


def _explicit_counts(raw: Mapping[str, Any]) -> dict[str, int]:
    """Counts the detector actually reported (the Dreamer parser defaults absent ones to 1)."""
    counts: dict[str, int] = {}
    for c in raw.get("classes") or []:
        if isinstance(c, Mapping) and c.get("count") is not None:
            name = str(c.get("class") or c.get("label") or c.get("name") or "").strip().lower()
            if name:
                counts[name] = counts.get(name, 0) + int(c["count"])
    return counts


def video_from_perception(raw: Mapping[str, Any], *, session_id: str, user_id: str,
                          stream: str = "cam0", pipeline: str = "vixy-perception") -> VideoObservation:
    """``{frame_id, timestamp, classes:[{class, count, confidence}], latitude?, longitude?}``
    -> validated ``vixy.video_observation.v1``. event_id = ``<stream>:<frame_id>`` (stable)."""
    raw = dict(raw)
    loc = _location(raw)
    if loc:                                   # parse_frame reads lat/lon or a location object
        raw["location"] = {"lat": loc["latitude"], "lon": loc["longitude"],
                           **({"accuracy_m": loc["accuracy_m"]} if "accuracy_m" in loc else {})}
    obs = to_video_observation(parse_frame(raw), session_id, stream=stream, pipeline=pipeline)
    counts = _explicit_counts(raw)
    src = obs["source"]
    obs["source"] = source_dict(user_id=user_id, adapter=VIDEO_ADAPTER, pipeline=src["pipeline"],
                                stream=src["stream"], frame_id=src["frame_id"], counts=counts or None)
    return VideoObservation.model_validate(obs)


def audio_from_transcription(raw: Mapping[str, Any], *, session_id: str, user_id: str,
                             stream: str = "mic0", pipeline: str = "vixy-asr",
                             recording_started_at: datetime | None = None) -> AudioSegment:
    """A finalized ASR segment -> validated ``vixy.audio_segment.v1``.

    Absolute shape: ``{segment_id, recording_id?, start_time, end_time, text, speaker_id?,
    speaker_is_wearer?, confidence?, language?, latitude?, longitude?}`` (ISO-8601 with offset).
    Relative (Whisper-style) shape: ``{id, start, end, text}`` plus ``recording_started_at``;
    this reuses context-engine's ``audio_from_relative_segment``.

    ``speaker_is_wearer`` must be an explicit boolean from upstream (VAD / diarization); it is
    never derived from ``speaker_id``, and an absent flag means "unknown".
    """
    wearer = raw.get("speaker_is_wearer", raw.get("speaker_is_user"))
    wearer = wearer if isinstance(wearer, bool) else None
    recording = raw.get("recording_id")
    meta = dict(user_id=user_id, adapter=AUDIO_ADAPTER, pipeline=pipeline, stream=stream,
                recording_id=recording, speaker_is_wearer=wearer, language=raw.get("language"))
    if "start" in raw and "start_time" not in raw:
        if recording_started_at is None or recording is None:
            raise ValueError("relative segments need recording_id and recording_started_at")
        seg = audio_from_relative_segment(raw, session_id=session_id, recording_id=str(recording),
                                          recording_started_at=recording_started_at,
                                          speaker_id=raw.get("speaker_id"))
        payload = seg.model_dump()
        payload["source"] = source_dict(**meta, segment_id=str(raw["id"]))
        return AudioSegment.model_validate(payload)
    segment_id = str(raw.get("segment_id") or raw.get("id") or "")
    if not segment_id:
        raise ValueError("ASR segment needs a stable segment_id (retries must reuse it)")
    payload: dict[str, Any] = {
        "event_id": f"asr:{recording or stream}:{segment_id}",
        "session_id": session_id,
        "start_time": raw["start_time"],
        "end_time": raw["end_time"],
        "text": str(raw["text"]).strip(),
        "source": source_dict(**meta, segment_id=segment_id),
    }
    if raw.get("speaker_id") is not None:
        payload["speaker_id"] = str(raw["speaker_id"])
    if raw.get("confidence") is not None:
        payload["confidence"] = float(raw["confidence"])
    if (loc := _location(raw)) is not None:
        payload["location"] = loc
    return AudioSegment.model_validate(payload)


def agent_observation(kind: str, event: Mapping[str, Any]) -> dict[str, Any]:
    """A Context Engine event (as stored/exported, JSON form) -> the agent's input dict.

    segment_id / frame_id are the CE event_id, so every agent decision can cite it.
    """
    loc = event.get("location") or {}
    source = event.get("source") or {}
    if kind == "audio":
        out: dict[str, Any] = {
            "segment_id": event["event_id"],
            "timestamp_start": event["start_time"],
            "timestamp_end": event["end_time"],
            "transcript": event["text"],
            "speaker_id": event.get("speaker_id"),
            "speaker_is_user": speaker_is_wearer(source),          # None = unknown
        }
        if source.get("language"):
            out["language"] = source["language"]
        if event.get("confidence") is not None:
            out["confidence"] = event["confidence"]
    else:
        counts = source.get("counts") or {}
        classes = []
        for d in event.get("detections") or []:
            c = {"class": d["label"], "confidence": d["confidence"]}
            if d["label"] in counts:
                c["count"] = counts[d["label"]]
            classes.append(c)
        out = {"frame_id": event["event_id"], "timestamp": event["timestamp"], "classes": classes}
    if loc:
        out["latitude"], out["longitude"] = loc["latitude"], loc["longitude"]
    return out


def memory_frame(event: Mapping[str, Any], ingest_seq: int | None = None) -> Frame:
    """A CE video event -> the Segmenter's Frame. frame_id is the CE event_id."""
    loc = event.get("location") or {}
    counts = (event.get("source") or {}).get("counts") or {}
    classes: dict[str, float] = {}
    for d in event.get("detections") or []:
        name = str(d["label"]).strip().lower()
        classes[name] = max(float(d["confidence"]), classes.get(name, 0.0))
    return Frame(
        frame_id=event["event_id"], ts_ms=parse_ts(event["timestamp"]), classes=classes,
        lat=loc.get("latitude"), lon=loc.get("longitude"), accuracy_m=loc.get("accuracy_m"),
        counts={k.lower(): int(v) for k, v in counts.items()},
        extra={"ingest_seq": ingest_seq} if ingest_seq is not None else {},
    )
