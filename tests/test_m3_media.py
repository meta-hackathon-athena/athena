"""Real local model edge cases, opt in with VIXY_M3_MODEL_DIR after process."""
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from vixy.perception import LocalModels, cache_identity, command, inspect_video, process_recording
from vixy.scenario import Scenario

MODEL_DIR = os.getenv("VIXY_M3_MODEL_DIR")


@pytest.mark.skipif(not MODEL_DIR, reason="set VIXY_M3_MODEL_DIR for real local model edge tests")
def test_genuine_models_silence_no_audio_empty_frames_and_stream_offset(tmp_path):
    import torch
    # CPU is always supported; MPS may not be exposed to a headless process.
    s = Scenario.model_validate({"user_id": "m3_media", "recordings": [{
        "id": "edge", "session_id": "edge", "video": "silent.mp4",
        "simulated_start": "2026-11-04T18:00:00+08:00"}]})
    models = LocalModels(s.perception, Path(MODEL_DIR))
    from ultralytics import settings
    assert settings["sync"] is False
    silent = tmp_path / "silent.mp4"
    command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=5:d=2",
             "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "2", "-c:v", "libx264", "-c:a", "aac", str(silent)])
    rec = s.recordings[0]
    cache = process_recording(s, rec, silent, models, cache_identity(s, rec, silent))
    assert len(cache["events"]) == 2 and all(e["kind"] == "video" for e in cache["events"])
    assert all(e["event"]["description"] == "No supported objects detected." for e in cache["events"])
    assert cache["model_usage"]["vision"]["calls"] == 2 and cache["model_usage"]["asr"]["calls"] == 0
    assert cache["warnings"] == ["no final speech segments (silence or ASR abstention)"]
    mute = tmp_path / "mute.mp4"
    command(["ffmpeg", "-v", "error", "-y", "-i", str(silent), "-an", "-c:v", "copy", str(mute)])
    mute_cache = process_recording(s, rec, mute, models, cache_identity(s, rec, mute))
    assert mute_cache["warnings"] == ["video has no audio stream"]
    source = Path(os.environ["VIXY_M3_SCENARIO"]).parent / "day1.mp4"
    shifted = tmp_path / "shifted.mp4"
    command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=5:d=12",
             "-itsoffset", "2", "-i", str(source), "-map", "0:v", "-map", "1:a", "-t", "12",
             "-c:v", "libx264", "-c:a", "aac", str(shifted)])
    shifted_cache = process_recording(s, rec, shifted, models, cache_identity(s, rec, shifted))
    info = inspect_video(shifted)
    assert info["audio_offset_s"] >= 1.8
    audio = [e for e in shifted_cache["events"] if e["kind"] == "audio"]
    assert audio and "Alex is allergic to peanuts" in audio[0]["event"]["text"]
    for row in audio:
        event = row["event"]
        assert event["source"]["capture_offset_s"] >= info["audio_offset_s"]
        delta = (datetime.fromisoformat(event["start_time"].replace("Z", "+00:00")) - rec.simulated_start).total_seconds()
        assert abs(delta - event["source"]["capture_offset_s"]) < .001
        assert row["available_at"] == datetime.fromisoformat(event["end_time"].replace("Z", "+00:00")).isoformat()
    if torch.backends.mps.is_available():
        import cv2
        models.settings = models.settings.model_copy(update={"vision_device": "mps"})
        frame = cv2.imread(str(source.parent / "public-bus.jpg"))
        assert any(d["class"] == "person" for d in models.detect(frame))
