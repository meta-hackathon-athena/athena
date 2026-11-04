# Video-driven wearable simulator

MP4 perception runs locally. Replay sends cached observations through the actual Context Engine, Episode Writer, Active Agent, unified FalkorDB, Dreamer and Ask service. Cache files contain observations only.

## Your recordings

```bash
cd vixy                                   # repository root
uv sync --frozen --extra perception
# FFmpeg and ffprobe must be on PATH (macOS: brew install ffmpeg).
mkdir -p demo/inputs/scenario_01
cp demo/scenario.example.json demo/inputs/scenario_01/scenario.json
# Put your own day1.mp4 and day2.mp4 in that directory. Edit scenario.json.
uv run --extra perception python -m vixy process --scenario demo/inputs/scenario_01/scenario.json
uv run --extra perception python -m vixy replay --scenario demo/inputs/scenario_01/scenario.json --fresh \
  --question "What do you remember about Alex?"
# Replay the same events without inference; no duplicate backend writes:
uv run --extra perception python -m vixy replay --scenario demo/inputs/scenario_01/scenario.json
```

`demo/inputs/**` and `demo/outputs/**` are ignored, including manifests with private annotations, generated MP4s, model weights, caches, state and transcripts. Only the safe example outside `inputs/` is committed. Never force-add recordings.

```text
demo/
  inputs/scenario_01/
    day1.mp4
    day2.mp4
    scenario.json
  outputs/<user_id>/
    models/                 # downloaded once; local CPU/MPS/CUDA inference
    cache/<fingerprint>.json # normalized observations, model provenance, media inspection
    state/                  # CE SQLite, processing/timer ledger, embedded graph if used
    replay.jsonl            # append-only monitor; includes private transcript text
    summary.json            # latest structured backend results
  scenario.example.json
  README.md
```

## Manifest

The example supports user identity, distinct per-recording sessions, relative or absolute MP4 paths, explicit-offset simulated start times, optional actual `recorded_at`, speed, GPS, speaker annotations and consolidation boundaries. `recorded_at:null` means unknown: file creation metadata is reported separately and never substituted. Capture offsets are relative to the media timeline, not a fabricated recording date.

- `playback_speed:0`: deterministic fast virtual time. Positive speed paces media inside each recording; gaps between recordings advance through virtual ticks without sleeping through the day. `--speed` overrides it.
- `tick_seconds`: timer checks during gaps (default 60, range 1–300). Context Engine uses its own existing `SimulatedClock`; Active Agent uses the same event/tick time; Dreamer receives boundary time. Audit creation/update fields can still use wall time.
- `end_time`: optional explicit-offset time after all recordings, for testing later reminders.
- `dream_after:true`: close this recording's episode and consolidate. Alternatively `dream_boundaries:["2026-11-02T23:00:00+08:00"]`. All events are globally ordered by availability; a transcript arrives at segment end.
- `gps:[{"offset_s":2,"latitude":1.3,"longitude":103.7,"accuracy_m":10}]`: supplied GPS fixes only. The last fix is used up to `gps_max_age_s` (default 30). No interpolation, map lookup or invented location.
- `speakers:[{"start":0,"end":12,"speaker_id":"annotated_wearer","speaker_is_wearer":true}]`: only use when you know who spoke. A segment must fit entirely within one annotation; otherwise identity remains unknown. No automatic diarization or face/voice identification.
- `language:null` selects Whisper language detection; use a multilingual model (`base`, `small`, etc.) for languages other than English. Default `base.en` is English only.
- `vision_device:"mps"` selects Apple acceleration when available; CPU is the tested default. CUDA indexes are supported by Ultralytics. ASR uses CPU or CUDA; Whisper MPS is not recommended by this implementation.

ASR segment times are model estimates, not word alignment. `confidence` stays absent because Whisper log probabilities are not calibrated confidence; original text, `avg_logprob` and `no_speech_prob` are retained in source metadata. Descriptions only list supported object classes; empty detections do not mean an empty physical scene.

## Storage, inspection and controls

Set `FALKORDB_URL=redis://localhost:6390` for the existing Docker server, or leave it blank for embedded single-process storage. Both use `vixy_<scenario user>`. SQLite and local graph state are under `demo/outputs/<user>/state`. Different sessions keep separate episode segmentation and correct Context Engine context.

```bash
# Reuse the same user, state directory and FALKORDB_URL for inspection:
uv run --extra perception python -m vixy --user m3_scenario_01 --state-dir demo/outputs/m3_scenario_01/state dream
uv run --extra perception python -m vixy --user m3_scenario_01 --state-dir demo/outputs/m3_scenario_01/state decisions --limit 20
uv run --extra perception python -m vixy --user m3_scenario_01 --state-dir demo/outputs/m3_scenario_01/state memories --about Alex
uv run --extra perception python -m vixy --user m3_scenario_01 --state-dir demo/outputs/m3_scenario_01/state ask "What do you remember about Alex?"
uv run --extra perception python -m vixy --user m3_scenario_01 --state-dir demo/outputs/m3_scenario_01/state serve --port 8765
# Explicitly reset only this named disposable session/user graph:
uv run --extra perception python -m vixy reset-replay --scenario demo/inputs/scenario_01/scenario.json
```

Reset and `--fresh` require `disposable:true`, a user beginning `m3_` or `test_`, the `vixy` graph prefix, and matching ownership markers for existing state/graphs. They never delete an embedded RDB file or a Docker volume. Regular replay is nondestructive and resumes safely from stable event IDs and boundary/tick checkpoints. Keep the same manifest and cache to resume; changing model/settings/media creates new IDs and requires deliberate scenario ownership/reset decisions.

Use `replay --pause-file demo/outputs/PAUSE`: create that file to pause, remove it to resume. Ctrl-C stops the CLI; repeat the same replay command to resume persisted event processing. Pacing is best-effort: accelerated replay skips day gaps and the CLI displays activity rather than rendering video.

## Generated audiovisual acceptance fixture

```bash
uv run --extra perception python -m vixy fixture --directory demo/inputs/synthetic --fixture-user m3_synthetic
uv run --extra perception python -m vixy process --scenario demo/inputs/synthetic/scenario.json
uv run --extra perception python -m vixy replay --scenario demo/inputs/synthetic/scenario.json --fresh
```

The fixture combines a public Ultralytics test photo with locally synthesized speech (`say` on macOS or `espeak` on Linux), into two 34-second MP4s. It downloads the public image and model weights once. The photo is a repeated still, not real wearable motion; the speech is generated. No transcript is supplied to ASR, no memory or notification is scripted. User-recording acceptance is pending until actual recordings are provided.

## Reproducible tests

`make smoke-av` runs this whole level in one command: it generates a new fixture, processes it with the local models, runs the acceptance/media tests on embedded and server storage, runs the MP4 replay smoke test, and cleans up. The manual equivalent:

```bash
MODEL_API_KEY='' BASETEN_API_KEY='' VIXY_TEST_FALKORDB_URL=redis://localhost:6390 \
 VIXY_M3_SCENARIO=demo/inputs/synthetic/scenario.json \
 VIXY_M3_OUTPUT=demo/outputs/m3_synthetic \
 VIXY_M3_MODEL_DIR=demo/outputs/m3_synthetic/models \
 uv run --extra perception pytest -q
```

The opt-in acceptance tests reset only the generated fixture's named disposable graph. They run real caches on embedded and Docker FalkorDB, exercise actual HTTP `/v1/ask`, verify evidence and duplicate replay, and prohibit perception model loading during replay. The media tests use actual models on silence, missing audio, black frames and shifted audio, plus MPS when available. No key or paid inference is required.


## Timestamp boundary diagnostics and real-recording verification

Preprocessing v2 measures decoded PCM duration instead of trusting Whisper's padded-window endpoint. It rejects segments outside actual audio and overhangs larger than 50 ms, reporting the recording/segment ID and reason. Small boundary drift is clipped and recorded. Digital-silence intervals are rejected using waveform energy; no-speech score alone does not discard otherwise confident Whisper output. Re-run `process` after upgrading; the versioned cache key prevents reuse of the earlier result.

The repair was validated on the user's actual two-day recordings under a separate local manifest `demo/inputs/scenario_01/scenario.repair-validation.json`, user `m3_real_repair_20261104`, and state `demo/outputs/m3_real_repair_20261104/state`. That private manifest and all actual-recording outputs are ignored. No wearer annotation was invented. The actual run created observed Notes and ran Dreamer, but produced no durable memories or notifications; keyword queries can still cite the original Notes.

```bash
# Inspect the verified embedded replay (ensure FALKORDB_URL is blank):
FALKORDB_URL='' uv run --extra perception python -m vixy \
  --user m3_real_repair_20261104 --state-dir demo/outputs/m3_real_repair_20261104/state \
  timeline 2026-11-02
# Opt-in generic real-recording acceptance; use only an explicitly disposable identity:
VIXY_REAL_M3_SCENARIO=demo/inputs/scenario_01/scenario.repair-validation.json \
 VIXY_REAL_M3_OUTPUT=demo/outputs/m3_real_repair_20261104 \
 uv run --extra perception pytest tests/test_m3_real_recordings.py -q
```

That acceptance test verifies real cache intervals, Note-to-CE text/hash provenance, Dreamer runs, actual HTTP Ask sources and duplicate replay. It does not require a prechosen memory or notification. The earlier generated-fixture acceptance remains separate and still verifies memory-aware decisions from its own synthetic evidence.
