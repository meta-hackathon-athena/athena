"""Generate an audiovisual test, never a substitute for user-recording acceptance."""
from __future__ import annotations

import json
import shutil
import urllib.request
from pathlib import Path

from .perception import command

IMAGE_URL = "https://raw.githubusercontent.com/ultralytics/assets/main/im/bus.jpg"


def generate(directory: Path, user_id: str = "m3_synthetic") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / "public-bus.jpg"
    if not image.exists():
        urllib.request.urlretrieve(IMAGE_URL, image)
    speech = ["Alex is allergic to peanuts.", "I am buying peanuts for Alex."]
    recordings = []
    for i, text in enumerate(speech, 1):
        video = directory / f"day{i}.mp4"
        if video.exists():
            raise ValueError("fixture output already exists; choose a new directory")
        audio = directory / f"day{i}.aiff"
        if shutil.which("say"):
            command(["say", "-v", "Samantha", "-r", "145", "-o", str(audio), text])
        elif shutil.which("espeak"):
            audio = audio.with_suffix(".wav")
            command(["espeak", "-s", "145", "-w", str(audio), text])
        else:
            raise ValueError("synthetic fixture requires macOS say or espeak (local TTS)")
        command(["ffmpeg", "-nostdin", "-v", "error", "-y", "-loop", "1", "-i", str(image),
                 "-i", str(audio), "-filter_complex", "[1:a]adelay=1000:all=1,apad[a]", "-map", "0:v", "-map", "[a]",
                 "-vf", "scale=640:-2", "-r", "5", "-t", "34", "-c:v", "libx264", "-preset", "fast",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(video)])
        recordings.append({"id": f"day{i}", "session_id": f"{user_id}_day{i}", "video": video.name,
            "simulated_start": f"2026-11-0{i+1}T18:00:00+08:00", "recorded_at": None, "dream_after": True,
            "speakers": ([{"start": 0, "end": 34, "speaker_id": "synthetic_wearer", "speaker_is_wearer": True}]
                         if i == 2 else [])})
        audio.unlink()
    scenario = {"schema_version": "vixy.scenario.v1", "user_id": user_id, "disposable": True,
                "playback_speed": 0, "perception": {"vision_model": "yolo11n.pt", "asr_model": "base.en"},
                "recordings": recordings}
    manifest = directory / "scenario.json"
    manifest.write_text(json.dumps(scenario, indent=2))
    (directory / "FIXTURE.md").write_text(
        "Generated test: repeated public Ultralytics bus photograph + locally synthesized speech.\n"
        f"Image source: {IMAGE_URL}\n"
        "No user recording, actual wearable footage, recorded GPS, or natural conversation.\n"
        "Day 1's speaker is unknown. Day 2's synthetic wearer annotation is explicit ground truth.\n"
        "The manifest supplies times, never transcription results or backend answers.\n")
    return manifest
