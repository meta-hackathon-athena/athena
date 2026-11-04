"""Validated local recording manifests. All media offsets are seconds."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from context_engine.schemas import Location, UtcDatetime
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PerceptionSettings(Strict):
    fps: float = Field(1, gt=0, le=30)
    vision_model: str = "yolo11n.pt"
    vision_device: str = "cpu"       # cpu, mps or CUDA index; explicit and reproducible
    confidence: float = Field(0.35, ge=0, le=1)
    image_size: int = Field(640, ge=32, le=2048)
    asr_model: str = "base.en"
    asr_device: str = "cpu"
    language: str | None = "en"    # null = Whisper language detection
    threads: int = Field(4, ge=1, le=32)


class SpeakerAnnotation(Strict):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    speaker_id: str | None = None
    speaker_is_wearer: bool | None = None

    @model_validator(mode="after")
    def interval(self):
        if self.end <= self.start:
            raise ValueError("speaker annotation end must follow start")
        return self


class GPSPoint(Location):
    offset_s: float = Field(ge=0)


class Recording(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    session_id: str = Field(min_length=1)
    video: str
    simulated_start: UtcDatetime
    recorded_at: UtcDatetime | None = None
    speakers: list[SpeakerAnnotation] = Field(default_factory=list)
    gps: list[GPSPoint] = Field(default_factory=list)
    gps_max_age_s: float = Field(30, ge=0)
    dream_after: bool = False

    @model_validator(mode="after")
    def annotations(self):
        ordered = sorted(self.speakers, key=lambda s: s.start)
        if any(a.end > b.start for a, b in zip(ordered, ordered[1:])):
            raise ValueError("speaker annotations must not overlap")
        if len({p.offset_s for p in self.gps}) != len(self.gps):
            raise ValueError("GPS offsets must be unique")
        return self


class Scenario(Strict):
    schema_version: str = "vixy.scenario.v1"
    user_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    disposable: bool = False
    playback_speed: float = Field(0, ge=0)  # 0 = fast deterministic virtual time
    tick_seconds: int = Field(60, ge=1, le=300)
    perception: PerceptionSettings = Field(default_factory=PerceptionSettings)
    recordings: list[Recording] = Field(min_length=1)
    dream_boundaries: list[UtcDatetime] = Field(default_factory=list)
    end_time: UtcDatetime | None = None

    @field_validator("schema_version")
    @classmethod
    def version(cls, v):
        if v != "vixy.scenario.v1":
            raise ValueError("unsupported scenario version")
        return v

    @model_validator(mode="after")
    def unique(self):
        if len({r.id for r in self.recordings}) != len(self.recordings):
            raise ValueError("recording IDs must be unique")
        return self


def load_scenario(path: str | Path) -> Scenario:
    return Scenario.model_validate_json(Path(path).read_text(encoding="utf-8"))


def video_path(manifest: Path, recording: Recording) -> Path:
    path = (manifest.resolve().parent / recording.video).resolve()
    if not path.is_file():
        raise ValueError(f"missing video for recording {recording.id}")
    if path.suffix.lower() != ".mp4":
        raise ValueError("this simulator accepts MP4 inputs")
    return path


def digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ms(value: datetime) -> int:
    return round(value.timestamp() * 1000)
