# Vixy — Full System Audit

**Date:** 2026-11-04  **Scope:** `master-repo` (`feature/active-agent`), `context-engine`, `dreamer-model`
**Method:** full read of every source file, plus execution of every test suite, every demo and a mocked cross-repo integration probe. No source files were modified, and no paid model APIs were called.

Status legend, used throughout:

| Tag | Meaning |
|---|---|
| **VERIFIED** | Implemented, read, and confirmed by running it during this audit |
| **PARTIAL** | Implemented but incomplete, or working only under conditions that don't hold in the integrated system |
| **MOCK** | Simulated or hard-coded behaviour |
| **MISSING** | Required for the MVP but not implemented anywhere |
| **NOT VERIFIED** | Present in code or claimed in docs, but this audit couldn't execute it (for example, it needs paid API credits or a live server) |

---

## 1. Executive Summary

Each of the three components is good engineering on its own, and each is internally tested. They don't form a system yet. Each was built against a different idea of what its neighbours send and store, so **no two of them connect without adapter code**. Two consequences follow:

- The FalkorDB **"long-term memory" exists twice**, with incompatible schemas.
- The **live ingestion step** that both downstream components assume ("Ingestion stores every transcript as a `Note`") **doesn't exist**.

| Component | Owner | What it really is | Own tests | Integrates today? |
|---|---|---|---|---|
| `context-engine` | Aryan | Strictly validated event log (SQLite) plus a rolling window, audio↔video alignment, a prompt renderer, JSONL export and a FastAPI service | **51/51 pass** | Rejects raw YOLO frames. Nothing consumes its snapshot. |
| `master-repo/agent` | Samu | Real-time rule-based agent: frame triggers, utterance intent, commitments, conflicts, questions, notification gating. Optional LLM. | **48/48 pass**, eval **14/14** | Consumes raw frames and raw transcripts in its own schema. Never sees Context Engine output. |
| `master-repo/memory` | Samu | A *second* long-term memory: frames → Visits (episodes) → Facts, in FalkorDB graph `veyra_<user>` | (included above) | Agent reads and writes it. The Dreamer doesn't know it exists. |
| `dreamer-model` | Ara | Batch "dreaming": mock frames → Context Engine (in-process) → FalkorDB `vixy_dreamer` (`Day/Frame/Class/Note`) → Muse tool loop or rule fallback → `Pattern`/`LongTermMemory` | **41/42** out of the box; **42/42** after installing undeclared deps | Writes a graph the agent never reads. |
| `master-repo/yolomodelvision` | — | Two scripts calling a **Baseten VLM** (not YOLO) with ~4.5k yes/no class questions per image. Print only. | none | Produces nothing the pipeline consumes. |

**Headline findings**

1. **There's no perception stage.** No YOLO model, no ASR and no speaker identification exist in any repo. `yolomodelvision/dec-y.py` sends one image to `decision-models/baseten-elephant-9b-latest` as roughly 18 Baseten requests of 256 questions each, and prints the results.
2. **The Active Agent consumes raw sensory data, not contextualised events.** It ingests YOLO-style `classes` frames and raw transcripts directly (`agent/engine.py:138`). It never calls the Context Engine.
3. **Semantic event extraction is split three ways, and nobody does it for the integrated system:**
   - The Agent does it inline (`agent/context.py`, `agent/interpret.py`).
   - Samu's `memory/` does it in batch (`segmenter.py`, `rules.py`).
   - The Dreamer does it in batch with an LLM.
   - The Context Engine explicitly doesn't do it (README "Non-goals").
4. **The Dreamer stores real data in FalkorDB.** I verified this with embedded `falkordblite`. But it is a **separate graph with a separate schema**, and the agent has no code path that reads `:LongTermMemory` or `:Pattern`.
5. **The flagship demo doesn't work end to end.** Aryan's demo is "day 1: *Alex is allergic to peanuts*; day 2: buying peanuts for Alex". The offline Dreamer stores the sentence as a plain `:Note` but extracts **no memory** from it. The agent has **no rule** for allergy or safety conflicts.
6. **Security issue:** `master-repo/.env` is committed. It holds `BASETEN_API_KEY` (41 characters) and has been in history since commit `aa020ad`. **Rotate the key.**
7. **Privacy issue:** the Dreamer defaults to `muse-spark-1.3-contributor` with `store: true` (`dreamer-model/src/prod_dreamer_model/config.py:44,69`). Meta's docs say contributor mode lets Meta use submitted content for training. That's fine for synthetic data. It's not acceptable for real wearable transcripts, which include other people's speech.
8. **The system isn't executable end to end today.** The mocked integration probe passed **6 of 18** interface checks. Every failure is an integration gap, not a broken component.

**Minimum to a working MVP:** the core components are mostly done. Five integration-heavy milestones are needed (§12):

1. Contracts and adapters.
2. A live loop through the Context Engine.
3. A unified memory graph.
4. Retrieval and recall.
5. App plus demo.

The biggest single decision is **merging the two FalkorDB schemas into one per-user graph**.

---

## 2. Repository Inventory and Git State

| Repo | Remote | Branch checked out | HEAD SHA | Last commit | Commits |
|---|---|---|---|---|---|
| `master-repo` | `https://github.com/meta-hackathon-vixy/master-repo.git` | `feature/active-agent` (tracking `origin/feature/active-agent`) | `32e067656c4b948a89faacf2fa70fddc08572074` | Samuel Lozano Fornari, 2026-10-30, "Show agent nodes (Commitment, Question, AgentLog) in graph viewer" | 6 |
| `context-engine` | `https://github.com/meta-hackathon-vixy/context-engine.git` | `main` | `a87659746b41afa48d4ff9b27ed3ab40cdd7bc64` | Aryan Dutt, 2026-11-04, "Add context-engine: ingestion, rolling context, alignment, dreamer export" | 2 |
| `dreamer-model` | `https://github.com/meta-hackathon-vixy/dreamer-model.git` | `main` | `02a79ba2f5ff13c87249a8cef45339df66d42194` | araaday, 2026-11-04, "first commit" | 1 |

**Branch note:** the requested branch `active-agent` doesn't exist. The remote has only `main` and `feature/active-agent`, and the latter contains Samu's Active Agent (`main@0deecd3` + 2 commits, +2,590 lines: all of `agent/`, `tests/test_agent.py`, and schema section 4). I used `git switch --track origin/feature/active-agent`. I didn't merge anything, commit anything, or modify any repo. All three working trees are clean (`git status --ignored` is empty), because every venv, DB and output lived in a scratch directory.

**Size** (Python lines, excluding tests / tests):

- master-repo: 3,786 / 382
- context-engine: 1,547 / 589
- dreamer-model: 3,342 / 594

**Tooling:**

| Repo | Python | Packaging | Main dependencies |
|---|---|---|---|
| master-repo | 3.12 | `uv`, `uv.lock` | `falkordb`, `falkordblite`, `baseten-performance-client`, `pillow`, `python-dotenv` |
| context-engine | ≥ 3.10 | setuptools | `pydantic`; FastAPI/uvicorn as an extra |
| dreamer-model | ≥ 3.10 | setuptools | `falkordb[hiredis]`, `requests`, `python-dotenv` (no lockfile) |

**Repository hygiene issues**

| Issue | Evidence | Severity |
|---|---|---|
| Committed secret | `master-repo/.env` holds `BASETEN_API_KEY`; added in `aa020ad` ("New folder"); `.env` isn't in `.gitignore` | **Critical**: rotate the key, untrack the file |
| Empty root README | `master-repo/README.md` is 0 bytes; `memory/readme.txt` is 0 bytes | Low |
| Undeclared deps (dreamer) | `graph.connect()` imports `redislite.falkordb_client` (`graph.py:711`), but `falkordblite` isn't in `pyproject.toml`. `bridge.py` needs `context_engine` + `pydantic`, which aren't declared either. | High: the README's "no server needed" path fails on a clean install (verified) |
| Two product names | "Veyra" in 12 master-repo files, including the agent wake word `^(hey )?veyra` (`agent/interpret.py:19`); "Vixy" everywhere else | Medium: users would say "Vixy, …" and the agent would ignore it |
| `yolomodelvision/main.py` | `print("Hello from metahackathon!")` | Placeholder |

---

## 3. What Aryan Implemented — `context-engine`

### 3.1 Inventory

| Module | Function | Status |
|---|---|---|
| `schemas.py` | Versioned Pydantic v2 contracts. `extra="forbid"`, `frozen=True` (`:62-65`). `AudioSegment` (`:87`), `VideoObservation` (`:121`), `Location`, `Detection`, `ContextSnapshot` (`:185`), `RenderedPrompt`, `DreamerEventRecord` (`:212`) | **VERIFIED** |
| `timeutil.py` | UTC helpers, availability-time rule (audio=`end_time`, video=`timestamp`), `SimulatedClock` | **VERIFIED** |
| `store.py` | SQLite (WAL, `synchronous=FULL`). `UNIQUE(event_id)`. Canonical-JSON SHA-256 dedup and conflict detection (`:87-119`). Integer-µs range queries. | **VERIFIED** |
| `buffer.py` | Per-session bounded buffer. Eviction driven by a high watermark, not wall clock. LRU over sessions. Lazy re-hydration from SQLite. | **VERIFIED** |
| `alignment.py` | `align()`: many-to-many video↔audio by `a.start−tol ≤ v.ts ≤ a.end+tol`, as id references | **VERIFIED** |
| `engine.py` | `ContextEngine.ingest_audio/ingest_video/get_context/export_events/export_jsonl/buffer_stats` | **VERIFIED** |
| `prompt.py` | `render_prompt()`: character-budgeted, newest-first, JSON-quoted speech, prompt-injection notice | **VERIFIED** |
| `api.py` | FastAPI: `GET /health`, `POST /events/audio`, `POST /events/video`, `GET /context`, `GET /events/export` (NDJSON). Uniform error envelope. | **VERIFIED** by `tests/test_api.py` (TestClient). I didn't run a live server. |
| `adapters.py` | `audio_from_relative_segment` (Whisper-style), `video_from_epoch_caption` | **MOCK**: self-described as hypothetical formats (`:3-8`) |
| `demo.py` + `fixtures/day1/day2.jsonl` | Deterministic two-day replay (13 synthetic events) | **VERIFIED** (exit 0) |
| `examples/mock_memory_integration.py` | `MockMemoryProvider` returns one hard-coded fact (`:41-51`) | **MOCK** (and labelled as such) |

### 3.2 Answers to the Phase 2 questions

1. **What it receives:** only *already processed* events. These are finalised transcript segments (`AudioSegment`) and processed visual observations with a mandatory free-text `description` (`VideoObservation`). Ingestion is in-process (`ingest_*`) or HTTP (`POST /events/*`).
2. **Perception formats:** exactly two v1 schemas. Anything else is rejected with 422 because of `extra="forbid"`. The two adapters target *assumed* formats, not real producers.
3. **Representation:**

   | Signal | Representation |
   |---|---|
   | Audio | `{event_id, session_id, start_time, end_time, text, speaker_id?, confidence?, is_final=true, location?, source?}` |
   | Video | `{event_id, session_id, timestamp, description, detections:[{label, confidence}], frame_ref?, location?, source?}` |
   | GPS | Optional `Location{latitude, longitude, accuracy_m?}` on every event. Never fabricated. |

4. **Normalisation and validation:**
   - ISO-8601 with an explicit offset is required. Naive timestamps and numeric epochs are rejected. Everything is converted to UTC (`schemas.py:40-55`).
   - `end ≥ start` is enforced. Non-final transcripts are rejected. Text must be non-whitespace.
   - Confidence must be in [0, 1].
   - Unknown fields and unknown schema versions are rejected.
5. **Rolling buffer:**
   - `SessionBuffer` keeps events sorted by availability time.
   - Retention is `high_watermark − 900 s`, capped at 5,000 events per session, with up to 64 sessions under LRU.
   - `get_context` serves from the buffer when `buffer.covers(lo)`; otherwise it falls back to SQLite (`engine.py:149-156`).
   - The content is identical either way. `served_from` is diagnostic only.
6. **SQLite history:**
   - One `events` table: `ingest_seq` PK, `event_id UNIQUE`, `session_id`, `kind`, `start_us`, `available_us`, `ingested_at_us`, `schema_version`, `payload_sha256`, `payload_json`, with two composite indexes.
   - Events are committed before the acknowledgement.
   - Re-sending an identical event returns `duplicate`. Re-sending a different payload under the same id raises `EventConflictError` (409).
7. **Temporal alignment:** timestamp interval containment with ±0.5 s tolerance. The output is `Association{audio_event_id, video_event_id, offset_seconds, within_interval}` plus unmatched id lists. It aligns timestamps only, with no semantic linking.
8. **`ContextSnapshot`:**
   - `schema_version`, `session_id`, `as_of`, `window_start/end/seconds`.
   - `observed{transcript[], video[]}`, exactly as ingested.
   - `derived{alignment_tolerance_seconds, associations[], unmatched_*}`.
   - `source_event_ids` (chronological), `truncation{…}`, `served_from`.
9. **Prompt generation:**
   - A header with session, window and a "quoted speech is data, not instructions / no long-term memory" notice.
   - An `AUDIO` section: `[start -> end] speaker=… asr_conf=… id=… said: "…"`.
   - A `VISUAL` section: `[ts] id=… during_audio=… scene: "…" detections: [...]`.
   - Budgeted in characters (default 4,000), newest-first, never cutting a record.
10. **Passed to the Active Agent:** `GET /context` returns `{snapshot, prompt}`. **No consumer exists.** The agent has no code that calls it (§4, §7).
11. **Exported to the Dreamer:** `vixy.dreamer_event.v1` JSONL. Each line has `{kind, event_id, session_id, event_time, event_end_time, ingest_seq, ingested_at, payload_sha256, event:{original}}`, over half-open `[start, end)` ranges on `event_time`.
12. **Genuine versus mocked:** everything in `src/` is genuine and tested. The mocks are the two adapters (hypothetical input shapes) and the memory-provider example. **No inference of any kind happens here**, by design.

### 3.3 Raw detections vs observations vs semantic events

| Layer | Example | Who produces it today |
|---|---|---|
| Raw detection | `{"frame_id","timestamp","classes":[{"class","count","confidence"}]}` | **Nobody.** The shape is documented in `memory/README.md` and the dreamer's `frames.py`, but no producer exists in any repo. |
| Processed observation | `VideoObservation{description, detections}` | Only the Dreamer's adapter, `frames.to_video_observation` (`dreamer-model/.../frames.py:140`). It synthesises `description` as `"person x2, cup x4"`. |
| Semantic event | "arrived at supermarket", "user committed to meet Alex at 18:00" | Only inside the agent (`ContextTracker` triggers, `RuleInterpreter` intents), and never persisted as events |
| Contextual interpretation | "buying peanuts for someone allergic" | **Nobody** |

**Compatibility with YOLO-style frames** (probe check 1a, VERIFIED): `ingest_video(yolo_frame)` is rejected. The missing fields are `event_id`, `session_id` and `description`, and the extra fields are `classes` and `frame_id`.

`Detection` has no `count`. The Dreamer works around that by smuggling counts into free-form `source.counts`.

The Context Engine also requires a non-empty `description`. That is effectively a scene-caption requirement, and **no scene-captioning component exists**.

**Missing stages:**

- A **perception adapter** (raw frame → `VideoObservation`, with counts as a first-class field).
- An **ASR adapter**, plus a wearer/non-wearer flag.
- A **semantic event extraction stage** whose output is persisted and shared.

---

## 4. What Samu Implemented — `master-repo` (`feature/active-agent`)

### 4.1 Active Agent (`agent/`)

| Module | Function | Status |
|---|---|---|
| `models.py` | `VisionObservation.parse` (`:41-69`) and `AudioObservation.parse` (`:72-119`). Tolerant parsing: extra fields go to `extra`, bad GPS is treated as no GPS. `Decision` and `Action{NO_ACTION, NOTIFY, ASK_USER, MEMORY_UPDATE}`. | **VERIFIED** |
| `context.py` | `ContextTracker.observe` (`:88-152`). Frame dedup (LRU), drops stale frames, confidence ≥ 0.5, 8-frame stable-class window, place type via `memory.rules.classify_place` with 5-frame hysteresis, GPS move > 60 m. Triggers: `place_entered`, `location_changed`, `bus_stop_wait`, `boarded`, and a once-per-minute `tick`. | **VERIFIED** |
| `interpret.py` | `RuleInterpreter` (`:96-151`): regex intents `command / reminder / answer / completion / commitment / chatter`, plus speaker gating, wake word, injection screen and forbidden verbs. `HybridInterpreter` (`:161-192`) calls the LLM **only** when the rules mark an utterance `uncertain`. | **VERIFIED** (with `FakeLLM`) |
| `temporal.py` | Deterministic date and time parsing. Ambiguity is kept explicit (`starts=[18:00, 06:00]`). `overlap()` never assumes a duration. | **VERIFIED** |
| `engine.py` | `ActiveAgent`: `handle` (`:138`), `_execute` with ordered write → notify → ask (`:231-284`), `_on_tick` (`:307`), `_on_place`/`_on_location` reminders, `_on_bus_stop`, `_on_audio` (`:391`), `_on_commitment` conflict and correction logic (`:448-551`), `answer` and `_resolve`, and a `retry_failed` queue | **VERIFIED** |
| `policy.py` | `NotificationGate`: dedup keys, per-prefix cooldowns, hourly budget per priority (3/6/12), expiry. `CollectingNotifier` (`:34`) just stores decisions in a list. | Gate **VERIFIED**. Delivery is **MOCK**. |
| `memory_tools.py` | `AgentMemory`, the agent's only path into FalkorDB. Targeted reads (`commitments_between`, `open_reminders`, `place_near`, `preference_statements`, questions). Allow-listed writes (`apply`, `:281-287`: `record_commitment`, `update_commitment_time`, `complete_reminder`), plus `save_question`, `mark_question`, and the `log` audit. | **VERIFIED** (embedded FalkorDB) |
| `llm.py` | `OpenAICompatibleLLM` with a 4 s timeout. `from_env` returns `None` unless `VEYRA_LLM_MODEL` is set. Default base URL is Baseten (`:41`). | **NOT VERIFIED** live (the README says so too) |
| `transit.py` | `MockTransitProvider` | **MOCK** |
| `scenarios.py`, `sim.py`, `cli.py` | 14 scenario evals, a simulated 8.9 h day, and a CLI | **VERIFIED** |

**Answers to Phase 3:**

1. **Triggers:**
   - Every vision frame goes through `ContextTracker`. About 98% are filtered with no DB or LLM work (sim: 29,905 of 30,420 frames).
   - Every audio segment is interpreted.
   - Once-per-minute ticks fire for upcoming commitments and due questions.
   - Answers arrive via `answer()`.
2. **Real input schema:**
   - Vision: `{frame_id?, timestamp|ts, classes:[{class,count,confidence}] | [str], latitude|lat?, longitude|lon?}`.
   - Audio: `{segment_id?, timestamp_start|timestamp, timestamp_end?, transcript, language?, speaker_id?, speaker_is_user?: bool, confidence?, latitude?, longitude?}`.
   - Routing: `handle()` sends a record to audio if and only if it has a `"transcript"` key (`engine.py:139`).
3. **Raw vs contextualised:** **raw.** It consumes raw YOLO-style class frames and individual raw transcripts. It never reads the Context Engine snapshot, its alignment, or multi-utterance context.
4. **Audio interpretation:** each utterance is classified in isolation:
   - Rules first.
   - A commitment needs a time expression plus a definite, proposal or event cue.
   - Commitments are written **only if `speaker_is_user is True`**. An unknown speaker can at most trigger a confirmation question when there's a clash.
   - Commands need `speaker_is_user=True`, the "Veyra" wake word, and an allow-listed verb.
5. **Visual interpretation:** a place type from stable classes (`memory/rules.py` cue tables), bus stop vs on the bus, and a GPS place lookup. Objects aren't interpreted beyond place cues.
6. **LLM vs rules:** deterministic rules by default. The LLM only labels `intent/strength/title/people` for uncertain utterances. It never writes Cypher or picks operations. All messages are templates.
7. **Deciding to act:**
   - Each trigger type has its own handler.
   - The default is `NO_ACTION`.
   - Actions require a deterministic condition: reminder place match, overlap, imminent commitment, correction, or due question.
8. **Supported actions:** `NOTIFY`, `ASK_USER`, `MEMORY_UPDATE` (three allow-listed operations) and `NO_ACTION`.
9. **Proactive notifications:**
   - **Decision-level: yes.** Examples are "Heads up: Meet Alex tomorrow at 6 PM overlaps Dentist appointment" and "You're at the supermarket. Reminder: buy milk".
   - **Delivery: MOCK.** There's only the `Notifier.deliver(decision) -> bool` protocol and `CollectingNotifier`. No push service, WebSocket or app exists.
10. **Short-term context:** its own in-memory `ContextTracker` (≈8 frames / 60 s) and audio dedup LRUs. It has no transcript history, so it can't relate "I'm getting peanuts for Alex" to anything said earlier.
11. **Long-term memory retrieval:** from **its own** graph `veyra_<user>`:
    - `Commitment` nodes for the day or window.
    - Open reminder `Note`s by place type.
    - Nearby `Place`.
    - `Fact{kind:'preference'}` statements, which are used only to filter bus routes (`engine.py:376-379`).
12. **Writes to memory:** yes. `Commitment`, `Question`, `AgentLog` (no raw transcripts), and reminder completion.
13. **Preventing repetition:**
    - Dedup keys per decision kind, with cooldowns (`conflict` and `upcoming` fire once ever, `reminder` every 2 h, and so on).
    - An hourly budget.
    - One open question at a time, with a 20-minute block.
    - Low-priority questions are deferred 10 minutes.
    - Audio is deduped by segment id *and* by `content_key` (same words in the same 5 s bucket).
    - Dedup keys are reloaded from `AgentLog` for the last 7 days at startup (`engine.py:129-133`).

**A hidden dependency:** the reminder feature and the commitment→Note evidence links both rely on someone calling `MemoryStore.add_note(text, ts)` for every transcript (`agent/README.md` "Who stores transcripts"). **Only the simulator does that** (`agent/sim.py:65`). In any integrated pipeline, no reminders would ever exist.

### 4.2 Memory graph (`memory/`) — a second long-term memory

| Module | Function | Status |
|---|---|---|
| `frames.py` | `parse_frame` (YOLO-style plus legacy shapes), `attach_gps` by nearest timestamp (≤ 30 s). `parse_ts` treats naive ISO strings as **machine-local time** (verified: `'2026-10-26T09:00:00'` → 01:00Z on this +08:00 machine). | **VERIFIED** (that timestamp behaviour is a latent bug) |
| `segmenter.py` | `Segmenter`: frames → `Visit` episodes (gap 5 min, GPS move 120 m, 3-frame mismatch hysteresis, blip removal, tag ratio ≥ 20%) | **VERIFIED** |
| `rules.py` | `classify_place` / `classify_activity` cue tables. Sensitive types are healthcare and worship. | **VERIFIED** (tuned on synthetic data) |
| `store.py` | `MemoryStore`: `add_visit`, `_resolve_place` (75 m, same type), `add_note` (keyword reminders and preferences), `consolidate` (`frequent_place` ≥ 3 distinct days in 28, `usual_activity` ≥ 60%, sensitive places excluded, unsupported facts set inactive), `day`, `last_seen`, `explain`, `context`, `forget_visit` | **VERIFIED** |
| `db.py` | `graph_for(user)` → graph `veyra_<user>`. Embedded `falkordblite` by default, or a server via `FALKORDB_HOST`. | **VERIFIED** (embedded) |
| `schema.cypher`, `viz.py`, `cli.py`, `demo.py` | Executable schema, HTML graph viewer, CLI, synthetic week | **VERIFIED** |

### 4.3 `yolomodelvision/`

| Item | Detail | Status |
|---|---|---|
| What it does | Sends `koufu.PNG` to Baseten `decision-models/baseten-elephant-9b-latest` with `{"type":"noul","instructions":"Is a <cls> present?"}` per class | — |
| Scale | `dec-y.py` does that for all of `classes.txt` (~4.5k lines, deduplicated, 256 per request) | — |
| Output | Printed probabilities. No `frame_id`, no `count`, no JSON. | — |
| Cost | Paid, and ~18 requests per frame. Not a viable per-frame perception path. | — |
| Execution in this audit | Not run (paid API) | **NOT VERIFIED** |

---

## 5. What Ara Implemented — `dreamer-model`

| Module | Function | Status |
|---|---|---|
| `frames.py` | Frame parsing (rejects naive timestamps, unlike master-repo) and `to_video_observation`, the **only real raw-frame → Context Engine adapter in the codebase** | **VERIFIED** |
| `mock_data.py` | 14 deterministic synthetic days (20,618 frames, 6 notes) | **MOCK** (by design) |
| `bridge.py` | `EventBridge`: wraps an in-process `ContextEngine`, ingests frames and notes, exports with `export_events`, groups them into `DayBundle`s per local day. `classify_note` is a keyword hint. | **VERIFIED** |
| `graph.py` | `ProdGraph`: schema `Day/Frame/Class/Note` (observed) plus `Pattern/LongTermMemory` (believed). `load_bundle` uses MERGE. `safe_cypher` is a regex read-only guard. `write_pattern`/`write_memory` validate inputs. `propagate_evidence`/`backfill_evidence`. `connect` uses a server or embedded DB. | **VERIFIED** (embedded, after adding the missing dep) |
| `tools.py` | `GraphToolBox` exposes four tools to the model: `run_cypher`, `write_pattern`, `write_memory`, `finish` | **VERIFIED** (with a fake client in tests) |
| `dreamer.py` | `Dreamer.dream`: 4 focus rounds over the Meta Responses API (`previous_response_id` chaining, `store:true`), budget nudges, fallback | Loop **VERIFIED** with a fake client. Live Muse **NOT VERIFIED**. |
| `heuristic.py` | `heuristic_dream`: 8 hard-coded `Signature`s (class name plus hour window, e.g. `"bus interior"` 05–10 h), sensitive one-offs, notes (preference, reminder, `_named_person` regex) | **MOCK-tuned**: the rules match the mock generator's vocabulary and schedule |
| `report.py`, `readable.py`, `cli.py` | Text, Markdown and JSON reports. `seed / dream / report / reset / all`. | **VERIFIED** |
| `dream_output/` (committed) | 13 patterns and 8 memories, generated 2026-11-04T04:11Z. The statements aren't heuristic templates, so they were probably produced by Muse. | **NOT VERIFIED** (I can't reproduce it without credits) |

**Answers to Phase 4:**

1. **Expected input:** perception-shaped `VisionFrame`s and note dicts. These go **through an in-process `ContextEngine`** and come back out through `export_events`. The graph is then loaded from those records.
2. **Can it consume the Context Engine's JSONL exports?** **Not as files and not over HTTP.**
   - There's no `load` command (probe 4a). The JSONL is written only as a side artifact.
   - It *can* read Context Engine history when it shares the same SQLite file or engine **and** the same `session_id` (probe 4b passed).
   - It defaults to `wearer-01`, while the CE fixtures use `demo-user-01` (probe 4c).
3. **How episodes are generated:** **they aren't.**
   - "Episodic memory" here means one `:Frame` node per frame (20,618 nodes and ~120k `SAW` edges for 14 days at one frame every ~20 s). At 1 fps that becomes about 1.2M frames per 14 days.
   - "Stays" are computed only for display in `readable.py`.
   - Samu's `segmenter.py` *does* produce episodes (`Visit`).
4. **Semantic fact extraction:** done by Muse through tool calls (read-only Cypher, then writes) or by the heuristic signatures. The output is free-text `statement`s with a `kind` enum. There's no structured subject–predicate–object form.
5. **Consolidation:**
   - It's a prompt instruction ("merge duplicates, retract weak ones, promote stable ones").
   - Patterns and memories are upserted by slug id.
   - Nothing is incremental: `all` **resets the graph** (`cli.py:118-121`), and each `dream` re-reads everything.
6. **Uses LLMs?** Yes: `muse-spark-1.3-contributor` via `https://api.meta.ai/v1/responses`, `reasoning.effort=low`, 8,192 max output tokens, up to 4×10 turns. The deterministic fallback runs when there's no key or no writes.
7. **Entities and relationships:** **no entity nodes** (no `Person`, `Place`, or `Object` beyond `:Class`). People appear only inside statement text, via the regex `_named_person` (`heuristic.py:179`). Places are raw lat/lon on frames.
8. **Duplicate or contradictory memories:**
   - Duplicates: id reuse is requested in the prompt, and writes are idempotent per slug.
   - Contradictions: **no detection.** There's no confidence decay or supersession, and `status` is set manually by the model.
9. **Episodic and semantic separated?** Observed vs believed labels: yes. The `Pattern` vs `LongTermMemory` distinction is fuzzy, because the heuristic writes almost the same statement into both.
10. **Provenance:**
    - `SUPPORTED_BY` links to `Frame`, `Note` and `Day`. `Note.note_id` equals the CE `event_id`, which is good.
    - `Frame.frame_id = source.frame_id`, so the CE `event_id` is only recoverable by its `cam0:` prefix. `payload_sha256` and `ingest_seq` are dropped.
    - **Two weaknesses:**
      - `backfill_evidence` *invents* provenance from a pattern's own time window and classes (`graph.py:610-693`), rather than recording what the model actually looked at.
      - **Bug (VERIFIED):** on the fallback path, `Dreamer.dream()` checks `toolbox.usage` counters (`dreamer.py:218-230`). But `heuristic_dream` writes through `ProdGraph` directly, so the counters stay `(0,0)` and propagate/backfill never run. Result: **7 of 11 long-term memories have zero evidence**, contradicting the README's claim that "why do you think that? always resolves".
11. **FalkorDB genuinely integrated?** **Yes** (VERIFIED with embedded `falkordblite`):
    - Indexes, MERGE writes, and real queries.
    - Server mode (`FALKORDB_URL`) is NOT VERIFIED.
    - Embedded mode needs `falkordblite`, which isn't declared.
12. **Querying stored memories:** `cli report` (text, Markdown, JSON), `ProdGraph.fetch_memories/fetch_patterns/fetch_evidence`, and arbitrary read-only Cypher. **There's no retrieval API.** The README lists it under "Limits".
13. **Scheduling:** **manual CLI only.** There's no scheduler, no event trigger, and no per-day incremental mode.

**Missing for reliable long-term memory:**

- Episode abstraction.
- Entity resolution (people, places).
- Structured facts (subject, predicate, object, valid time).
- Contradiction and supersession handling.
- Incremental dreaming.
- A retrieval API.
- One shared graph with the agent.
- Correct evidence on the fallback path.
- A non-contributor model for real data.
- A deterministic fallback that isn't tuned to the mock.

**Safety:** `safe_cypher` is a regex guard. FalkorDB's read-only query command (`GRAPH.RO_QUERY`, exposed as `Graph.ro_query` in `falkordb-py`) would enforce read-only access in the server rather than by pattern matching.

---

## 6. Current End-to-End Architecture

This is what actually exists. Solid arrows exist in code. Dashed arrows (`- ->`) are assumed in docs but have no implementation.

```text
                 (no camera / mic / ASR / YOLO code anywhere)
                                   :
          yolomodelvision/dec-y.py :  Baseten VLM, prints only
                                   :
   +-------------------------------+-------------------------------------------+
   | mock_data.py (Ara)            | agent/scenarios.py, memory/demo.py (Samu) | fixtures/day*.jsonl (Aryan)
   v                               v                                           v
 dreamer frames.py           agent/models.py ----------+               context_engine.ContextEngine
   | to_video_observation        |  (raw classes +     |                (SQLite, buffer, alignment)
   v                             |   raw transcripts)  |                   |            |
 ContextEngine (in-process,      v                     v                   | GET /context  GET /events/export
 its own SQLite file)      ContextTracker +     memory.segmenter           v            v
   | export_events          RuleInterpreter     -> Visit / Place / Fact   (no consumer) (no consumer)
   v                             |                     |
 FalkorDB graph "vixy_dreamer"   v                     v
 (:Day :Frame :Class :Note)  ActiveAgent  <---->  FalkorDB graph "veyra_<user>"
   |                             |                (:Visit :Place :Tag :Fact :Note{id}
   v                             v                 :Commitment :Question :AgentLog)
 Dreamer (Muse | heuristic)  CollectingNotifier          ^
   v                         (in-memory list)            :  MemoryStore.add_note per transcript
 :Pattern :LongTermMemory                                :  - - assumed "Ingestion", only sim.py does it
   |
   +- - - - - - - - - (never read by the agent; different graph and schema) - - - -x
```

In short:

- **Three disconnected islands.**
- **Two long-term memory graphs.**
- **Two ingestion paths into SQLite** (the Context Engine's own, and the Dreamer's private copy).
- **No real producer and no real consumer at either end.**

---

## 7. Data Contracts and Compatibility Matrix

### 7.1 Interface matrix

| # | Producer → Consumer | Actual function / endpoint | Schemas (key fields) | Status | Missing |
|---|---|---|---|---|---|
| I1 | Vision perception → CE | `ContextEngine.ingest_video` / `POST /events/video` | Producer (documented only): `{frame_id, timestamp, classes[{class,count,confidence}]}`. CE: `{event_id*, session_id*, timestamp*, description*, detections[{label,confidence}]}` | **INCOMPATIBLE** (probe 1a) | A producer. Promote the adapter `dreamer frames.to_video_observation` (works, probe 1b). Add `count` and caption policy. |
| I2 | Audio perception (ASR) → CE | `ingest_audio` / `POST /events/audio` | CE: `{event_id, session_id, start_time, end_time, text, speaker_id?, confidence?}` | **MISSING producer** | ASR, VAD, a wearer flag, and the real adapter (`adapters.audio_from_relative_segment` is hypothetical) |
| I3 | CE → Active Agent | `GET /context` → `{snapshot, prompt}` | Agent expects `transcript/timestamp_start/segment_id/speaker_is_user` and `classes` | **INCOMPATIBLE** (probes 3a, 3b, 3c, 3e) | An event adapter (`text→transcript`, `start_time→timestamp_start`, `event_id→segment_id`, `detections→classes`, `location.latitude→latitude`), plus `speaker_is_user` and a snapshot-aware entry point. A six-field rename makes audio parse (probe 3d). |
| I4 | CE → Dreamer | In-process `EventBridge(engine).export_range` | `DreamerEventRecord` → `FrameNode`/`NoteNode` | **PARTIAL** (probe 4b passes only when the engine/SQLite is shared and `session_id` is overridden) | A JSONL/HTTP loader (probe 4a), a shared session/user id, and an incremental watermark |
| I5 | Dreamer → FalkorDB | `ProdGraph.load_bundle`, `write_pattern`, `write_memory` | `vixy_dreamer` schema | **VERIFIED** (embedded) | Declare `falkordblite`. Fix the evidence bug. |
| I6 | FalkorDB (Dreamer) → Active Agent | none | Agent queries `Note{kind:'reminder',status:'open'}` with key `id`, and `Fact`. Dreamer writes `Note{note_id, kind}` (no status) and `LongTermMemory`. | **INCOMPATIBLE** (probes 5a/5c; on a copy of the 14-day graph, `open_reminders()=[]` and `preference_statements()=[]` even though the reminder note and 11 long-term memories exist) | One schema, and an LTM retrieval method in `AgentMemory` |
| I7 | Active Agent → User / app | `Notifier.deliver(decision)` | `Decision.to_dict()` (shape documented in `agent/README.md`) | **MOCK** | A notification service (WebSocket/SSE/push) and the app |
| I8 | App → Active Agent (answers) | `ActiveAgent.answer(question_id, text)` (Python) | — | **PARTIAL** (no transport) | An HTTP endpoint |
| I9 | Dreamer → Active Agent (questions) | Agent supports `Question{source:'dreamer',kind:'dreamer',status:'pending'}` (`memory_tools.due_questions`) | — | **MISSING producer** | The Dreamer never writes `Question` |
| I10 | Raw frames → Samu's memory graph | `memory.cli ingest frames.jsonl` → `segment` → `add_visit` | YOLO-style frames | **PARTIAL**: batch file only; `parse_frame` on a CE `VideoObservation` gives `classes={}` (probe 6a) | A live "episode writer" fed from the CE |
| I11 | Transcripts → `Note` (agent's assumption) | `MemoryStore.add_note` | text, ts | **MISSING** outside `sim.py` | The same episode writer |
| I12 | Agent ↔ its own graph | `AgentMemory` | `veyra_<user>` schema | **VERIFIED** | — |

### 7.2 Identifiers

| Concept | Context Engine | Active Agent / memory | Dreamer |
|---|---|---|---|
| Person | — (session only) | graph name `veyra_<user>` (CLI `--user`, default `demo`) | `DREAMER_USER_ID` (read in `config.py`, **never used**) |
| Session | `session_id` (required, opaque) | none | `DREAMER_SESSION_ID`, default `wearer-01` |
| Graph | — | `veyra_<user>` | `vixy_dreamer` (single, fixed) |
| Event id | `event_id`, globally unique, stable across retries | `frame_id` / `segment_id`, or a content hash | `cam0:<frame_id>`; notes `note-YYYY-MM-DD-HHMM` |
| Note key | — | `Note.id = note_<sha1(ts:text)>` | `Note.note_id = CE event_id` |

### 7.3 Timestamps and time zones

| | Context Engine | Agent / memory | Dreamer |
|---|---|---|---|
| Wire format | ISO-8601 **with an offset required**; epochs rejected | ISO **or** epoch s/ms; **naive ISO is accepted and read as machine-local time** (`memory/frames.py:31-35`) | ISO with offset or epoch; naive rejected |
| Storage | integer UTC µs (SQLite); output `Z` | epoch ms (FalkorDB) | epoch ms, plus `local_*` fields |
| Local time zone | none (UTC only) | `VEYRA_TZ`, default Asia/Singapore | `DREAMER_TZ`, default Asia/Singapore |
| Audio "event time" | `start_time`; window keyed on `end_time` | `ts_ms = end_ms` | `start_time` |

### 7.4 Error handling

| Component | Behaviour |
|---|---|
| Context Engine | Typed: 422 validation, 409 conflict, 400 bad request, one error envelope, no payload echo |
| Agent | Never raises on bad input; returns `NO_ACTION "invalid observation"`. `MemoryUnavailable` produces `status:failed` plus a retry queue. LLM failure falls back to the rule result. |
| Dreamer | Tool errors are returned to the model. API errors abort the round and trigger the fallback. Bad Cypher returns `{ok:false}`. |

These three behaviours aren't reconciled into one cross-component policy (retries, dead letters).

---

## 8. Working Features vs Missing Features

| # | MVP capability | What exists | Status | Missing | Owner (recommended) |
|---|---|---|---|---|---|
| 1 | Camera, mic, optional GPS input | GPS fields in all schemas | **MISSING** (capture) | Device capture (Ray-Ban Meta / phone camera + mic), upload | New: perception service |
| 2 | Visual and audio perception | Baseten VLM experiment (prints only) | **MISSING** | Local detector (YOLO-class) emitting the documented frame JSON; ASR with timestamps; wearer flag | Perception service, feeding CE via adapters |
| 3 | Short-term multimodal context | CE buffer, alignment, prompt (**VERIFIED**); agent's own tracker | **PARTIAL** (not consumed) | Agent consumes CE events and snapshot | CE (keep) |
| 4 | Semantic interpretation | Agent rules (place, intents, time); `memory/rules.py`; Dreamer LLM batch | **PARTIAL** | One persisted semantic-event stream; scene captions on scene change; health/safety cues | Live: Samu's tracker/segmenter fed by CE. Deep: Dreamer. |
| 5 | Proactive reasoning and interventions | Agent: conflicts, reminders, upcoming, questions, gate (**VERIFIED** on synthetic data) | **PARTIAL** | Use of long-term memory (allergy-type recall); LLM-gated relevance check; real delivery | Active Agent |
| 6 | Episodic memory | Samu: `Visit`/`Note` (**VERIFIED**). Ara: raw `Frame` nodes. | **PARTIAL** (duplicated, not fed live) | Live episode writer; pick `Visit` as the canonical episode | memory/ (Samu) |
| 7 | Semantic memory | Samu: `Fact` (frequent place, activity, preference). Ara: `Pattern`/`LongTermMemory`. | **PARTIAL** (duplicated) | Structured facts, entities, contradictions | Dreamer (Ara) |
| 8 | FalkorDB long-term storage | Both, embedded (**VERIFIED**) | **PARTIAL** | One server (Docker), one schema, one graph per user | Shared |
| 9 | Memory retrieval across days | Samu: `day/last_seen/explain/context`. Ara: `report`. | **PARTIAL** | Retrieval API for agent and app; LTM in the agent's context | New memory API (in master-repo) |
| 10 | Natural-language questions about the past | None (CLI subcommands only) | **MISSING** | `/ask`: LLM plus read-only graph tools (reuse `GraphToolBox.run_cypher`) | Memory API |
| 11 | Mobile-accessible frontend | `memory/viz.py` static HTML graph | **MISSING** | PWA: notifications, answers, timeline, ask | New: app |
| 12 | Reproducible end-to-end demo | Three separate demos (**VERIFIED** each) | **MISSING** (joint) | One replay script across all components (§13) | Integration owner |

---

## 9. Duplicated Responsibilities and Architectural Conflicts

| # | Duplication / conflict | Where | Resolution |
|---|---|---|---|
| D1 | **Two FalkorDB memory schemas and two consolidators** | `memory/store.py` (`veyra_<user>`: Visit/Place/Tag/Fact/Note{id}) vs `dreamer-model/graph.py` (`vixy_dreamer`: Day/Frame/Class/Note{note_id}/Pattern/LTM). Same label `Note` with different keys and properties. Same rel names `ABOUT`/`SUPPORTED_BY` with different targets. | Single per-user graph. Episodic layer = Samu's `Visit/Note/Place/Tag` (compact, already used by the agent). Belief layer = Ara's `Pattern/LongTermMemory`, linked to `Visit`/`Note`. Keep raw frames in CE SQLite only, referenced by `event_id`. |
| D2 | **Three frame parsers** | `memory/frames.py`, `agent/models.py` (reuses `memory.frames._parse_classes`), `dreamer frames.py`, plus CE's `VideoObservation` | One adapter (raw frame → CE `VideoObservation`); everything downstream reads CE events |
| D3 | **Two short-term context layers** | CE `SessionBuffer` (raw window, alignment) vs agent `ContextTracker` (stable classes, place hysteresis) | Not true duplicates; they do different jobs. Keep CE as the store and `ContextTracker` as a semantic-state reducer **fed from CE events**. Give the agent the CE transcript window for multi-utterance context. |
| D4 | **Three dedup mechanisms** | CE `event_id` plus SHA-256; agent `LRUSet` and `content_key`; memory MERGE ids | Rely on CE for transport dedup. Keep the agent's `content_key` (it catches ASR re-segmentation). |
| D5 | **Two SQLite event stores** | The Dreamer creates its own `ContextEngine` (`bridge.py:185`) instead of reading Aryan's | The Dreamer reads the shared CE (HTTP `/events/export` or the shared DB path) |
| D6 | **Three semantic-extraction sites** | agent `interpret.py`/`context.py` (live), `memory/rules.py` + `segmenter.py` (batch), Dreamer (batch LLM) | Live semantic events: the episode writer and agent. Deep, cross-day facts: Dreamer. |
| D7 | **Note-to-fact keyword rules in three places** | `memory/store.add_note` (`remind me`, `I like`), `dreamer bridge.classify_note`, agent `RuleInterpreter` | One utterance classifier, run once at ingestion, with its result stored on the `Note` |
| D8 | **Conflicting assumptions about who writes Notes** | Agent README: "Ingestion stores every transcript as a Note". Context Engine: "no long-term memory". Dreamer: loads Notes itself. | Assign the episode writer (new, in master-repo) as the single writer of `Note`/`Visit` from CE events |
| D9 | **Naming** | Veyra (agent wake word, graph prefix, env vars `VEYRA_*`) vs Vixy | Pick "Vixy" and rename wake word, graph prefix and env vars together |
| D10 | **Time policy** | CE requires an offset; master-repo silently accepts naive timestamps | CE policy everywhere |
| D11 | **Product focus** | CE demo = allergy recall. Agent = calendar, reminders, transit. Dreamer = routines. | Choose one demo narrative that exercises all three (§13) |

**What has to change for the Context Engine and Active Agent to work together:**

1. Write an adapter: CE `AudioSegment`/`VideoObservation` → agent `AudioObservation`/`VisionObservation`, or change the agent's parsers to accept CE schemas.
2. Add a wearer flag. Either extend CE `AudioSegment` with optional `speaker_is_user`/`is_wearer` (a v1 → v1.1 bump, since `extra="forbid"` rejects it today), or derive it from `speaker_id` in the adapter.
3. Carry YOLO classes **and counts** into `detections` (add optional `count` to `Detection`) so the agent's place classifier works.
4. Add a runtime loop. On each CE ingest, call `agent.handle(event)`. On triggers, include the CE prompt (recent transcript plus scene) when the LLM is consulted.
5. Make the agent's top-level `latitude`/`longitude` read CE's nested `location`.
6. Make the agent's `Note` dependency explicit: the episode writer must persist each CE audio event as a `Note` *before* the agent handles it (as `sim.py` already does).

---

## 10. Tests and Demo Results

All commands ran in isolated `uv` venvs under a scratch directory, with `BASETEN_API_KEY`, `VEYRA_LLM_*`, `MODEL_API_KEY`, `FALKORDB_HOST` and `FALKORDB_URL` blanked. Outputs, SQLite and `.rdb` files went to the scratch directory, never into the repos. **No paid API was called.** I didn't run `yolomodelvision/*.py` or `dream` with Muse.

| # | Command (from the repo root unless noted) | Result |
|---|---|---|
| T1 | `context-engine$ python -m pytest -q` | **51 passed** (0.40 s) |
| T2 | `master-repo$ python -m pytest -q` (uv, `--frozen`) | **48 passed** (4.18 s) |
| T3 | `master-repo$ python -m agent.cli eval` | **14/14 scenarios PASS**: no-action, conflict, ambiguous speaker, location reminder, duplicates, irrelevant frames, uncertain obs, correction, DB failure, prompt injection, bus stop (mock), upcoming, selective LLM (FakeLLM, 2 calls), question lifecycle |
| T4 | `dreamer-model$ python -m pytest -q` (declared deps only) | **41 passed, 1 FAILED**: `test_bridge_round_trip…` raises `ModuleNotFoundError: pydantic`, because sibling `context-engine/src` is found but its dependency isn't installed |
| T5 | Same, after `uv pip install -e ../context-engine falkordblite` into the scratch venv | **42 passed** |
| T6 | `python -c "import redislite.falkordb_client"` in the dreamer venv (declared deps) | **ModuleNotFoundError**: the documented embedded mode can't work on a clean install |
| D1 | `python -m context_engine.demo --out-dir <scratch>` | **exit 0.** Duplicate retry detected, 9 day-1 records exported, day-2 context excludes day-1, restart retains history, 4 buffered / 9 evicted / 13 persisted |
| D2 | `python context-engine/examples/mock_memory_integration.py` | **exit 0** (MOCK provider, labelled) |
| D3 | `python -m memory.cli --db <scratch> --user audit demo` | **exit 0.** 11,166 frames → 39 visits → 9 facts. `explain` = "Seen on 5 separate days". Supermarket context returns the milk reminder. Clinic visit stored, but no fact. |
| D4 | `python -m agent.cli sim` | **exit 0.** 8.92 simulated h, 30,420 frames (29,905 filtered), 11 utterances, 7 delivered (0.79/h), `llm_calls=2` (FakeLLM). Correct conflict, correction, reminder and completion flows. |
| D5 | `python -m prod_dreamer_model.cli --out-dir <scratch> all --offline --days 14 --start-date 2026-10-19` (DB env vars → scratch) | **exit 0** in 12.5 s. 20,624 events accepted → 14 days / 20,618 frames / 6 notes in the graph → fallback: 10 patterns, 11 memories. **7/11 memories have `evidence_count=0`** (bug §5.10). |
| P1 | Integration probe (§10.1) | **6/18 checks pass** |

### 10.1 Mocked cross-repo integration probe

This was a scratch script that imports the three unmodified repos together. It didn't patch any source.

| Check | Result | Evidence |
|---|---|---|
| 1a YOLO frame → `ContextEngine.ingest_video` as-is | FAIL | Rejected: `classes, description, event_id, frame_id, session_id` |
| 1b YOLO frame via `dreamer frames.to_video_observation` | PASS | `description='person x2, cup x4'`, counts kept in `source` |
| 2a CE day-2 snapshot | PASS | `['d2-v1','d2-a1','d2-v2','d2-v3']` |
| 3a CE `AudioSegment` → `ActiveAgent.handle` | FAIL | `invalid observation: missing timestamp (timestamp / ts)` (no `transcript` key, so it's routed to vision) |
| 3b CE `VideoObservation` detections → agent classes | FAIL | `classes={}` (the agent reads `classes`; CE emits `detections`) |
| 3c Agent entry point for a CE snapshot | FAIL | Treated as a vision frame |
| 3d CE audio with a six-field rename adapter | PASS | Parses; `NO_ACTION` |
| 3e CE carries `speaker_is_user` | FAIL | Only `speaker_id`, so the agent never stores a commitment |
| 4a Dreamer CLI loads CE JSONL | FAIL | Subcommands are `all, dream, report, reset, seed` |
| 4b `EventBridge` reads CE history (shared engine, overridden session) | PASS | Bundle sizes `[9, 4]` |
| 4c Dreamer default session matches CE | FAIL | `wearer-01` vs `demo-user-01` |
| 4d Dreamer graph loaded from CE fixtures | PASS | `Note 5, Frame 8, Class 8, Day 2` |
| 4e Offline dreamer turns "Alex is allergic to peanuts." into LTM | **FAIL** | 0 memories |
| 4f Allergy utterance stored as `:Note` | PASS | `('d1-a2','note')` |
| 5a Same graph name for agent and dreamer | FAIL | `vixy_dreamer` vs `veyra_<user>` |
| 5b Agent `open_reminders()` on the Dreamer graph | FAIL | `[]`. Also re-run on a copy of the 14-day graph: raw `Note{kind:'reminder'}` exists with no `id`/`status`, so it's invisible to the agent. `preference_statements()=[]`. 11 LTM nodes are ignored. |
| 5c Agent code reads `:LongTermMemory`/`:Pattern` | FAIL | No occurrence in `agent/` |
| 6a `memory.frames.parse_frame` on a CE `VideoObservation` | FAIL | `classes={}` |

**Can the components run together today? No.**

- Each runs independently (VERIFIED).
- Joined, they fail at I1, I3, I6, I10 and I11.
- I4 works only with configuration overrides.

---

## 11. Proposed Target Architecture

I'd keep the intended architecture, with **four modifications**:

1. A real perception layer.
2. An explicit **episode writer** (the missing "Ingestion").
3. **One graph per user** shared by the agent and Dreamer.
4. A **memory API** that serves both the agent's live context and the app's natural-language questions.

```text
 Wearable / phone (camera 1 fps, mic, GPS)
        |  frames + audio chunks
        v
 PERCEPTION (local, cheap)                          new
   - detector -> {frame_id, timestamp, classes[{class,count,confidence}], gps}
   - ASR + VAD -> segments {start, end, text, speaker_id, is_wearer}
   - caption only on scene change (place_entered) -> VLM (rare)
        |  adapters (promote dreamer frames.to_video_observation)
        v
 CONTEXT ENGINE  (Aryan, unchanged core; v1.1 adds Detection.count, AudioSegment.is_wearer)
   SQLite event log = single source of truth for raw observations
        |                                     |
        | per-event callback (in-process)     | GET /events/export (half-open, per day)
        v                                     v
 VIXY RUNTIME (master-repo)                 DREAMER (Ara), nightly/cron per finished day
   1. Episode writer: segmenter -> :Visit,    - reads :Visit/:Note (+ CE transcripts)
      utterance -> :Note (single writer)       - Muse -> :LongTermMemory / :Pattern
   2. ContextTracker (fed CE events)            SUPPORTED_BY -> :Visit/:Note (CE event_ids)
   3. ActiveAgent: rules -> candidates;       - writes :Question{source:'dreamer'}
      memory-relevance check (LTM by entity,  - fallback = memory.store.consolidate
      LLM only when a candidate exists)                 |
   4. Memory API (FastAPI):                             |
      /context-for-agent, /ask, /timeline,              |
      /answer, /notifications (SSE)                     |
        |                 \                             |
        v                  \--------> FalkorDB server (Docker), graph vixy_<user_id>
 MOBILE PWA  <-- SSE notifications, answers, "ask about my past", timeline
```

**Key technical decisions**

| Decision | Recommendation | Rationale |
|---|---|---|
| Canonical raw store | CE SQLite | Already the most rigorous: dedup, conflicts, UTC, provenance hashes |
| Canonical graph | One graph `vixy_<user_id>` on a FalkorDB **server** (Docker) | Embedded `falkordblite` per process can't be safely shared by runtime and Dreamer. Docker is available on the dev machine. |
| Episode unit | Samu's `Visit` (+ `Note`), not per-frame nodes | About 40 nodes per week instead of 20k. Far better LLM input for the Dreamer. |
| Belief unit | Ara's `LongTermMemory` (+ optional `Pattern`), linked to `Visit`/`Note` and to entity nodes (`Person{name}`, `Place`) | Gives provenance with one hop, and lets retrieval look up memories by entity |
| Who extracts semantics | Live and cheap: episode writer + agent rules. Cross-day and deep: Dreamer (LLM). | Avoids LLM calls per frame |
| Identity | `user_id` (stable) = graph key; CE `session_id` = `user_id` for the MVP | Removes the `wearer-01` / `demo-user-01` / `demo` split |
| Process model | One runtime process hosts CE + agent + API in-process (CE's lock is single-process). Dreamer is a separate job. | Simplest correct topology |
| Fallback dreamer | Replace mock-tuned `heuristic.py` with `memory.store.consolidate` plus note rules | Already generic and tested on real cue tables |

---

## 12. Prioritized Roadmap

Five milestones. Each one ends in a demonstrable, testable state.

### M1 — Contracts, adapters and hygiene ("everything speaks CE v1.1")

| | |
|---|---|
| **Objective** | Any raw frame or transcript can enter the CE. The team shares one identity, time zone and naming convention. |
| **Reuse** | `dreamer frames.parse_frame/to_video_observation`; CE `adapters.py`; CE schemas |
| **Implement** | (a) CE v1.1: optional `Detection.count`, optional `AudioSegment.is_wearer` (or `speaker_is_user`), `description` optional for detector-only frames (or a documented synthesised caption). (b) Move the frame adapter into CE `adapters.py`. (c) An ASR adapter for whatever ASR is chosen. (d) Shared config: `VIXY_USER_ID`, `VIXY_TZ`, `FALKORDB_URL`. (e) Rotate the Baseten key, untrack `.env`, add `.env.example`. (f) Declare dreamer deps (`falkordblite`, `vixy-context-engine`). (g) Rename Veyra → Vixy, including the wake word. |
| **Repos** | context-engine, dreamer-model, master-repo |
| **Dependencies** | Team agreement on the contract (one page) |
| **Decisions** | Counts as a first-class field; `is_wearer` from mic/VAD heuristics first, a diarization embedding later |
| **Acceptance** | YOLO-style JSONL and Whisper-style JSONL replay into CE with 0 rejections; all three suites green on a clean install; `git log` contains no secrets going forward |
| **Demo** | `vixy replay day1.frames.jsonl day1.asr.jsonl` → CE snapshot shows aligned audio and scene |

### M2 — Live loop through the Context Engine ("the agent sees CE events and writes episodes")

| | |
|---|---|
| **Objective** | One runtime process: CE ingest → episode writer → agent decisions, against one FalkorDB graph |
| **Reuse** | `ContextEngine`, `memory.segmenter.Segmenter` (streaming `add`), `MemoryStore.add_visit/add_note`, `ActiveAgent`, `AgentMemory`, `sim.py` (as the template for ingestion ordering) |
| **Implement** | (a) A runtime module with a CE→agent adapter (≈30 lines, see §9). (b) An episode writer that persists `Note` before `agent.handle`, and `Visit` on segment close. (c) The agent's LLM path receives `render_prompt(snapshot)` for multi-utterance context. (d) A FastAPI wrapper reusing CE's `create_app`, plus `/answer` and an SSE `/notifications` Notifier. (e) A FalkorDB Docker server. |
| **Repos** | master-repo (runtime), context-engine (callback hook or in-process use) |
| **Dependencies** | M1 |
| **Decisions** | In-process CE (no HTTP hop for the live path); the agent's own `ContextTracker` kept as a reducer |
| **Acceptance** | `agent.cli eval` scenarios re-run **through CE** and produce identical decisions; restart-safe (dedup reload); p95 audio decision < 100 ms against a server FalkorDB |
| **Demo** | Replay a day; notifications stream to `curl -N /notifications` |

### M3 — Unified long-term memory ("the Dreamer dreams on the agent's graph")

| | |
|---|---|
| **Objective** | Nightly consolidation of `Visit`/`Note` into evidence-linked `LongTermMemory` in the **same** graph, including spoken facts about people |
| **Reuse** | `Dreamer`, `GraphToolBox`, `safe_cypher`, `ResponsesMuseClient`, `memory.store.consolidate` |
| **Implement** | (a) Re-target `graph.py` reads to `Visit/Note/Place/Tag` (drop `:Frame` loading, keep CE `event_id` refs). (b) Extend `write_memory` with `subject`/entity (`Person{name}`), `predicate`, `valid_from`, `supersedes`. (c) Fix the fallback evidence bug (count heuristic writes, or always call `backfill_evidence`). (d) Replace `heuristic.py` with `consolidate` plus a note fact extractor. (e) Incremental mode: dream only the newest finished day with a watermark; never `reset`. (f) Write `Question{source:'dreamer'}` for low-confidence beliefs. (g) Use `ro_query` for `run_cypher`. (h) Default to a **non-contributor** model for real data. |
| **Repos** | dreamer-model, master-repo (schema) |
| **Dependencies** | M2 graph populated |
| **Decisions** | One LLM pass per day; Muse sees episodes and notes, not frames |
| **Acceptance** | From the 2-day fixture: `LongTermMemory{subject:'Alex', statement:'Alex is allergic to peanuts', SUPPORTED_BY Note(event_id='d1-a2')}`; every LTM has ≥ 1 evidence edge; a re-run is idempotent; offline mode yields at least the deterministic facts |
| **Demo** | `vixy dream --day 2026-03-14` → `vixy memory explain <id>` cites the exact utterance and time |

### M4 — Retrieval and recall ("Vixy uses what it remembers")

| | |
|---|---|
| **Objective** | The agent uses long-term memory proactively; the user can ask natural-language questions about past days |
| **Reuse** | `AgentMemory` read pattern, `MemoryStore.day/last_seen/explain/context`, `GraphToolBox.run_cypher`, CE `render_prompt` (as the "observed" section), `examples/mock_memory_integration.py` (the intended prompt layout) |
| **Implement** | (a) `AgentMemory.memories_about(entities, keywords)`: entity and keyword match on `LongTermMemory`, with optional embeddings later. (b) A new agent handler: when an utterance or scene mentions an entity with memories (e.g. "Alex", "peanuts"), **one** LLM call judges relevance or risk and drafts the message, run through the existing gate. (c) `/ask`: an LLM with read-only graph tools plus a CE transcript lookup by `event_id`; answers cite evidence. (d) `/timeline?date=` from `MemoryStore.day`. |
| **Repos** | master-repo, dreamer-model (toolbox reuse) |
| **Dependencies** | M3 |
| **Decisions** | Retrieval by entity first (cheap, explainable). The LLM runs only when a deterministic candidate exists. |
| **Acceptance** | Day-2 replay produces a HIGH-priority NOTIFY citing day-1 `d1-a2`; `/ask "What is Alex allergic to?"` answers "peanuts", citing the time and source; ≤ 1 LLM call per triggering utterance, 0 per frame |
| **Demo** | The core of the hackathon demo (§13) |

### M5 — Mobile app, real capture, demo hardening

| | |
|---|---|
| **Objective** | Phone-accessible experience and a reproducible demo |
| **Reuse** | `memory/viz.py` (graph view), the runtime API from M2–M4 |
| **Implement** | (a) A PWA with notification feed (SSE), question answer buttons → `/answer`, an "Ask Vixy" box → `/ask`, and a daily timeline. (b) Phone or wearable capture client: camera at 1 fps → local detector, mic → ASR, posting to CE. (c) `make demo`: one command that replays fixtures through everything and checks the outcomes. |
| **Repos** | master-repo (or a new `app/`) |
| **Dependencies** | M2–M4 |
| **Acceptance** | Fresh clone + `docker compose up` + `make demo` passes; the live capture path works on one real session |
| **Demo** | §13 on a phone |

**Model plan.** Availability below is from Meta's public docs only, fetched during this audit. **Run `GET https://api.meta.ai/v1/models` with the team key to confirm account access before relying on any of it.**

| Task | Frequency | Recommendation |
|---|---|---|
| Object/scene detection | every frame | **Local**, open-source YOLO-family detector (check the license: Ultralytics is AGPL) or an open-vocabulary detector restricted to the rule cue vocabulary. Replace the Baseten 18-requests-per-frame approach. |
| Scene caption | only on `place_entered` / scene change | `muse-spark-1.3` with image input (listed with text/image/video/audio/PDF input), or skip it and synthesise the caption from detections |
| ASR | continuous | Local open-source Whisper variant, or Meta `muse-voice-transcribe-1.0` (listed; realtime `wss://api.meta.ai/v1/asr/realtime`; pricing NOT VERIFIED) |
| Agent uncertain-intent and relevance check | ≤ a few per hour | `muse-spark-1.3` through the existing `OpenAICompatibleLLM` (Meta lists Chat Completions for Muse Spark). Set `VEYRA_LLM_BASE_URL=https://api.meta.ai/v1`; NOT VERIFIED against the live endpoint. |
| Dreaming | 1× per day | `muse-spark-1.3`. Use `-contributor` **only** for synthetic demo data: per Meta's docs, contributor mode lets Meta train on your content. |
| `/ask` Q&A | on demand | `muse-spark-1.3` with read-only graph tools |

---

## 13. Recommended End-to-End Demo Scenario

**"Vixy remembers for you"**: two days, replayed from fixtures and optionally performed live. It exercises every component.

**Day 1, evening, kitchen and phone call**

| Input | Effect |
|---|---|
| Frames: kitchen, two people | CE observation → episode writer creates a `Visit(home)` |
| Friend: *"Alex is allergic to peanuts."* (`is_wearer=false`) | CE `d1-a2` → `Note` |
| Wearer: *"Remind me to buy milk at the supermarket."* | `Note{kind:reminder, open}` |
| Wearer: *"I'll meet Alex tomorrow at 6pm."* | Agent: `MEMORY_UPDATE` (tentative commitment). If a dentist appointment is pre-loaded at 18:00, a HIGH `NOTIFY` conflict is raised **(already working, `scenarios.py`)**. |

**Night, Dreamer**

- `LongTermMemory: "Alex is allergic to peanuts"` (subject `Alex`, evidence `d1-a2`).
- Facts: `frequent_place` etc.

**Day 2, grocery store**

| Input | Effect |
|---|---|
| Frames: `supermarket`, `shopping cart` | `place_entered` → NOTIFY "You're at the supermarket. Reminder: buy milk" **(already working)** |
| Wearer: *"I'm getting peanuts for Alex."* + frame "jar of peanuts" (aligned by CE) | Agent finds an LTM about `Alex` + `peanut` → one LLM relevance call → HIGH NOTIFY: "Heads up: yesterday at 18:30 someone said Alex is allergic to peanuts." The app shows the cited utterance. |
| Wearer: "I bought the milk" → "yes" | Reminder completed **(already working)** |

**In the app**

- "What did I do yesterday?" → timeline.
- "What is Alex allergic to?" → "Peanuts. Mentioned on Sat 14 Mar at 18:30 (kitchen)."
- "Why do you think that?" → evidence.

**Pass criteria (automated):**

- Exact notifications and their priorities.
- Every memory has evidence.
- 0 LLM calls on frames.
- ≤ 3 LLM calls on day 2.
- The re-run is deterministic in offline mode, except for the Muse-dependent statements.

---

## 14. Top Five Immediate Actions

1. **Rotate the Baseten API key and untrack `master-repo/.env`** (`git rm --cached .env`, add it to `.gitignore`, commit `.env.example`). Decide as a team whether to rewrite history. The key has been exposed since `aa020ad`.
2. **Agree on one data-contract page (CE v1.1)**:
   - Raw frame JSON, with `count` first-class.
   - The `AudioSegment` wearer flag.
   - `user_id`/`session_id` = one value.
   - UTC ISO with an offset everywhere.
   - "Vixy" as the name and wake word.

   This unblocks I1, I2 and I3.
3. **Decide on the single FalkorDB schema and graph** (`vixy_<user_id>`: Samu's episodic + agent labels, Ara's `LongTermMemory`, entity nodes) and stand up a FalkorDB Docker server. This unblocks I6 and removes D1/D5.
4. **Build the runtime glue in master-repo** (M2): CE → episode writer (`Visit`/`Note`) → `ActiveAgent`, with the six-field adapter. This is the "Ingestion" both the agent and memory READMEs assume.
5. **Make the Dreamer reliable on the shared graph** (M3 subset):
   - Declare `falkordblite` and `vixy-context-engine`.
   - Fix the fallback evidence bug (`dreamer.py:218-230`).
   - Read `Visit`/`Note` instead of `Frame`.
   - Extract person-linked facts (the allergy line).
   - Switch the default off the `-contributor` model for anything that isn't synthetic.

---

## Explicit Answers

**Is the Active Agent consuming raw sensory data or contextualised events?**
Raw. It parses YOLO-style `classes` frames and individual raw transcripts in its own schema (`agent/models.py:41-119`). It never calls the Context Engine. Its only context is its own 8-frame tracker.

**Who performs semantic event extraction?**
No single owner, and nothing is persisted as shared events:

- Live: the Active Agent (`context.py` triggers, `interpret.py` intents, `temporal.py`).
- Batch: Samu's `memory/` (`segmenter.py`, `rules.py`, `consolidate`) and Ara's Dreamer (Muse, or `heuristic.py`).
- The Context Engine explicitly doesn't.
- No scene captioning exists.

**Are the Context Engine and Active Agent duplicating functionality?**
Partly. Both do input validation and normalisation, event dedup, and a short-term rolling window (D2–D4). They are complementary in substance: CE is a durable aligned observation store, and the agent is a semantic-state reducer plus decision maker. The real duplication is elsewhere: **two long-term memory graphs** (Samu's `memory/` vs Ara's Dreamer) and **three frame parsers**.

**Can the Dreamer consume Aryan's exports without modification?**
Not the JSONL files and not the HTTP export: there's no loader, and its CLI only seeds from mock data. It can read Aryan's event history only by sharing the same `ContextEngine` / SQLite file **and** overriding `session_id`, since it defaults to `wearer-01` (probes 4a–4c). The record format itself (`DreamerEventRecord`) is fully compatible, because the Dreamer imports Aryan's own classes.

**Does the Dreamer actually store persistent knowledge in FalkorDB?**
Yes, VERIFIED with embedded `falkordblite`: 14 days → 20,618 `Frame`, 14 `Day`, 6 `Note`, plus 10 `Pattern` and 11 `LongTermMemory` nodes with MERGE-idempotent writes. Caveats:

- Embedded mode needs an undeclared dependency.
- Server mode is NOT VERIFIED.
- 7/11 offline memories lack evidence (bug).
- `all` wipes the graph on each run.

**Can the Active Agent retrieve consolidated memories?**
No. It reads only its own graph (`veyra_<user>`) and only `Commitment`, reminder `Note`, `Place` and preference `Fact`. It has no query for `:LongTermMemory` or `:Pattern`. Even pointed at the Dreamer's graph, it retrieves nothing because the `Note` schemas differ (probe 5b). It *can* use Samu's own consolidated `Fact`s, but only preference statements, and only to filter bus routes.

**Is the system currently executable end-to-end?**
No. Each component runs and passes its own tests (51 + 48 + 14 scenarios + 42 after the dependency fix). The mocked integration probe passed 6/18 interface checks. There's no perception producer, no CE consumer, no shared memory graph, no notification delivery and no frontend.

**What is the minimum remaining work to demonstrate a fully functional Vixy MVP?**
Integration plus three genuinely missing capabilities:

- **Integration** (largest share): M1 contracts and adapters, M2 runtime glue (CE → episode writer → agent, SSE delivery), and the M3 schema merge.
- **Missing capabilities:**
  1. A perception front end: a local detector and ASR with a wearer flag (M1/M5).
  2. Memory-aware proactive reasoning plus natural-language recall over the shared graph (M4).
  3. A mobile PWA (M5).

For a fixture-replayed hackathon demo (§13), M1–M4 are sufficient, and M5 can be a thin web page. Real-device capture is the last step.

---

### Appendix A — Commands used

```bash
# setup (workspace root)
git clone https://github.com/meta-hackathon-vixy/{master-repo,context-engine,dreamer-model}.git
git -C master-repo fetch --all --prune && git -C master-repo switch --track origin/feature/active-agent

# isolated envs (outside the repos)
uv venv --python 3.12 $S/venvs/ce && VIRTUAL_ENV=$S/venvs/ce uv pip install -e 'context-engine[api,dev]'
(cd master-repo && UV_PROJECT_ENVIRONMENT=$S/venvs/mr uv sync --frozen)
(cd dreamer-model && UV_PROJECT_ENVIRONMENT=$S/venvs/dm uv sync)        # then: uv pip install -e ../context-engine falkordblite

# all runs with: BASETEN_API_KEY= VEYRA_LLM_MODEL= MODEL_API_KEY= FALKORDB_HOST= FALKORDB_URL=
python -m pytest -q                                   # in each repo
python -m agent.cli eval ; python -m agent.cli sim    # master-repo
python -m memory.cli --db $S/mr_memory.rdb --user audit demo
python -m context_engine.demo --out-dir $S/ce_demo ; python context-engine/examples/mock_memory_integration.py
DREAMER_DB_PATH=$S/dm_graph.rdb DREAMER_EVENTS_DB=$S/dm_events.sqlite3 \
  python -m prod_dreamer_model.cli --out-dir $S/dm_out all --offline --days 14 --start-date 2026-10-19
python -I $S/integration_probe.py <workspace> $S      # cross-repo probe (scratch only, imports repos read-only)
```

All generated artifacts (`__pycache__`, `*.egg-info`, the `uv.lock` that `uv sync` created in dreamer-model) were removed afterwards. `git status --ignored` is empty in all three repos.

### Appendix B — External sources consulted for model availability

- Meta Model API, model catalogue: https://dev.meta.ai/docs/models.md (redirected from https://ai.developer.meta.com/docs/models/). Lists `muse-wonder-1.0`, `muse-spark-1.3`, `muse-spark-1.3-contributor`, `muse-spark-1.2(-contributor)`, `muse-spark-1.1`, `muse-image-1.0`, `muse-voice-transcribe-1.0`, `sam-3.1`, and the `GET /v1/models` endpoint. Contributor mode = Meta may train on your content.
- Promptfoo provider notes (Llama API retired 2026-07-06, replaced by the Meta Model API): https://www.promptfoo.dev/docs/providers/meta/
