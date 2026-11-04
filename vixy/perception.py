"""Local MP4 -> actual YOLO + Whisper -> CE contracts. No remote inference."""
from __future__ import annotations

import importlib.metadata
import json
import math
import os
import subprocess
import tempfile
import time
import wave
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from context_engine import AudioSegment, VideoObservation

from . import adapters
from .scenario import PerceptionSettings, Recording, Scenario, digest, file_hash, video_path

PREPROCESS_VERSION = "vixy.perception.v2"
CACHE_SCHEMA = "vixy.perception_cache.v1"

# Whisper timestamp tokens are quantized to 20 ms. This covers small token,
# mel-frame and resampling drift, never a padded inference window.
ASR_DRIFT_TOLERANCE_S = 0.05
SILENCE_RMS = 1e-5


def command(args: list[str]) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as e:
        raise ValueError(f"required executable missing: {args[0]}") from e
    if result.returncode:
        # FFmpeg errors can contain private absolute paths; do not echo them.
        raise ValueError(f"{args[0]} failed (exit {result.returncode}); check media/codec support")
    return result.stdout


def inspect_video(path: Path) -> dict:
    try:
        raw = json.loads(command(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]))
        video = next(s for s in raw["streams"] if s["codec_type"] == "video")
        audio = next((s for s in raw["streams"] if s["codec_type"] == "audio"), None)
        origin = float(raw["format"].get("start_time", video.get("start_time", 0)))
        format_duration = float(raw["format"].get("duration", video.get("duration", 0)))
        video_offset = float(video.get("start_time", origin)) - origin
        audio_offset = float(audio.get("start_time", origin)) - origin if audio else None
        video_duration = float(video.get("duration", format_duration))
        audio_duration = float(audio.get("duration", format_duration)) if audio else None
        # MP4 edit lists can include a leading empty interval in format.duration.
        # Stream ends relative to the playback origin describe actual media.
        duration = max(video_offset + video_duration,
                       audio_offset + audio_duration if audio else 0)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("MP4 has no finite positive duration")
        return {"duration_s": duration, "width": video["width"], "height": video["height"],
                "format_duration_s": format_duration, "timeline_origin_s": origin,
                "video_duration_s": video_duration, "video_end_s": video_offset + video_duration,
                "audio_duration_s": audio_duration,
                "video_codec": video["codec_name"], "video_offset_s": video_offset,
                "audio_codec": audio.get("codec_name") if audio else None,
                "audio_offset_s": audio_offset,
                "recording_time_metadata": raw["format"].get("tags", {}).get("creation_time")}
    except (KeyError, StopIteration, json.JSONDecodeError) as e:
        raise ValueError("invalid MP4 metadata or missing video stream") from e


class LocalModels:
    def __init__(self, settings: PerceptionSettings, model_dir: Path):
        # Keep package settings and weights in ignored local output, never user home.
        model_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("YOLO_CONFIG_DIR", str(model_dir / "ultralytics"))
        import torch
        from ultralytics import settings as yolo_settings
        yolo_settings.update({"sync": False})
        from ultralytics.utils.events import events as yolo_events
        yolo_events.enabled = False
        from ultralytics import YOLO
        import whisper

        torch.set_num_threads(settings.threads)
        self.settings = settings
        candidate = Path(settings.vision_model)
        if not candidate.is_file():
            if candidate.name != str(candidate) or candidate.suffix != ".pt":
                raise ValueError("vision_model must be an existing local checkpoint or a YOLO .pt model name")
            candidate = model_dir / candidate.name
        self.vision = YOLO(str(candidate))
        self.asr = whisper.load_model(settings.asr_model, device=settings.asr_device, download_root=str(model_dir))
        self.usage = {
            "vision": {"model_id": settings.vision_model, "checkpoint_sha256": file_hash(candidate),
                       "package": "ultralytics", "version": importlib.metadata.version("ultralytics"),
                       "device": settings.vision_device, "calls": 0, "seconds": 0.0},
            "asr": {"model_id": settings.asr_model, "package": "openai-whisper",
                    "version": importlib.metadata.version("openai-whisper"), "device": settings.asr_device,
                    "calls": 0, "seconds": 0.0},
            "torch": torch.__version__, "paid_api_calls": 0,
        }
        asr_checkpoint = model_dir / f"{settings.asr_model}.pt"
        if asr_checkpoint.exists():
            self.usage["asr"]["checkpoint_sha256"] = file_hash(asr_checkpoint)

    def detect(self, frame) -> list[dict]:
        t = time.perf_counter()
        result = self.vision.predict(frame, conf=self.settings.confidence, imgsz=self.settings.image_size,
                                     device=self.settings.vision_device, verbose=False)[0]
        grouped = defaultdict(list)
        for cls, confidence in zip(result.boxes.cls.tolist(), result.boxes.conf.tolist()):
            grouped[result.names[int(cls)]].append(float(confidence))
        self.usage["vision"]["calls"] += 1
        self.usage["vision"]["seconds"] += time.perf_counter() - t
        return [{"class": k, "count": len(v), "confidence": max(v), "instance_confidences": v}
                for k, v in sorted(grouped.items())]

    def provenance(self, kind: str) -> dict:
        return {k: v for k, v in self.usage[kind].items() if k not in {"calls", "seconds"}}

    def transcribe(self, wav: Path) -> dict:
        import numpy as np
        import whisper
        audio = whisper.load_audio(str(wav))
        if not len(audio) or float(np.sqrt(np.mean(audio ** 2))) < SILENCE_RMS:
            return {"segments": [], "language": self.settings.language, "silence": True}
        t = time.perf_counter()
        result = self.asr.transcribe(audio, language=self.settings.language, temperature=0,
                                     fp16=self.settings.asr_device.startswith("cuda"), verbose=None,
                                     condition_on_previous_text=False)
        self.usage["asr"]["calls"] += 1
        self.usage["asr"]["seconds"] += time.perf_counter() - t
        return result


def cache_identity(scenario: Scenario, recording: Recording, path: Path, model_dir: Path | None = None) -> dict:
    settings = scenario.perception.model_dump()
    # Custom checkpoints may change without their filename changing.
    for field in ("vision_model", "asr_model"):
        checkpoint = Path(settings[field])
        if not checkpoint.is_file() and model_dir:
            checkpoint = model_dir / (checkpoint.name if field == "vision_model" else f"{checkpoint.name}.pt")
        if checkpoint.is_file():
            settings[field + "_sha256"] = file_hash(checkpoint)
    versions = {p: importlib.metadata.version(p) for p in ("ultralytics", "openai-whisper", "opencv-python", "torch")}
    return {"preprocessing": PREPROCESS_VERSION, "source_sha256": file_hash(path), "versions": versions,
            "user_id": scenario.user_id, "recording": recording.model_dump(mode="json"), "settings": settings}


def _location(recording: Recording, offset: float) -> dict | None:
    fixes = [p for p in recording.gps if 0 <= offset - p.offset_s <= recording.gps_max_age_s]
    if not fixes:
        return None
    return max(fixes, key=lambda p: p.offset_s).model_dump(exclude={"offset_s"}, exclude_none=True)


def _asr_interval(segment: dict, *, audio_duration_s: float, audio_offset_s: float,
                  media_duration_s: float) -> tuple[tuple[float, float] | None, str]:
    """Bound ASR by actual PCM support before mapping it to the media timeline.

    No-overlap/padded-window text is rejected, not shifted into real audio.
    Crossing a boundary is clipped only within the explicit small drift bound.
    Malformed intervals fail processing, so arbitrary invalid data is not hidden.
    """
    try:
        start, end = float(segment["start"]), float(segment["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("ASR interval needs numeric start/end") from exc
    if not all(math.isfinite(x) for x in (start, end, audio_duration_s, audio_offset_s, media_duration_s)):
        raise ValueError("ASR interval and audio bounds must be finite")
    if end <= start or audio_duration_s <= 0 or media_duration_s <= 0:
        raise ValueError("ASR interval and audio bounds must have positive duration")
    lower = max(0.0, -audio_offset_s)
    upper = min(audio_duration_s, media_duration_s - audio_offset_s)
    if start >= upper or end <= lower:
        return None, "outside_decoded_audio"
    if max(lower - start, end - upper) > ASR_DRIFT_TOLERANCE_S + 1e-9:
        return None, "boundary_exceeds_50ms_tolerance"
    bounded_start, bounded_end = max(lower, start), min(upper, end)
    status = "clipped_small_drift" if (bounded_start, bounded_end) != (start, end) else "accepted"
    return (bounded_start + audio_offset_s, bounded_end + audio_offset_s), status


def _normalize(scenario: Scenario, rec: Recording, key: str, kind: str, offset: float, end: float,
               raw: dict, metadata: dict) -> dict:
    start = rec.simulated_start + timedelta(seconds=offset)
    stop = rec.simulated_start + timedelta(seconds=end)
    loc = _location(rec, offset)
    if kind == "video":
        event = adapters.video_from_perception({**raw, "timestamp": start.isoformat(), "location": loc},
                                                session_id=rec.session_id, user_id=scenario.user_id)
        payload = event.model_dump(mode="json")
        payload["frame_ref"] = f"sha256:{metadata['source_sha256']}#t={offset:.6f}"
    else:
        # Only a single annotation containing the entire segment grounds attribution.
        annotation = next((a for a in rec.speakers if a.start <= offset and end <= a.end), None)
        event = adapters.audio_from_transcription({**raw, "recording_id": rec.id,
            "start_time": start.isoformat(), "end_time": stop.isoformat(), "location": loc,
            **(annotation.model_dump(exclude={"start", "end"}, exclude_none=True) if annotation else {})},
            session_id=rec.session_id, user_id=scenario.user_id)
        payload = event.model_dump(mode="json")
    payload["source"].update({**metadata, "recording_id": rec.id, "cache_key": key,
        "capture_offset_s": offset, "capture_end_s": end,
        "recorded_at": rec.recorded_at.isoformat() if rec.recorded_at else None,
        "capture_time": (rec.recorded_at + timedelta(seconds=offset)).isoformat() if rec.recorded_at else None,
        "simulated_start": rec.simulated_start.isoformat(), "preprocessing": PREPROCESS_VERSION})
    model = VideoObservation if kind == "video" else AudioSegment
    payload = model.model_validate(payload).model_dump(mode="json")
    # Audio becomes available only when its final segment ends; video at capture time.
    return {"kind": kind, "available_at": stop.isoformat(), "event": payload}


def process_recording(scenario: Scenario, rec: Recording, path: Path, models: LocalModels, identity: dict) -> dict:
    import cv2
    import numpy as np

    info = inspect_video(path)
    usage_before = json.loads(json.dumps(models.usage))
    key = digest(identity)
    events, warnings = [], []
    common = {"source_sha256": identity["source_sha256"], "source_video": Path(rec.video).name}
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError(f"cannot decode recording {rec.id}")
    next_sample, index = 0.0, 0
    last_pts = -1.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            pts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000 + info["video_offset_s"]
            if not math.isfinite(pts) or pts < last_pts:
                raise ValueError("decoder returned invalid/nonmonotonic frame timestamps")
            last_pts = pts
            if pts + 1e-6 < next_sample:
                continue
            classes = models.detect(frame)
            event = _normalize(scenario, rec, key, "video", pts, pts,
                {"frame_id": f"{key[:24]}-{index:08d}", "classes": classes},
                {**common, "model": models.provenance("vision"), "instance_detections": classes})
            if not classes:
                event["event"]["description"] = "No supported objects detected."
            events.append(event)
            index += 1
            next_sample = (math.floor(pts * scenario.perception.fps) + 1) / scenario.perception.fps
    finally:
        cap.release()
    if not events:
        raise ValueError("no video frames decoded")
    # Truncated streams sometimes terminate without a decoder exception.
    if info["video_end_s"] - last_pts > max(2, 2 / scenario.perception.fps):
        raise ValueError("video ended substantially before declared duration")
    if info["audio_codec"]:
        with tempfile.TemporaryDirectory(prefix="vixy-audio-") as tmp:
            wav = Path(tmp) / "audio.wav"
            command(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", "0:a:0",
                     "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
            with wave.open(str(wav)) as pcm:
                info["audio_decoded_samples"] = pcm.getnframes()
                info["audio_decoded_rate"] = pcm.getframerate()
                info["audio_decoded_duration_s"] = pcm.getnframes() / pcm.getframerate()
                samples = np.frombuffer(pcm.readframes(pcm.getnframes()), dtype="<i2").astype(np.float32) / 32768
            result = models.transcribe(wav) if info["audio_decoded_samples"] else {"segments": []}
        for segment in result.get("segments", []):
            text = str(segment["text"])
            # Whisper already applies its combined no-speech/logprob rule. A second
            # no_speech_prob-only gate discarded confident speech in real recordings.
            if not text.strip():
                continue
            try:
                interval, timing_status = _asr_interval(segment,
                    audio_duration_s=info["audio_decoded_duration_s"], audio_offset_s=info["audio_offset_s"],
                    media_duration_s=info["duration_s"])
            except ValueError as exc:
                raise ValueError(f"recording {rec.id}, ASR segment {segment.get('id')}: {exc}") from exc
            timing = {"raw_start_s": segment["start"], "raw_end_s": segment["end"], "status": timing_status}
            if interval is None:
                warnings.append(f"{rec.id}: rejected ASR segment {segment['id']} "
                                f"[{segment['start']}, {segment['end']}]s: {timing_status}")
                continue
            start, end = interval
            first = round((start - info["audio_offset_s"]) * info["audio_decoded_rate"])
            last = round((end - info["audio_offset_s"]) * info["audio_decoded_rate"])
            supported = samples[max(0, first):min(len(samples), last)]
            rms = float(np.sqrt(np.mean(supported ** 2))) if len(supported) else 0.0
            if rms < SILENCE_RMS:
                warnings.append(f"{rec.id}: rejected ASR segment {segment['id']}: near_digital_silence")
                continue
            if timing_status != "accepted":
                warnings.append(f"{rec.id}: ASR segment {segment['id']} clipped within 50ms drift tolerance")
            events.append(_normalize(scenario, rec, key, "audio", start, end,
                {"segment_id": f"{key[:24]}-{segment['id']}", "text": text, "language": result.get("language")},
                {**common, "model": models.provenance("asr"), "original_transcript": text,
                 "asr_timing": timing, "audio_rms": rms,
                 "avg_logprob": segment.get("avg_logprob"), "no_speech_prob": segment.get("no_speech_prob")}))
        if not any(e["kind"] == "audio" for e in events):
            warnings.append("no final speech segments (silence or ASR abstention)")
    else:
        warnings.append("video has no audio stream")
    events.sort(key=lambda e: (e["available_at"], e["kind"], e["event"]["event_id"]))
    usage = json.loads(json.dumps(models.usage))
    for kind in ("vision", "asr"):
        for metric in ("calls", "seconds"):
            usage[kind][metric] -= usage_before[kind][metric]
    return {"schema": CACHE_SCHEMA, "identity": identity, "key": key, "media": info,
            "events": events, "events_sha256": digest(events), "warnings": warnings,
            "model_usage": usage, "decoder": {"opencv": cv2.__version__}}


def load_cache(scenario: Scenario, rec: Recording, manifest: Path, output: Path) -> dict:
    identity = cache_identity(scenario, rec, video_path(manifest, rec), output / "models")
    key = digest(identity)
    path = output / "cache" / f"{key}.json"
    if not path.is_file():
        raise ValueError(f"no matching perception cache for {rec.id}; run process first")
    cache = json.loads(path.read_text())
    if (cache.get("schema") != CACHE_SCHEMA or cache.get("identity") != identity or cache.get("key") != key
            or cache.get("events_sha256") != digest(cache["events"])):
        raise ValueError("stale or damaged perception cache; process again")
    for row in cache["events"]:
        model = VideoObservation if row["kind"] == "video" else AudioSegment
        model.model_validate(row["event"])
    return cache


def process_scenario(manifest: Path, output: Path, *, force: bool = False, emit=print) -> dict:
    from .scenario import load_scenario
    scenario = load_scenario(manifest)
    output.mkdir(parents=True, exist_ok=True)
    models, results = None, []
    for rec in scenario.recordings:
        path = video_path(manifest, rec)
        identity = cache_identity(scenario, rec, path, output / "models")
        cache_path = output / "cache" / f"{digest(identity)}.json"
        if cache_path.exists() and not force:
            cache = load_cache(scenario, rec, manifest, output)
            emit(f"{rec.id}: verified cache, {len(cache['events'])} events")
        else:
            # Probe before loading large models, so invalid files fail cheaply.
            inspect_video(path)
            if models is None:
                models = LocalModels(scenario.perception, output / "models")
            # Downloads may have made checkpoint hashes available for the first time.
            identity = cache_identity(scenario, rec, path, output / "models")
            cache_path = output / "cache" / f"{digest(identity)}.json"
            cache = process_recording(scenario, rec, path, models, identity)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(cache, indent=2, allow_nan=False))
            temporary.replace(cache_path)
            emit(f"{rec.id}: processed {len(cache['events'])} events; warnings={cache['warnings']}")
        results.append({"recording": rec.id, "cache_key": cache["key"], "events": len(cache["events"]),
                        "warnings": cache["warnings"], "model_usage": cache["model_usage"]})
    return {"schema": "vixy.process.v1", "recordings": results, "paid_api_calls": 0}
