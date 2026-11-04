# Athena backend API — `/v1` (Milestone 2)

The JSON interface for the future Athena app (Milestone 4) and the simulator (Milestone 3).
Code: `vixy/api.py` (routes) and `vixy/ask.py` (questions). Start it with:

```bash
cd vixy                                   # repository root
FALKORDB_URL=redis://localhost:6390 uv run python -m vixy --user demo_m2 serve --port 8765
curl -s localhost:8765/v1/ask -d '{"question": "What do you remember about Alex?"}'
```

- Local development only: no authentication, bound to `127.0.0.1` by default, one runtime (one
  user) per process. Requests are handled one at a time (the runtime is single-threaded).
- Every response is a JSON object with a `schema` key: `vixy.api.v1` on success,
  `vixy.error.v1` on failure (`{"error": {"code", "message"}}`, HTTP 400/404/413/500). Error
  messages never contain transcript text or stack traces.
- Times are ISO-8601 with an explicit offset; a naive time is rejected (`400`).
- Versioning: breaking changes get a new path prefix (`/v2`). Fields may be added to `v1`
  responses; clients must ignore unknown fields.
- All `GET` routes and `/v1/ask` are read-only: they use `GRAPH.RO_QUERY` queries with fixed
  Cypher. The only writes are `POST /v1/events/*` (the normal ingestion path) and
  `POST /v1/dream` (the Dreamer job).

## Routes

| method | path | body / query | returns |
|---|---|---|---|
| GET | `/v1/health` | | `{ok, graph, graph_schema}` |
| GET | `/v1/status` | | `{status}`: event counts, ledger, label counts, agent metrics (incl. `recall` stats, `llm_calls`), `ask_model_usage` |
| POST | `/v1/ask` | `{question, conversation_id?, now?}` | `{result}`: a `vixy.ask.v1` object (below) |
| GET | `/v1/memories` | `?about=&status=active,unconfirmed&limit=` | `{memories, patterns}` |
| GET | `/v1/memories/{id}` | | `{memory}`: statement, structured claim fields, `derived_from`, `people`, `conflicts_with`, `supersedes`, `superseded_by`, `evidence[]` each with `original` = the context-engine record (`event_id`, `payload_sha256`, `matches`, `text`) |
| GET | `/v1/people/{id}` | | `{person}`: name, status, `possibly_same_as`, memory ids |
| GET | `/v1/timeline` | `?date=YYYY-MM-DD` or `?from=ISO&to=ISO` (≤ 31 days) | `{from, to, items}`: Visits and Notes by time |
| GET | `/v1/decisions` | `?action=NOTIFY&since=ISO&limit=&delivered=1` | `{decisions}` (AgentLog, newest first) |
| GET | `/v1/decisions/{id}` | | `{decision}`: action, status, message, reason, `reasoning`, `trigger_event_id`, `concerns`, `supporting_memories`, and `memories` (each fully explained, with evidence) |
| POST | `/v1/events/audio` | a CE `AudioSegment`, or `{"asr": {segment_id, start_time, end_time, text, speaker_id?, speaker_is_wearer?, ...}}` | `{outcome}` (`EventOutcome`, see CONTRACTS.md) |
| POST | `/v1/events/video` | a CE `VideoObservation`, or `{"perception": {frame_id, timestamp, classes, latitude?, longitude?}}` | `{outcome}` |
| POST | `/v1/dream` | | `{dream}`: the Dreamer run summary (offline unless the runtime was given a model) |

## `vixy.ask.v1`

```json
{
  "schema": "vixy.ask.v1",
  "question": "What do you remember about Alex?",
  "conversation_id": "conv_3f2a…",            // pass it back for follow-ups
  "now": "2026-11-03T21:00:00+08:00",         // the clock used for "yesterday", "upcoming", ...
  "status": "answered",                        // answered | insufficient_evidence | ambiguous | unsupported
  "answer": "About Alex: Alex is allergic to peanuts (reported by someone else; not verified, confidence 0.65, first said Monday 2 Nov).",
  "interpretation": {"type": "about_entity", "people": ["Alex"], "topic_keywords": [],
                     "time_range": null, "follow_up": false},
  "items": [{"id": "ltm-claim-…", "label": "LongTermMemory", "layer": "inferred",
             "epistemic_status": "reported", "statement": "Alex is allergic to peanuts.",
             "subject": "Alex", "predicate": "allergic_to", "object": "peanut", "polarity": true,
             "reported_by": ["other"], "contested": false, "confidence": 0.65, "status": "active",
             "time": "2026-11-02T12:30:00+08:00", "source_ids": ["note_…"]}],
  "sources": [{"id": "note_…", "label": "Note", "source_event_id": "asr:rec-…:m2seg003",
               "speaker_role": "other", "speaker_id": "spk_2", "time": "2026-11-02T12:30:00+08:00",
               "statement": "Alex is allergic to peanuts.", "verified": true}],
  "answered_by": "rules:vixy.ask.v1",          // or "model:<id>" when a model rephrased a grounded answer
  "model_usage": null
}
```

- `interpretation.type`: `day_activity` ("what did I do / where did I go …"), `schedule`
  ("when am I meeting …"), `what_said` ("what did Marta tell me about …"), `about_entity`
  ("what do you remember about …"), `why_decision` ("why did you remind me …"), `evidence`
  ("what evidence supports that memory?"), `search` (anything else: keyword retrieval).
- `items[].epistemic_status`: `observed` (Note/Visit), `reported`, `self_reported`,
  `hypothesis`, `inferred`, `operational` (Commitment/AgentLog). The app must show reported
  and hypothesis items as unverified.
- `sources[].verified`: the Note's text and SHA-256 were re-checked against the original
  context-engine event (`true`), the check failed (`false`), or it could not be done (absent).
- `status: insufficient_evidence` means nothing in the graph supports an answer; `answer` says
  so in words. The service never fills the gap.
- Follow-ups: send the same `conversation_id`. "that memory" / "this" / "him" resolve to the
  previous answer's memory, decision or people. The context is stored in the runtime ledger
  (SQLite), so it survives restarts.
- Speaker ids are diarization labels, not names: a "what did X tell me" answer always says
  that it cannot confirm who spoke.

## Decisions (`/v1/decisions/{id}`)

```json
{"decision": {"id": "dec_…", "ts_ms": 1793784003000, "action": "NOTIFY", "status": "done",
  "priority": "normal", "confidence": 0.65, "trigger": "audio",
  "trigger_event_id": "asr:rec-m2-2026-11-03:m2seg007",
  "message": "Someone mentioned yesterday that Alex is allergic to peanuts. I haven't verified it. You may want to check with Alex before buying.",
  "reason": "remembered allergic_to 'peanut' conflicts with what is planned (peanut); reported memory ltm-claim-…",
  "reasoning": "rules:vixy.recall.v1",
  "supporting_memory_ids": ["ltm-claim-…"],
  "supporting_memories": [{"id": "ltm-claim-…", "statement": "Alex is allergic to peanuts.",
                           "epistemic_status": "reported", "evidence": [{"id": "note_…", "source_event_id": "asr:…"}]}],
  "concerns": [{"label": "LongTermMemory", "id": "ltm-claim-…"}, {"label": "Note", "id": "note_…"}],
  "memories": [{"id": "ltm-claim-…", "statement": "…", "evidence": [ … ]}]}}
```


## M3 local video commands

The existing HTTP event, dream, memory, decision and `/v1/ask` routes serve the simulator's actual runtime. M3 adds local CLI controls; it does not add a second API or full frontend.

| Command | Purpose |
|---|---|
| `vixy process --scenario <manifest> [--output <directory>] [--force]` | Decode MP4, run local detector/ASR, validate and cache observations |
| `vixy replay --scenario <manifest> [--fresh] [--speed N] [--pause-file <path>] [--question <text>]` | Single- or multi-day replay through the real integrated backend |
| `vixy reset-replay --scenario <manifest>` | Reset only the explicitly named disposable replay user/session state |
| `vixy fixture --directory <local inputs> --fixture-user m3_synthetic` | Generate a public-image/local-TTS audiovisual test fixture |
| `vixy --user <id> --state-dir <output>/state dream` | Offline consolidation at the runtime's simulated time |
| `vixy --user <id> --state-dir <output>/state decisions / memories / ask / serve` | Existing inspection, question and HTTP commands |

Run with `uv run --extra perception python -m vixy ...` after `uv sync --frozen --extra perception`; FFmpeg/ffprobe must be on PATH. The same perception dependency versions are required for cache identity verification, but replay runs no detector or ASR inference. Inputs are local files; they are not uploaded. See [demo/README.md](../demo/README.md) for exact commands, safe reset rules and pause behavior.

Default output is `demo/outputs/<scenario user>/`; `--state-dir` is a global option before the subcommand. Reset/`--fresh` needs `disposable:true`, a user beginning `test_` or `m3_`, a matching replay ownership marker for existing state/graph, and the `vixy` graph prefix. It does not unlink an embedded RDB or touch a Docker volume. `vixy_demo` is excluded by these rules.

Perception and replay are offline inference paths and cannot opt into paid model calls. Existing programmatic M2 reasoner/extractor/answer-model hooks and `dream --online` remain opt-in and require separate billing authorization. Shared credential: `MODEL_API_KEY`, with explicit process environment > `VIXY_ENV_FILE` > `DREAMER_ENV_FILE` > repository-root `.env` > legacy fallback files. Contributor models are forbidden in unified wearable paths even when a legacy synthetic-data override is present.

Meta readiness was verified using one metadata-only `GET /v1/models`: HTTP 200 and `muse-spark-1.3` listed; no inference or upload occurred. The endpoint and bearer auth are documented in [Meta's API reference](https://dev.meta.ai/docs/api-reference/models). Generation availability, tool calling and paid usage have not been tested live.

Standalone Dreamer also defaults offline with a credential present. `dream --online` and `all --online` explicitly enable the paid tool loop; `--model-extraction` is a separate explicit opt-in for budgeted extraction. `--offline` disables only the tool loop, preserving the M2 `--offline --model-extraction` interface.
