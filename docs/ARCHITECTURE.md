# Athena architecture (monorepo, Milestones 1–3)

This is what is built and tested now. The target design is [VIXY_TARGET_ARCHITECTURE_EN.md](VIXY_TARGET_ARCHITECTURE_EN.md) in this canonical directory.
The monorepo layout comes first. Then Milestone 2 (intelligence, memory and reasoning), the Milestone 1 pipeline it
builds on, and Milestone 3 perception and replay.

## Monorepo layout

All backend components live in this repository, one uv workspace with one `uv.lock`. They are
imported as installed packages, so code doesn't refer to any directory outside the repository.

| Path | Component (owner) | Python package(s) | Installed as |
|---|---|---|---|
| `vixy/` | integrated runtime: Episode Writer, CLI, `/v1` API, ask, retrieval, M3 perception/replay | `vixy` | workspace root (not built; on `sys.path` via the repo root and pytest's `pythonpath`) |
| `agent/` | Active Agent (Samu) | `agent` | workspace root |
| `memory/` | operational and episodic graph memory, schema `vixy.graph.v2` (Samu) | `memory` | workspace root |
| `components/context-engine/` | Context Engine: validation, SQLite store, rolling context (Aryan) | `context_engine` | workspace member `vixy-context-engine` (editable) |
| `components/dreamer-model/` | Dreamer: consolidation, semantic memory extraction (Ara) | `prod_dreamer_model` | workspace member `prod-dreamer-model` (editable) |
| `frontend/` | reserved for the app (Ara) | | not part of the backend |
| `docker-compose.yml` | one FalkorDB server (port 6390), one graph `vixy_<user>` per user | | |

The end-to-end flow is unchanged from M3:

```text
MP4 / camera observations
  -> YOLO11n + Whisper            (vixy/perception.py; local, on this machine)
  -> Context Engine + SQLite      (components/context-engine)
  -> Episode Writer               (vixy/episode_writer.py)
  -> Active Agent                 (agent/)
  -> FalkorDB vixy_<user>         (memory/, docker-compose.yml)
  <-> Dreamer                     (components/dreamer-model, vixy/dream_job.py)
  -> memory retrieval / /v1/ask / notifications   (vixy/retrieval.py, vixy/ask.py, vixy/api.py)
```

Where things run:

- **Local, on this machine:** perception (YOLO11n, Whisper), Context Engine, Episode Writer, Active Agent rules and recall, FalkorDB, Dreamer (offline rules), and `/v1/ask` (rules). This is the default and needs no key.
- **Can use Meta Muse** (`MODEL_API_KEY`, opt-in only, separately authorized):
  - Dreamer `dream --online` tool loop, and `--model-extraction`
  - the agent's recall reasoner and `/ask` rephrasing, when a model is passed to `VixyRuntime`
- **Not verified:** none of the Muse paths has been exercised with a live inference call. Only a metadata `GET /v1/models` was made (M3). Tests use `FakeStructuredModel`.

## Milestone 2: reasoning architecture

```text
 new observation ─► Context Engine ─► Episode Writer (Note/Visit) ─► ActiveAgent.handle
                                                                        │
     frames: ContextTracker → place/tick triggers → M1 rules (reminders, heads-ups)   no LLM, no retrieval
     speech: interpret (rules; LLM only if uncertain and configured) → M1 rules (commitments, conflicts, …)
             └─► MemoryRecall (agent/recall.py)
                   1. cheap gate: a known Person named, or the wearer's own caution memory touched   (in-memory index)
                   2. bounded retrieval  vixy.retrieval.MemoryRetriever  (GRAPH.RO_QUERY, LIMITs, reasons)
                   3. deterministic relevance: caution (allergy/diet/avoid/dislike × buy/cook/bring/give X,
                      incl. food families) · plan clash (new plan vs remembered plan of that person)
                   4. uncertain only → optional reasoner model (budget/hour; may pick NOTIFY|NO_ACTION for a
                      candidate id; never writes text, never names an op)
                   5. message rendered from memory fields: attribution, "not verified", contested, ambiguity
             └─► decisions → NotificationGate (dedup `recall:<memory>`, 12 h cooldown, rate limits, expiry)
                           → Notifier · AgentLog{reasoning, supporting_memory_ids, supporting_json, CONCERNS}
                                                    │
                       FalkorDB vixy_<user> (vixy.graph.v2)   ◄── Dreamer job (vixy dream / POST /v1/dream)
                                                    │              routines · Fact promotion ·
                                                    │              semantic claims (semantic.py) +
                                                    │              optional model extraction (validated, cached) ·
                                                    │              entity resolution (entities.py) ·
                                                    │              reinforcement / SUPERSEDES / CONFLICTS_WITH
                                                    ▼
             /v1 API (vixy/api.py) ─► AskService (vixy/ask.py): interpret → read-only queries →
                                      re-check sources against Context Engine → grounded answer (+ optional rephrase)
```

Three levels of cost, in order:

| level | used for | cost |
|---|---|---|
| deterministic rules | every frame and utterance (M1 rules, the recall gate, relevance rules, question types) | in-process |
| graph retrieval | utterances that name a known person (or touch the wearer's own caution memory); `/ask` | a few bounded read-only queries |
| model reasoning | uncertain recall candidates; optional Dreamer extraction and `/ask` rephrasing; never per frame | budgeted per hour / per run, timed out, schema-validated; off by default |

Model configuration (all optional; the default runtime makes **no** model calls):

| what | how | defaults |
|---|---|---|
| Dreamer claim extraction | `prod_dreamer_model.cli … dream --model-extraction`, or `run_dream(extractor=ModelExtractor(model))` | `MODEL_API_KEY`; `DREAMER_EXTRACT_ID` / `MUSE_MODEL` = `muse-spark-1.3` (non-contributor), `_MAX_CALLS` 20, `_TIMEOUT_S` 30, `_MAX_OUTPUT_TOKENS` 1200, `_REASONING` low |
| agent recall reasoner, `/ask` rephrasing | `VixyRuntime(reasoner=…, answer_model=…)` with `model_from_env("VIXY_MODEL")` | same variables with the `VIXY_MODEL_` prefix; reasoner also capped at 6 calls per simulated hour |
| offline / tests | `FakeStructuredModel(responder)` | results labelled `fake:<name>`, never as a real model |

The client (`prod_dreamer_model.model_extract.ResponsesStructuredModel`) posts to the Meta Responses
API (`https://api.meta.ai/v1/responses`) with one forced function call, `store: false`,
untrusted text inside `<data>` as JSON, refuses `-contributor` models unless explicitly allowed,
and validates every output locally (`BudgetedModel`). Failures (timeout, HTTP error, malformed
or schema-invalid output, budget exhausted) raise `ModelError`; every caller then keeps its
deterministic result.

## Milestone 1 pipeline

```text
 perception output (synthetic in M1: vixy/demo_data.py)
   frames {frame_id, timestamp, classes[{class,count,confidence}], gps}     ASR segments {segment_id, start/end, text, speaker…}
        |  vixy/adapters.py  (reuses prod_dreamer_model.frames + context_engine.adapters)
        v
 +-----------------------------  repo root: vixy.runtime.VixyRuntime (one process, in-process calls)  ------------------------+
 |  ContextEngine.ingest_audio / ingest_video     (context-engine, unchanged core)                                            |
 |     validate · dedupe (event_id + sha256) · commit to SQLite · rolling buffer · alignment                                |
 |        | accepted, or duplicate-but-unfinished                                                                             |
 |        v                                                                                                                  |
 |  Ledger.begin (SQLite: per-event progress, segmenter checkpoint)                                                          |
 |        v                                                                                                                  |
 |  EpisodeWriter  audio -> (:Note) via MemoryStore.add_note   ── BEFORE the agent                                          |
 |                 video -> Segmenter -> (:Visit) via MemoryStore.add_visit (+ Place, Tag, NEXT, Day, DURING)               |
 |        v                                                                                                                  |
 |  ActiveAgent.handle(agent_observation(CE event))   ContextTracker · RuleInterpreter (+LLM if uncertain, with CE snapshot) |
 |        |   commitments · reminders · conflicts · questions · NotificationGate · AgentLog{trigger_event_id}                |
 |        v                                                                                                                  |
 |  Ledger.done(decision ids)          Decision -> Notifier (CollectingNotifier in M1)                                       |
 +---------------------------------------------------------|----------------------------------------------------------------+
                                                           |  read/write (allow-listed, MERGE on stable ids)
                                                           v
                 FalkorDB server (docker compose, port 6390)  — graph vixy_<user_id>, schema vixy.graph.v1
                 observed: Note Visit Day · entity: Place Tag Person · operational: Commitment Question AgentLog
                 inferred: Fact Pattern LongTermMemory (SUPPORTED_BY real Note/Visit) · meta: VixyMeta DreamRun
                                                           ^
                                                           |  same server, same graph
 +---------------------------------------------------------|----------------------------------------------------------------+
 |  Dreamer job (vixy.dream_job.run_dream  or  prod_dreamer_model.cli --layout unified … dream)                               |
 |   1. MemoryStore.consolidate()   Visits -> Fact (frequent place, usual activity)                                        |
 |   2. Dreamer.dream()             Muse loop (not used in M1) or offline consolidate.unified_dream:                       |
 |        routines -> Pattern + LongTermMemory · Facts -> LongTermMemory · statements about people -> LTM + Person          |
 |        every Note verified against ContextEngine.get_event_record (same text, same sha256) before use                   |
 |   3. finalize_evidence: inherit pattern evidence; no window-matched backfill; evidence-less -> 'unconfirmed'             |
 +---------------------------------------------------------------------------------------------------------------------------+
                                                           |
                       vixy.memory_api.SharedMemory (GRAPH.RO_QUERY) — used by the runtime, the CLI, and the agent
                       (AgentMemory.long_term_memories / memory_evidence)
```

## Exact runtime data flow (one event)

1. **Adapter.** A perception frame or ASR segment becomes a validated `VideoObservation` / `AudioSegment`. Counts, confidences, GPS, ids and the speaker flag are preserved, and nothing is invented.
2. **Session check.** The event's `session_id` must belong to `VIXY_USER_ID`; otherwise the outcome is `rejected`, with nothing stored.
3. **Context Engine.** `ingest_*` commits the event to SQLite before acknowledging it.
   - `duplicate` plus ledger `done`: return `duplicate`. Nothing else happens.
   - Same id with a different payload: `conflict`.
4. **`Ledger.begin(event_id, ingest_seq)`.**
5. **Episode Writer.**
   - Audio: `MemoryStore.add_note(note_id=note_<sha1(event_id)>, attribute_to_user=(speaker_role == wearer), props=provenance)`, then `ON_DAY`. A "remind me … supermarket" from the wearer becomes an open reminder with `trigger_place_type`.
   - Video: `Segmenter.add(frame)`, and the segmenter checkpoint `{from_seq, last_fed_seq}` is saved. A closed visit is stored with `add_visit` plus provenance, `ON_DAY`, and `DURING` links from Notes inside it.
   - A graph failure gives `failed`/`episode`. The visit stays queued and the frame is not fed twice.
6. **Ledger `episode_done`.**
7. **Agent.** `ActiveAgent.handle(adapters.agent_observation(kind, event))` receives the Context Engine event (`segment_id`/`frame_id` = `event_id`).
   - The Note already exists, so the commitment links `SUPPORTED_BY` to `Note{source_event_id}` and reminders can match.
   - Every Decision and AgentLog carries `trigger_event_id`.
   - If agent memory was unavailable: `failed`/`agent`.
8. **Ledger `done`**, with the decision ids.
9. **Start-up or `recover()`.**
   - `EpisodeWriter.rehydrate` replays the open visit's frames (`from_seq..last_fed_seq`) from Context Engine.
   - Every Context Engine event after the ledger's resume point that isn't `done` is processed again (outcome `recovered`).
   - Triggers the agent queued during an outage are re-run (`retry_failed`).

## Process model

- One runtime process per user holds Context Engine, the Episode Writer and the agent in-process. Context Engine's lock is per process. There are no brokers or queues.
- The Dreamer runs as a job, either in-process (`vixy dream`) or as Ara's CLI in its own process.
- With `FALKORDB_URL` set, the runtime and the Dreamer share the graph through the server. This is verified with the Dreamer as a separate process (`tests/test_integration.py`, server backend).
- Without it, both use one embedded falkordblite file, which is **one process at a time** (tests, offline development). Two processes on one embedded file are not shared storage.

## Components (originally separate repositories)

Until the monorepo migration ([MONOREPO_MIGRATION.md](MONOREPO_MIGRATION.md)) these were three repositories. `master-repo` is now the repository root, and the other two are under `components/`. The table records what each milestone changed.

| Repo | Role | Milestone 1 changes |
|---|---|---|
| `context-engine` (Aryan) | canonical observation store | `get_event_record`, `records_after` (read-only consumer APIs) |
| `dreamer-model` (Ara) | semantic consolidation | deps declared + `uv.lock`; non-contributor default model and contributor guard; fallback evidence bug fixed; unified layout (`graph_layout="unified"`, Visit/Note evidence, `Tag`/`Person` ABOUT, `unconfirmed` status, reset/load guards, `GRAPH.RO_QUERY`); `consolidate.py`; JSONL `load`; `EventHistory` |
| `context-engine` (M2) | | no changes (its consumer APIs from M1 are enough) |
| `dreamer-model` (M2) | | `semantic.py` (structured claims), `entities.py` (person resolution), `model_extract.py` (budgeted structured model client, validated extractor, fake), claim consolidation with reinforcement / supersession / conflicts in `consolidate.py`, `superseded` status, graph helpers, `--model-extraction` |
| `master-repo` (M2) | | `vixy/retrieval.py`, `agent/recall.py` (memory-aware agent), `vixy/ask.py`, `vixy/api.py`, `vixy/demo_m2.py`, schema v2 + `migrate`, `Decision.supporting_memories/reasoning`, SharedMemory decisions/timeline/person, CLI `demo-m2 ask serve decisions decision timeline migrate` |
| `master-repo` (Samu + integration) | runtime entry point | `vixy/` package; Vixy rename (wake word, graph prefix, env aliases); `add_note` provenance/attribution; exact commitment evidence; `trigger_event_id`; CE snapshot for the LLM path; LTM reads in `AgentMemory`; naive-timestamp rejection; `.env` untracked; docker-compose; docs; integration tests |


## Milestone 3: real perception and replay

The implementation is in `vixy/scenario.py`, `perception.py`, `replay.py` and `fixture.py`, exposed through the existing `vixy` CLI. No second backend is introduced.

```text
MP4 -> ffprobe metadata
    -> OpenCV sequential decode + PTS-based sampling (1 FPS default) -> local YOLO11n
    -> FFmpeg mono 16 kHz extraction + audio-stream offset -> local Whisper base.en
    -> existing Vixy adapters -> validated CE video/audio contracts
    -> source/config/version-hashed observation cache
    -> temporal replay + Context Engine SimulatedClock + virtual agent ticks
    -> Context Engine SQLite -> per-session Episode Writer -> existing Active Agent
    -> unified per-user FalkorDB -> Dreamer at explicit boundaries
    -> shared retrieval, grounded /v1/ask, agent policy and provenance inspection
```

Recording and session identities are explicit. Each session has its own Episode Writer/checkpoint; the agent's Context Engine provider uses the event's session. Time-only ticks create no synthetic sensor event. A replay advances Context Engine's clock at every event/tick/boundary, Active Agent through `advance_clock`, and Dreamer through `now_ms`. Timer and boundary checkpoints survive restart. Capture time, media offsets, simulated event time and wall-time audit fields remain distinct.

A transcript enters replay when its final segment ends, so context cannot see unfinished/future speech. Annotated speaker identity is applied only to wholly contained segments. Unknown speakers stay unknown. GPS is optional, sampled from supplied tracks with a maximum age. YOLO captions list classes only; they do not assert relationships or complex activities. Existing segmentation/activity rules still make their own inferences.

Process and replay use the same package lock. Model IDs, checkpoint hashes, library versions, inference counts and elapsed inference time are recorded. Cache identity includes source SHA-256, recording/user/session/time/annotation configuration, model/preprocessing settings and dependency versions. Replay validates the source and cache integrity and never loads perception models. A force-process replaces cached observations; changed output under an existing event ID is rejected by CE rather than silently rewritten.

Inputs, media, weights, caches, monitor transcripts and graph state are local/ignored. Ultralytics analytics synchronization is disabled. First processing can download public model weights; inference is local. Integrated wearable paths reject contributor models, including when a legacy synthetic override is set. `.env` is loaded by the shared Dreamer configuration loader before the runtime imports its storage; process environment values always win.

See [demo workflow](../demo/README.md) and the [M3 report](reports/VIXY_M3_IMPLEMENTATION_REPORT.md) for measured acceptance and limitations.
