# Athena event contracts and conventions (Milestones 1 and 2)

Milestone 2 adds no change to the event contracts. New versioned contracts:

| contract | where |
|---|---|
| `vixy.graph.v2` | graph schema (additive over v1; explicit `python -m vixy migrate`) — `GRAPH_SCHEMA.md` |
| `/v1` HTTP API, `vixy.api.v1`, `vixy.error.v1` | `API.md` |
| `vixy.ask.v1` | answer format of `/v1/ask` and `VixyRuntime.ask` — `API.md` |
| `vixy.semantic.v2` | Dreamer claim extraction rules (`extracted_by = rules:vixy.semantic.v2`) |
| `vixy.extract.v1` | model extraction prompt/tool version (`extracted_by = model:<id>:vixy.extract.v1`) |
| `vixy.recall.v1` | agent recall rules (`AgentLog.reasoning = rules:vixy.recall.v1`) |

Epistemic statuses used everywhere (graph, decisions, API): `observed`, `reported`,
`self_reported`, `hypothesis`, `inferred`, `operational`. Reported speech is never presented
as verified fact; a Note's `speaker_id` is a diarization label, never a person's name.

Context Engine's v1 schemas are the wire contract, and they are **unchanged**:

- `vixy.audio_segment.v1`
- `vixy.video_observation.v1`
- `vixy.context_snapshot.v1`
- `vixy.dreamer_event.v1`

Their `extra="forbid"` rule still rejects unknown fields. The fields Context Engine has no slot for travel in the free-form `source` object, in one validated shape: `vixy.source.v1` (`vixy/contracts.py`). This is a backward-compatible extension, so stored events, payload hashes and Aryan's tests are unaffected. A first-class `Detection.count` / `AudioSegment.speaker_is_wearer` would need a CE `v1.1` bump. That's worth doing once perception is real (Milestone 3), but nothing in Milestone 1 required it.

## Identity

| Concept | Convention |
|---|---|
| User | `VIXY_USER_ID`, matching `[A-Za-z0-9_-]+` (default `demo`). One FalkorDB graph per user: `vixy_<user_id>`, owner recorded in `(:VixyMeta).user_id`. |
| Session | Context Engine `session_id`. A user owns the sessions in `VIXY_SESSION_IDS` (default: exactly the user id, the Milestone 1 single-user mapping). The runtime rejects events from any other session **before** they reach Context Engine. |
| Future separation | Give each device or recording session its own id (e.g. `demo.glasses-01`) and list it under the user. Events keep their `session_id`, and graph nodes store both `session_id` and `user_id`, so provenance survives the split. |
| Event id | Context Engine `event_id`, stable across retries. Video: `<stream>:<frame_id>` (default stream `cam0`, Ara's convention). Audio: `asr:<recording_id or stream>:<segment_id>` (Aryan's adapter convention). |
| Graph ids | Derived deterministically: Note `note_<sha1(event_id)>`; Visit `vis_<sha1(user:first_event_id:start)>`; Commitment `cmt_<sha1(title:start)>`; Question `qst_<sha1(dedup_key)>`; Decision/AgentLog `dec_<sha1(dedup_key:ts)>`; Person `per_<name>`; memories by content (GRAPH_SCHEMA.md). |
| Product name | **Athena** (formerly Vixy). Internal identifiers keep the `vixy` name: the wake word implemented by the agent is still "Vixy, …", and "Veyra" is still accepted as a legacy wake word; `VEYRA_*` environment variables are honoured as fallbacks for their `VIXY_*` replacements. |

## Time

- On the wire: ISO-8601 with an explicit offset. Naive timestamps and bare epoch numbers are rejected by Context Engine; `memory.frames.parse_ts` now rejects naive strings too, instead of reading them as machine-local time.
- Storage: Context Engine uses integer UTC µs; the graph uses epoch ms plus UTC ISO strings.
- `local_date`, `dow` and `start_min` use `VIXY_TZ` (default `Asia/Singapore`).
- Audio availability time is `end_time` (Context Engine window rule). A Note's `ts_ms` is its `start_ms`.

## Location

Context Engine `Location{latitude, longitude, accuracy_m?}`. It's optional on every event, never fabricated, and carried through to the agent (`latitude`/`longitude`) and the graph (`lat`/`lon`). Bad GPS is the same as no GPS.

## Speakers

`speaker_id` is passed through exactly as ASR gives it, or is absent. In the graph an absent id becomes `'unknown'`.

`source.speaker_is_wearer` is set only from an explicit upstream boolean (VAD or diarization):

| Flag | Agent `speaker_is_user` | Graph `speaker_role` |
|---|---|---|
| `true` | `True` | `wearer` |
| `false` | `False` | `other` |
| absent | `None` | `unknown` |

**An unknown speaker is never the wearer.** In practice:

- The agent does not record their commitments.
- The Episode Writer does not create reminders or user preferences from their words. The phrasing is kept as `Note.intent_hint`.
- The Dreamer keeps their statements as reported speech with lower confidence.

## `vixy.source.v1` (inside `event.source`)

```json
{"schema": "vixy.source.v1", "user_id": "demo", "adapter": "vixy.perception_frame.v1",
 "pipeline": "vixy-perception", "stream": "cam0", "frame_id": "frame_001", "counts": {"person": 2}}
{"schema": "vixy.source.v1", "user_id": "demo", "adapter": "vixy.asr_segment.v1", "pipeline": "vixy-asr",
 "stream": "mic0", "recording_id": "rec-1", "segment_id": "seg-7", "speaker_is_wearer": false, "language": "en"}
```

Rules:

- `counts` holds only counts the detector actually reported. The Dreamer's parser defaults a missing count to 1; the adapter does not pass that default on.
- Other keys may be added by upstream pipelines. None of these keys is ever null (an unknown value is absent).

## Adapters (`vixy/adapters.py`)

| From | To | Notes |
|---|---|---|
| Perception frame `{frame_id, timestamp, classes:[{class, count?, confidence}], latitude?, longitude?, accuracy_m?}` | `VideoObservation` | Reuses Ara's `prod_dreamer_model.frames.parse_frame` + `to_video_observation` (now with a `stream` parameter). `description` is the synthetic detection caption (`"person x2, cup"`); no scene details are invented. Confidence is kept, rounded to 4 decimals by Ara's parser. |
| Finalized ASR segment `{segment_id, recording_id?, start_time, end_time, text, speaker_id?, speaker_is_wearer?, confidence?, language?, latitude?, longitude?}` | `AudioSegment` | Whisper-style relative segments `{id, start, end, text}` reuse Aryan's `audio_from_relative_segment`. |
| Context Engine event | Agent input (`VisionObservation` / `AudioObservation` dict) | `event_id` becomes `frame_id`/`segment_id`; `text`→`transcript`; `start_time`/`end_time`→`timestamp_start`/`timestamp_end`; `detections`+`source.counts`→`classes`; `location`→`latitude`/`longitude`; `source.speaker_is_wearer`→`speaker_is_user`. |
| Context Engine video event | `memory.frames.Frame` for Samu's Segmenter | `frame_id` = `event_id`. |

## Errors (runtime `EventOutcome.status`)

| status | meaning | side effects |
|---|---|---|
| `processed` | accepted by Context Engine; Note/Visit written; agent ran | yes, exactly once |
| `duplicate` | identical re-send of a fully processed event | none |
| `recovered` | the event was already in Context Engine but its side effects had not finished; they are completed now | completed once |
| `rejected` (stage `ingest`) | invalid payload, or a session not mapped to this user | none; not stored |
| `conflict` (stage `ingest`) | same `event_id` with different content (Context Engine 409) | none |
| `failed` (stage `episode` or `agent`) | stored in Context Engine, but a graph write or agent memory read failed | recorded in the runtime ledger; `recover()` (also run at start-up) finishes it |

The runtime never logs transcript text, only ids and statuses.

## Context for the agent

`ActiveAgent(context_provider=runtime.context_at)`: the agent can pull the `ContextSnapshot` for the window that ends at an event's time (`VIXY_CONTEXT_WINDOW_S`, default 300 s).

In Milestone 1 it is used on the LLM path. When the rules mark an utterance uncertain, the rendered Context Engine prompt (recent transcript and scene, quoted as data) goes into the request. The rule path never builds a snapshot.


## M3 recording, cache and replay contracts

`vixy.scenario.v1` is validated with unknown fields forbidden. It contains `user_id`, `disposable`, `playback_speed`, `tick_seconds`, perception settings and one or more recordings (`id`, `session_id`, `video`, `simulated_start`, optional `recorded_at`, GPS, speaker annotations and `dream_after`). Optional `dream_boundaries` and `end_time` use explicit UTC offsets. Recording IDs are unique. Annotation intervals cannot overlap. The [example manifest](../demo/scenario.example.json) and [workflow](../demo/README.md) specify the complete shape.

The CE v1 schemas remain unchanged. Detection counts are retained in `source.counts`; per-instance scores in `source.instance_detections`; optional wearer attribution in `source.speaker_is_wearer`. Missing location and identity are never filled from detector classes, voice guesses or file metadata. Unknown ASR confidence stays null; `avg_logprob` and `no_speech_prob` remain clearly labelled source scores.

Perception provenance additionally carries `source_sha256`, source video basename, `recording_id`, `cache_key`, preprocessing version, model ID/version/checkpoint SHA/device, `capture_offset_s`, `capture_end_s`, `recorded_at`, `capture_time`, and `simulated_start`. `original_transcript` preserves ASR's exact segment text; the canonical CE text is trimmed by the existing adapter. `frame_ref` is `sha256:<video hash>#t=<media seconds>`. Video frame IDs and ASR segment IDs are derived from recording/cache identity and inference sample/segment index. Different configurations create new IDs; retransmission of identical cached IDs is idempotent.

`vixy.perception_cache.v1` holds only normalized events, immutable identity metadata, media inspection, warnings, actual model usage and an events digest. Events have `kind`, `available_at` and a CE `event`; audio availability is end time. Source bytes and dependency/config/version identity are checked before replay/reset; an events hash detects accidental cache damage. Cache files are trusted local data, not a signed interchange format.

`vixy.replay.v1` JSONL rows carry simulated time, type, recording/progress, source event ID, capture time/offsets, objects/transcript, runtime outcome, Note/Visit IDs, decisions and recall counters. Boundary rows expose episode and Dreamer results; ask rows contain standard grounded answers. The latest summary is `vixy.replay_summary.v1`. JSONL includes private transcript text and is always written under ignored output directories.

Ticks are operational inputs with distinct IDs, persisted in the runtime ledger; they never masquerade as camera/audio observations in CE. The default interval is one minute. Replay uses one global availability order and virtual day transitions; positive playback speed applies inside recordings. A pause sentinel and stable ledger checkpoints support pause/resume and interrupted reruns. Concurrent replay processes for the same user/state are unsupported.

M3 provenance explicitly uses null `recorded_at`/`capture_time` when the real recording time is unknown; this differs from the older optional source fields, which are omitted when unknown. Named model checkpoints must remain available for cache fingerprint verification; their contents are hashed without loading the models.


### ASR interval repair (preprocessing v2)

Perception now measures the extracted PCM sample count/rate. A finalized speech segment must overlap that actual decoded audio interval after accounting for stream origin/offset. Fully out-of-support segments are rejected with warnings. Boundary crossings are clipped only within 50 ms; larger crossings are rejected, not moved into valid audio. Reversed, non-finite or non-numeric intervals fail with recording/segment IDs. The PCM waveform must also contain more than near-digital-silence energy in the proposed interval (RMS ≥ 1e-5). This is a silence guard, not speaker recognition or a guarantee of ASR correctness.

Whisper already combines no-speech score with token log probability; the duplicate score-only filter was removed because it discarded confident speech. Accepted events preserve raw ASR timestamps, adjustment status and interval RMS in source provenance. Stream-relative durations avoid counting leading MP4 empty edits as observed media. Video truncation is checked against the video stream end, not a potentially longer audio/container duration. Preprocessing v2 invalidates old caches; `process` creates matching new caches without changing recordings.
