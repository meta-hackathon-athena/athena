# Vixy: Milestone 1 implementation report

**Date:** 2026-11-04. **Scope:** unified architecture and system integration, as set out in `VIXY_TARGET_ARCHITECTURE_EN.md` (the authoritative target) and `VIXY_SYSTEM_AUDIT.md`.

**Status: accepted; committed locally.** One local commit per repository on `integration/m1-unified` (SHAs in §12). Nothing was pushed, merged, deployed or billed.

## 1. Summary

The three components now run as one executable workflow, with one FalkorDB graph per user:

```text
perception-shaped input
  → adapters
  → Context Engine (SQLite, canonical)
  → Episode Writer (Note/Visit)
  → Active Agent (decisions + AgentLog)
  → graph vixy_<user_id>
  → Dreamer (offline consolidation into Pattern/LongTermMemory, with real evidence)
  → shared read-only memory API (used by the runtime, the CLI and the agent)
```

What `python -m vixy demo --fresh` exercises, on both the shared FalkorDB server (Docker) and the embedded file:

- It replays three synthetic days (1,413 events) through the full chain.
- It produces the agent's existing decisions from Context Engine events: a HIGH conflict alert, a supermarket reminder, the completion question and its answer, and upcoming-commitment heads-ups.
- Replaying everything changes nothing.
- An offline Dreamer run writes 6 evidence-backed memories into the same graph without touching operational records.
- It traces `Marta has a presentation on Thursday at 9 AM.` → `Note` → the original Context Engine event, with a matching SHA-256.

The demo ends with 9/9 self-checks passing.

## 2. What changed in each repository

### `context-engine` (Aryan): 3 files modified, 1 test file added, +37 lines

No schema change.

- `ContextEngine.get_event_record(event_id)` and `records_after(session_id, after_seq, limit)`, backed by `EventStore.get` and `query_after_seq`. These are read-only consumer APIs for provenance checks and restart recovery.
- `tests/test_consumer_reads.py` (2 tests).
- README section "Consumer reads".

### `dreamer-model` (Ara): 13 files modified, 4 added (+708/−85, plus `consolidate.py` with 273 lines)

**Dependencies**

- `falkordblite` is now declared, with a `python_version >= '3.12'` marker because that's what falkordblite requires.
- `vixy-context-engine` is an optional extra and part of the dev group, resolved from the sibling checkout through `[tool.uv.sources]`.
- New `uv.lock`. A clean `uv sync --frozen` now passes all tests with no manual installs (previously 41/42).

**Privacy**

- The default model is `muse-spark-1.3`, no longer `-contributor`.
- `ResponsesMuseClient` refuses contributor models unless `DREAMER_ALLOW_CONTRIBUTOR_MODEL=1`.
- `MUSE_STORE_RESPONSES` is configurable. The default stays on, because `previous_response_id` chaining requires it.

**Evidence bug fixed** (audit §5.10)

- The fallback's writes now count toward the tool-usage counters, so evidence finalization runs after every dream.
- New `GraphToolBox.finalize_evidence()`. It inherits pattern evidence, which is genuine. It runs the window-matched `backfill_evidence` only if `DREAMER_ALLOW_INFERRED_EVIDENCE=1`, because that provenance is invented. Then it calls `ProdGraph.enforce_evidence()`, which marks evidence-less beliefs `unconfirmed`.
- On the legacy 14-day offline run, 11 of 11 memories now have evidence: 77 propagated links and 0 inferred. Before the fix, 7 of 11 had none.

**Unified layout** (`graph_layout="unified"`, `--layout unified --user <id>`)

- Graph name: `vixy_<user_id>`.
- Reads `Visit` and `Note`. `ABOUT` points to `Tag` or `Person`. Evidence must be a `Visit.id`, `Note.id` or `Note.note_id`.
- New status value `unconfirmed`.
- Every belief now gets a common `id` key and the `layer` property.
- `DERIVED_FROM` can point to a `Fact`. `(:Person)` and `(:DreamRun)` are new.
- `reset`, `all` and `load_bundle` refuse to run on a unified graph.
- `run_cypher` uses `GRAPH.RO_QUERY`.
- The Muse prompts, schema brief and briefing are layout-aware.

**`consolidate.py`: generic deterministic offline consolidation**

- Routines: a place type seen on 3 or more days becomes a `Pattern` and a `LongTermMemory`.
- Fact promotion: a place Fact is merged into the routine memory for that place type instead of being duplicated.
- Statements about named people become a `LongTermMemory` plus a `Person`. Every note is verified against the original Context Engine event first (same text, same hash).
- It is not tuned to the mock vocabulary, and no phrase is hard-coded.

**Context Engine history**

- `bridge.EventHistory` reads from a shared engine, the runtime's SQLite file, or a JSONL export. The Dreamer never creates its own store.
- `load_export_jsonl`, `bundles_from_records`, and a `cli load` command (fixes audit probe 4a).

**Smaller changes**

- `frames.to_video_observation` takes `stream` and `pipeline` parameters; the defaults are unchanged.
- Report rendering shows Visit ids as evidence.
- README and `.env.example` updated.
- Tests: `test_unified.py` (7) and `test_evidence.py` (1), both against real embedded FalkorDB and Context Engine.

### `master-repo` (Samu, plus the integration entry point): 18 files modified (+526/−55); new `vixy/` package (1,457 lines), docs, tests, compose file

**Security**

- `.env` is untracked (`git rm --cached`, staged) and ignored. The local copy is preserved.
- New `.env.example` with placeholders and a rotation notice.

**Packaging**

- `pyproject.toml` depends on `vixy-context-engine` and `prod-dreamer-model` as editable sibling paths.
- `uv.lock` regenerated.

**`vixy/` (new)**

| module | purpose |
|---|---|
| `config.py` | Identity, storage, and connecting to the shared graph |
| `contracts.py` | `vixy.source.v1`, speaker roles, schema versions |
| `adapters.py` | Perception and ASR input to Context Engine; Context Engine events to the agent and the Segmenter. Reuses Ara's and Aryan's adapters. |
| `schema.py` | Versioned bootstrap, merged index set, ownership table |
| `ledger.py` | Per-event side-effect progress and segmenter checkpoint |
| `episode_writer.py` | The single writer of `Note`, `Visit` and `Day` |
| `runtime.py` | `VixyRuntime`: the runtime itself |
| `memory_api.py` | `SharedMemory`, read-only |
| `dream_job.py` | Runs the Dreamer on the runtime's graph and history |
| `demo_data.py`, `cli.py` | Demo data and the `python -m vixy` CLI |

**`memory/` (Samu)**

- `parse_ts` rejects naive ISO strings (Context Engine policy). This fixes the machine-local-time bug.
- `graph_for` and `graph_name` use the `vixy_` prefix; `VIXY_GRAPH_PREFIX=veyra` opens legacy graphs.
- `FALKORDB_URL` support. `VIXY_DB_PATH` and `VIXY_TZ`, with the `VEYRA_*` names as fallbacks.
- `add_note(note_id=…, attribute_to_user=…, props=…)`. Someone else's "remind me" or "I like" is never assigned to the user, and a replay never reopens a completed reminder.
- `Segmenter.open_frames()`, plus `Visit.last_frame_id` and `frame_ids`.

**`agent/` (Samu)**

- The wake word is "Vixy"; "Veyra" is still accepted.
- `Decision.trigger_event_id` is set on every decision, stored in `AgentLog`, and kept on retried triggers.
- `ActiveAgent(context_provider=…)`: the LLM path receives the rendered Context Engine snapshot.
- `record_commitment` links `SUPPORTED_BY` to the exact `Note{source_event_id}`. The text-plus-±2-minute match now applies only to legacy notes without an event id.
- `AgentMemory.long_term_memories()` and `memory_evidence()`.
- The dedup-history reload is relative to the newest `AgentLog` entry, so replays behave after a restart.
- `VIXY_LLM_*` environment names, with `VEYRA_*` fallbacks.
- `CONCERNS` can point to `Visit`, `LongTermMemory` and `Pattern`.
- Reminder text in messages loses its trailing punctuation.

**Infrastructure and docs**

- `docker-compose.yml`: FalkorDB pinned by digest, port 6390, named volume with AOF.
- `docs/ARCHITECTURE.md`, `docs/GRAPH_SCHEMA.md`, `docs/CONTRACTS.md`, and the root `README.md` (previously empty).
- Notes added to `agent/README.md`, `memory/README.md` and `memory/schema.cypher`.

**Tests:** `tests/test_integration.py` (20) and `tests/test_m1_units.py` (5).

## 3. Architecture

```text
 synthetic perception (vixy/demo_data.py)  frames{frame_id,timestamp,classes[{class,count,confidence}],gps}
                                           ASR{segment_id,start,end,text,speaker_id?,speaker_is_wearer?,confidence}
        │  vixy/adapters.py  (prod_dreamer_model.frames.to_video_observation, context_engine.adapters)
        ▼
 ┌──────────────────────────── master-repo · VixyRuntime (one process, in-process calls) ──────────────────────────┐
 │ session check ─► ContextEngine.ingest_* (validate · dedupe · SQLite commit · buffer · alignment)               │
 │                     │ accepted / unfinished duplicate                                                          │
 │                     ▼                                                                                          │
 │ Ledger.begin ─► EpisodeWriter: audio → Note (BEFORE agent) · video → Segmenter → Visit (+Place, Tag, Day)      │
 │                     ▼                                                                                          │
 │ ActiveAgent.handle(CE event) ── context_provider ─► ContextEngine.get_context (snapshot for the LLM path)      │
 │   ContextTracker · rules · commitments · reminders · conflicts · questions · gate · AgentLog{trigger_event_id} │
 │                     ▼                                                                                          │
 │ Ledger.done ─► EventOutcome {processed|duplicate|recovered|rejected|conflict|failed(stage)}                    │
 └───────────────────────────────────────────────┬────────────────────────────────────────────────────────────────┘
                                                 ▼  MERGE on stable ids; allow-listed agent writes
       FalkorDB server (docker compose, :6390) — ONE graph  vixy_<user_id>  (schema vixy.graph.v1)
         observed: Note Visit Day · entity: Place Tag Person · operational: Commitment Question AgentLog
         inferred: Fact Pattern LongTermMemory ─SUPPORTED_BY→ Note|Visit ─source_event_id→ CE SQLite
                                                 ▲  same server, same graph
 ┌──────────────── Dreamer job (vixy dream | prod_dreamer_model.cli --layout unified dream) ─────────────────────┐
 │ MemoryStore.consolidate (Visit → Fact) → Dreamer.dream (Muse loop, or offline consolidate.unified_dream)       │
 │ → finalize_evidence (inherit · no inferred backfill · evidence-less ⇒ unconfirmed) · EventHistory = runtime CE  │
 └────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
        SharedMemory (GRAPH.RO_QUERY): notes/visits by day · LTM/Pattern · evidence · commitments · reminders
```

## 4. Exact runtime data flow (per event)

1. **Adapter.** Raw perception output becomes a validated `VideoObservation` or `AudioSegment`.
2. **Session check.** The event's session must belong to `VIXY_USER_ID`; otherwise the outcome is `rejected` and nothing is stored.
3. **Context Engine.** `ContextEngine.ingest_*` commits the event to SQLite.
   - Identical re-send whose ledger status is `done`: `duplicate`, with no side effects.
   - Same id with a different payload: `conflict`.
4. **Ledger.** `Ledger.begin(event_id, ingest_seq)`.
5. **Episode Writer.**
   - Audio: write the Note. Its id is `note_<sha1(event_id)>`, with provenance, speaker role, CE hash, and `ON_DAY`.
   - Video: feed the Segmenter and save its checkpoint. A visit that closes is stored with `add_visit`, provenance, `ON_DAY`, and `DURING` links from Notes inside it.
   - A failure here gives `failed` at stage `episode`.
6. **Ledger** status `episode_done`.
7. **Agent.** `ActiveAgent.handle(agent_observation(event))` runs. If its memory was unavailable, the outcome is `failed` at stage `agent`.
8. **Ledger** status `done`, with the decision ids.
9. **Start-up or `recover()`.**
   - The open visit is rehydrated from Context Engine.
   - Every Context Engine event that isn't `done` is finished (`recovered`).
   - Triggers the agent queued during an outage are retried.

## 5. Final graph schema (`vixy.graph.v1`; full reference in `master-repo/docs/GRAPH_SCHEMA.md`)

| Layer | Labels | Writer |
|---|---|---|
| observed | `Note`, `Visit`, `Day` | Episode Writer |
| entity | `Place`, `Tag` (Episode Writer); `Person` (Dreamer) | as shown |
| operational | `Commitment`, `Question`, `AgentLog`, plus `Note.status` | Active Agent (allow-listed ops) |
| inferred | `Fact` (user-stated: Episode Writer; inferred: Dreamer job); `Pattern`, `LongTermMemory` | as shown |
| meta | `VixyMeta` (schema version, owner); `DreamRun` (audit) | runtime / Dreamer |

Relationships:

```text
Note-DURING→Visit, Note|Visit-ON_DAY→Day, Visit-AT→Place, Visit-SAW→Tag, Visit-NEXT→Visit,
Fact-ABOUT→Place, Fact-SUPPORTED_BY→Visit|Note, Commitment-SUPPORTED_BY→Note (exact event),
Question-ABOUT→Commitment|Note, AgentLog-CONCERNS→*, Pattern|LTM-SUPPORTED_BY→Visit|Note,
Pattern|LTM-ABOUT→Tag|Person, LTM-DERIVED_FROM→Pattern|Fact
```

Conflicts resolved:

| Conflict | Resolution |
|---|---|
| `Note.id` vs `Note.note_id` | Both kept. `id` is the graph key; `note_id` and `source_event_id` hold the CE event id. Both teams' queries work unchanged. |
| Graph names (`veyra_<user>` vs `vixy_dreamer`) | One name: `vixy_<user>` |
| `Class` vs `Tag` | `Tag` |
| `Frame` per frame vs episodes | `Visit` episodes; raw frames stay in SQLite, referenced by event id |
| `Fact` vs `LongTermMemory` | `Fact` is a structured attribute; `LongTermMemory` is the single memory surface, `DERIVED_FROM` the Fact |
| Timestamps | An explicit offset is required everywhere |
| User/session identity | `VixyMeta.user_id`, plus `session_id` and `user_id` on every observed node |

## 6. Event contracts and adapters (full reference in `master-repo/docs/CONTRACTS.md`)

Context Engine v1 schemas are unchanged and still `extra="forbid"`. The values they have no field for travel in `event.source` as a validated `vixy.source.v1` object:

- `user_id`, `adapter`, `pipeline`, `stream`
- `frame_id` and `counts`; `counts` holds only counts the detector reported
- `segment_id`, `recording_id`, `speaker_is_wearer` (only from an explicit flag), `language`

This keeps stored events and hashes compatible. A Context Engine v1.1 with a first-class `Detection.count` and `speaker_is_wearer` is deferred to the perception milestone.

Conventions:

- **Event ids:** video `<stream>:<frame_id>`; audio `asr:<recording_id|stream>:<segment_id>`.
- **Sessions:** a session belongs to a user through `VIXY_SESSION_IDS` (default: the user id).
- **Time:** UTC ISO with an offset.
- **GPS:** never fabricated.
- **Speakers:** an unknown speaker is `unknown`, never the wearer.
- **Agent mapping:** `event_id`→`segment_id`/`frame_id`, `text`→`transcript`, `detections`+`counts`→`classes`, `location`→`latitude`/`longitude`, `speaker_is_wearer`→`speaker_is_user`.

## 7. How the Agent and the Dreamer share memory

- **Same server and graph.** Both open `vixy_<user_id>` on the same FalkorDB server (`FALKORDB_URL`).
  - `dream_job.dreamer_config()` derives the Dreamer's graph, user, session, timezone and store from the runtime config.
  - Tests check `dreamer_config(cfg).graph_name == runtime graph name`.
  - On the server backend, Ara's own CLI runs in a **separate process**, consolidates the runtime's graph, and the runtime then reads the result.
- **What the Dreamer reads.** The Episode Writer's `Note`s and `Visit`s, and the shared Context Engine SQLite (to verify notes). It never loads frames, never creates an event store, and never resets.
- **What the agent reads.** The Dreamer's memories, through `AgentMemory.long_term_memories()` and `memory_evidence()`, which use `SharedMemory` on the same graph with read-only queries.
- **Write isolation.** Each label has one writer (§5). Tests confirm that every Commitment, Question, AgentLog, Note and Visit, with its status, is identical before and after a dream.

## 8. Install and run

```bash
# workspace with the three sibling clones; Python 3.12; uv
cd master-repo
uv sync                                      # reproducible from uv.lock (context-engine + dreamer-model as editable path deps)
docker compose up -d falkordb                # shared FalkorDB on localhost:6390
export FALKORDB_URL=redis://localhost:6390   # omit to use the embedded file (single process)
uv run python -m vixy demo --fresh           # the complete Milestone 1 pipeline, 9 self-checks, exit 0 on success
uv run python -m vixy memories --about Marta
uv run python -m vixy explain <memory_id>    # memory → derivation → Note/Visit → context-engine event id
uv run python -m vixy dream | status | recover | flush | day 2026-11-02 | ingest events.jsonl
# Dreamer as its own process on the same graph:
cd ../dreamer-model && uv sync && FALKORDB_URL=redis://localhost:6390 uv run python -m prod_dreamer_model.cli \
    --layout unified --user demo dream --offline --history-db ../master-repo/data/demo/vixy_events.sqlite3
```

## 9. Actual test results

Every command below ran with `BASETEN_API_KEY`, `MODEL_API_KEY`, `VEYRA_LLM_MODEL`, `VIXY_LLM_MODEL` and `FALKORDB_HOST` blanked. No paid API was called.

| Suite | Before (baseline, re-run today) | After |
|---|---|---|
| context-engine `pytest` | 51 passed | **53 passed** (51 original + 2 new) |
| dreamer-model `pytest` | 41 passed / 1 failed (undeclared deps); 42 with manual installs | **50 passed** after a clean `uv sync --frozen` (42 original + 8 new) |
| master-repo `pytest` (embedded + server) | 48 passed | **73 passed** (48 original + 5 unit + 20 integration, of which 9 run on the Docker FalkorDB server) |
| `python -m agent.cli eval` | 14/14 | **14/14** |
| `python -m agent.cli sim` | 7 delivered / 30,420 frames / 2 LLM calls (FakeLLM) | identical |
| `context_engine.demo`, `memory.cli demo`, dreamer legacy `all --offline --days 14` | exit 0 | exit 0. Dreamer: 11 of 11 memories now have evidence (was 4 of 11) |
| `python -m vixy demo --fresh` (embedded and server) | n/a | **9/9 checks**; 1,413 events processed; replay gives 1,413 duplicates with the graph unchanged |
| Clean copy of the three working trees → `uv sync --frozen` → `pytest` + demo | n/a | 64 passed, 9 skipped (server variants, no URL set), demo 9/9 |

Integration-suite coverage against the requested checks (`tests/test_integration.py`):

| Check | What is verified |
|---|---|
| **A. Ingestion** | Every event is accepted by Context Engine. An invalid one is rejected and never reaches the agent. |
| **B. Normalization** | Ids, UTC times, counts (none invented), confidences, GPS and speaker flags are preserved (true/false/unknown); naive times are rejected. |
| **C. Episode writing** | 9 Notes, each with its CE id and hash; 11 Visits with first/last frame event ids; zero `Frame` nodes. |
| **D. Agent decisions** | A HIGH conflict alert with `trigger_event_id` set to the utterance's event; one supermarket reminder; the reminder completed. The commitment is `SUPPORTED_BY` the exact Note. Unknown speakers create no preferences. |
| **E. Shared graph** | Same graph name and schema. On the server, the Dreamer CLI runs as a separate process. |
| **F. Dreamer isolation** | Operational and observed records identical before and after. No active belief without evidence, and all evidence points to a Note or Visit. |
| **G. Retrieval** | Memory → Person/Note → CE record with the same text and SHA-256. The agent reads the same memory. |
| **H. Idempotency** | Replaying events and re-running the dream produce an identical graph and event count. Conflicting re-sends and foreign-session events are refused. |
| **I. Persistence and recovery** | Restart in a new process in the middle of a visit gives a graph identical to an uninterrupted run. Memories survive a process restart. A graph outage is reported as `failed` and later `recovered` with no duplicates. A crash after the Context Engine commit is finished at start-up. |
| **J. Regression** | The suites in the table above. |
| Also | The agent's LLM path receives the Context Engine snapshot; the schema version and owner are enforced; every indexed label is documented. |

## 10. Remaining limitations

1. **FalkorDB 6.0.3 server bug (found during this work).** The pinned server image panics (`reply.rs:668 unwrap on None`) on `OPTIONAL MATCH … WHERE (label OR label)` followed by another `OPTIONAL MATCH` and a `collect(DISTINCT …)`. Docker restarts the server and AOF preserves the data. Embedded falkordblite runs FalkorDB 4.22.1 and does not crash. The two affected queries were rewritten with pattern comprehensions, and a full suite run produced no new panics. This is worth reporting upstream and keeping in mind for future Cypher.
2. **Embedded mode is single-process.** Only the server is shared storage. The image is `linux/amd64`, so it runs under emulation on Apple Silicon.
3. **Some in-memory state doesn't survive a restart:**
   - The agent's ContextTracker. Place hysteresis re-settles from new frames.
   - The agent's in-process retry queue.

   Notification dedup *is* restored from `AgentLog`, and unfinished events are restored by the ledger.
4. **A visit is written only when it closes** (at a gap, a scene change, or `flush`). The open visit isn't visible to the agent or the Dreamer until then, but it is rebuilt after a restart.
5. **Offline consolidation is deliberately simple.**
   - The person-statement rule is English-only regex over capitalized names. It may keep transient statements such as "Alex is coming at 6".
   - `Person` identity is just the name as heard; there is no entity resolution.
   - There's no contradiction or supersession handling beyond retiring a memory whose Fact lost support.
6. **The agent doesn't yet use long-term memory in its decisions.** It can read the memories; using them is Milestone 2.
7. **Untested paths.**
   - The Muse online dream loop in the unified layout is wired and unit-tested with a scripted client, but has not been run live (not authorized).
   - The agent's 14 eval scenarios still drive the agent directly. The integration test covers the main behaviours through Context Engine; re-running the whole eval through the runtime is not done.
8. **Context Engine schema stays at v1.** Counts and the wearer flag live in `source` metadata.
9. **No perception.** `yolomodelvision/` is unchanged and still calls a paid API. It is not used.

## 11. Deferred to Milestone 2

- Meta-model consolidation over the unified graph, including contradictions and supersession.
- Entity resolution and structured facts (subject, predicate, object, valid time).
- Dreamer-authored `Question{source:'dreamer'}` nodes.
- Memory-assisted proactive decisions, such as the allergy-recall scenario.
- Natural-language question answering over `SharedMemory`.
- Incremental and scheduled dreaming with a watermark.
- Re-running the agent eval through Context Engine.

## 12. Git branch and working-tree status (no remote changes)

Final local commits (2026-11-04), one per repo, each on top of the audited commit:

| Repo | Original branch (untouched) | Branch | Final commit | Working tree |
|---|---|---|---|---|
| master-repo | `feature/active-agent` @ `32e0676`; `main` @ `0deecd3` | `integration/m1-unified` (local) | `a3991569450e7c5194a3af305c42fbb809b42c4f` | clean |
| context-engine | `main` @ `a876597` | `integration/m1-unified` (local) | `685413299a69e3ad694c847dfb56c6bb0148cb68` | clean |
| dreamer-model | `main` @ `02a79ba` | `integration/m1-unified` (local) | `68ce46e4135427ec444b732bee5aae322b1ff193` | clean |

- **`.env`.** It is no longer tracked from `a399156` on. It is gitignored, and the local copy is preserved. `.env.example` holds placeholders only; a scan found the key's value in no committed file.
- **Git operations.** Nothing was pushed, merged, force-pushed, rebased or reset, and history was not rewritten.
- **Re-run after committing.** context-engine 53 passed; dreamer-model 50 passed (clean `uv sync --frozen`); master-repo 73 passed (embedded + server); agent eval 14/14; `vixy demo --fresh` 9/9 on the server. The demo ran under a separate scratch user, so the existing `vixy_demo` graph was left untouched.
- **Preserved local state.**
  - The Docker container `vixy-falkordb-1` is still running on `127.0.0.1:6390`.
  - The volume `vixy_vixy_falkordb_data` and the `vixy_demo` graph are kept. `docker compose down -v` would delete them, so don't run it unless you mean to.
  - `.venv/` in master-repo is gitignored.
  - The unrelated Redis on port 6379 was not touched.

## 13. Actions that need your permission or someone else's access

1. **Revoke or rotate the exposed Baseten API key: manual, by the account owner.** It was committed in `master-repo/.env` in `aa020ad`. It stays readable in git history even though the file is now untracked, so treat it as compromised until it is rotated. This is also recorded in `master-repo/.env.example` and `master-repo/README.md` (Security).
2. **Decide whether to rewrite git history** to purge the key. That needs a team decision, a force-push and coordination. It has not been done.
3. **Push and open PRs** for each teammate's repo, when you want them reviewed. Not done.
4. Optionally, **report the FalkorDB 6.0.3 panic upstream**, or pin a different server image.

## 14. Definition of done

| # | Criterion | Status |
|---|---|---|
| 1 | Context Engine, Active Agent and Dreamer connected in an executable local workflow | ✅ `python -m vixy demo` |
| 2 | One unified FalkorDB graph per user | ✅ `vixy_<user_id>`, version and owner enforced |
| 3 | Graph combines Ara's semantic layer with Samu's episodic and operational layers | ✅ §5 |
| 4 | Audio and video pass through Context Engine before reaching the agent | ✅ tested (A, D) |
| 5 | Episode Writer persists Notes and Visits | ✅ tested (C); Note written before the agent runs |
| 6 | Dreamer reads the shared history and writes LongTermMemory into the shared graph | ✅ tested (E, F), including as a separate process on the server |
| 7 | Every demonstrated consolidated memory has real evidence | ✅ 6/6 in the demo; evidence-less beliefs are forced to `unconfirmed`; no inferred backfill |
| 8 | Agent and runtime retrieve the Dreamer's memories through a shared interface | ✅ `SharedMemory` and `AgentMemory.long_term_memories` |
| 9 | Idempotent and restart-safe | ✅ tested (H, I), with the in-memory caveats in §10.3 |
| 10 | Existing tests pass and new integration tests pass | ✅ §9 |
| 11 | Reproducible configuration without paid services | ✅ `uv.lock`, pinned Docker image, offline defaults, clean-copy check |
| 12 | Original repositories recoverable; no remote changes | ✅ original branches untouched, nothing pushed; M1 committed locally on `integration/m1-unified` (§12) |
