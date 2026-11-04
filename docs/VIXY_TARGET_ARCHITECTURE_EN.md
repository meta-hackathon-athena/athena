# Vixy — Target Architecture and Functional Contract

**Version:** 1.0 — Team decisions agreed on November 4, 2026  
**Reference document:** `VIXY_SYSTEM_AUDIT.md` (audit of `master-repo`, `context-engine`, and `dreamer-model`).  
**Purpose:** Define the system we intend to build, not describe the current state as though it were already integrated.

## 1. Product vision and demo

Vixy is an AI assistant that accompanies the user in the physical world: it observes scenes through a camera, processes conversations through audio, and uses location/time when available. It interprets what is happening, maintains recent context, creates persistent episodic and semantic memories, retrieves information from the past, and decides whether to intervene proactively or respond to a question.

**Meta Hackathon demo:** We will not depend on wearable hardware. We will inject prerecorded videos (with audio, timestamps, and optional GPS) through a player/simulator. The backend must process real information extracted from the video—or reproducible, preprocessed perception outputs—and genuinely run context tracking, the agent, memory, and retrieval. A responsive application in a mobile emulator will display activity, memories, queries, and notifications. Fake, hardcoded answers or notifications cannot substitute for working components. A simulated clock will allow hours or days to pass.

**Development:** We will implement the integration, perception/replay, intelligence, UI, and demo ourselves. We will reuse the three teammates' code, avoiding unnecessary rewrites. We prefer one coding agent working at a time, with substantial milestones and tests at the end of each milestone.

## 2. Logical diagram

```text
PRERECORDED VIDEO / AUDIO / GPS  (real hardware later)
                  |
                  v
PERCEPTION (later milestone)
  local detector -> classes, counts, and confidence
  ASR/VAD -> timed text; reliable speaker ID or unknown
  caption/VLM only when valuable (scene change / semantic need)
                  |
                  v
ADAPTERS / VERSIONED CONTRACTS
                  |
                  v
CONTEXT ENGINE — Aryan
  validated observed data + SQLite (canonical record)
  recent buffer, audio-video alignment, ContextSnapshot
             /                                      \
            v                                        v
VIXY RUNTIME                              DREAMER — Ara
  Episode Writer                          periodic consolidation
  audio -> Note                           deeper semantic interpretation
  scenes -> Visit/Episode                 pattern and memory extraction
  CE event -> Active Agent                evidence-grounded memory updates
  Agent receives recent context                    |
            |                                      |
            +----------------+---------------------+
                             |
                             v
               FALKORDB — ONE GRAPH PER USER
    episodes, notes, places, people, commitments,
     patterns, facts, and LongTermMemory with SUPPORTED_BY
                             |
                      shared queries
                             |
                             v
           ACTIVE AGENT — Samu + MEMORY API
             decide / recall / explain / notify
                             |
                             v
              MOBILE APP / EMULATOR (later)
```

This diagram represents the **target architecture**, not a claim that these connections already exist.

## 3. Non-negotiable responsibilities

### Context Engine — Aryan

- Canonical source of **processed observations**, not semantic inferences.
- Ingests typed, versioned `AudioSegment` and `VideoObservation` records.
- Preserves original events with stable IDs and UTC timestamps; deduplicates; stores history in SQLite.
- Aligns video and audio; provides `ContextSnapshot`, a context prompt, and event exports for Dreamer.
- Does not decide what to remember or generate `LongTermMemory`.

### Active Agent — Samu

- Processes **events already normalized by Context Engine**, using an adapter to its internal structures. The integrated runtime must not contain a second, parallel sensor-ingestion path.
- `ContextTracker` remains useful as a *semantic reducer* (visual stability, place, triggers), not as a replacement for Context Engine history.
- Uses `ContextSnapshot` for recent context and queries FalkorDB memory for relevant background.
- Applies intent rules, commitments, reminders, intervention policy, and anti-spam limits; may use an LLM for complex cases in later milestones.
- Decisions must include state, reason, priority, and evidence references where relevant. Notifications will reach the app in a later milestone.

### Dreamer — Ara

- **Ara's structure and consolidation logic are the foundation of semantic memory.** Preserve `Pattern`, `LongTermMemory`, the FalkorDB integration, and its Meta model/tool approach, while adapting them to the shared graph.
- Processes episodes and notes already stored by the runtime, plus relevant original observations retrieved from Context Engine via `event_id`.
- Extracts semantic information across multiple events, connects people/places/facts, and consolidates and updates memories. Evidence links must point to real supporting records, not fabricated records or links inferred solely from temporal proximity.
- Must not create a second observation log or an independent graph in production.
- Dreamer may run on a schedule later. For Milestone 1, it is enough to invoke it reproducibly and verify that it shares the graph and formats.

### Episode Writer — new integration component

- The **single canonical writer** that summarizes observations into `Note` (audio) and `Visit`/`Episode` (visual blocks) nodes in the graph.
- Receives events from Context Engine and preserves `source_event_id`, `session_id`, `user_id`, timestamps, and provenance.
- Persists `Note` **before** calling `ActiveAgent.handle(...)`, because the agent already relies on that sequence.
- Reuses Samu's segmentation and Ara's relevant adaptations without creating duplicate notes or episodes on retries.

## 4. Unified memory — final decision

**One FalkorDB graph per user**, for example `vixy_<user_id>`; never two isolated `veyra_*` and `vixy_dreamer` graphs in the final runtime. Runtime and Dreamer must share one FalkorDB server instance. `falkordblite` may be useful for isolated tests but not for sharing concurrent state across processes.

**Semantic foundation: Ara; episodic and operational capabilities: Samu.**

- **Episodic/operational:** `Day`, `Visit`/`Episode`, `Note`, `Place`, `Person`, `Commitment`, `Question`, `AgentLog`, and required attributes/tags. Reuse Samu's segmentation.
- **Semantic/consolidated:** `Pattern`, `LongTermMemory`, `Fact`, entity relations, and confidence/validity attribution where appropriate. Reuse Dreamer's consolidation.
- **Evidence:** Every consolidated memory has one or more `SUPPORTED_BY` links to real `Note`/`Visit` records (with references to Context Engine events). The system can explain when and why something is remembered. Inferred information must be distinguished from observed information. Avoid provenance links invented by heuristics.
- **Raw storage:** Do not automatically persist every frame as a graph node. High-frequency observations remain in SQLite; the graph stores episodes, entities, facts, memories, and references to relevant frames when necessary.
- **Identity:** For the demo, use one canonical `VIXY_USER_ID`, explicitly mapped to a consistent `session_id`. Document how users and sessions can be separated later without breaking event provenance.
- **Schemas:** Resolve incompatibilities (`Note.id` versus `Note.note_id`, types, relationships, graph names, fields, and migrations) through a documented contract and compatible queries—not merely by renaming strings without updating tests.
- **Writes:** Episode Writer creates summarized observations; Agent updates operational state; Dreamer creates/updates consolidated memories. Prevent conflicting writes to the same entities.
- **Idempotency:** Reinjecting the same event or rerunning consolidation must not generate duplicate nodes or contradictory memories.

## 5. Expected functional flow

1. The player/hardware produces video/audio/GPS. Perception generates detections and timestamped transcription segments.
2. Adapters convert external formats into valid Context Engine contracts; values such as `count`, speaker identity, and GPS must be preserved without inventing them.
3. Context Engine acknowledges and saves observations, deduplicates, aligns them, and builds recent context.
4. Runtime receives each new event. Episode Writer persists a `Note` or idempotently updates a `Visit` episode. The agent is then invoked.
5. Active Agent evaluates triggers, current context, and relevant memory and decides `NO_ACTION`, notification, question, or operational update.
6. Dreamer receives a bounded set of episodes/notes for the period, retrieves original observations when necessary, and consolidates `Pattern`/`LongTermMemory` in **the same graph**, always grounded in real evidence.
7. In subsequent milestones, the agent and question-answering API retrieve these memories to answer or intervene; the app presents them with their provenance.

**Milestone 1 integration-test example:** Ingest a synthetic day's observations, create `Note` and `Visit`, generate Agent decisions, and run deterministic consolidation that writes a `LongTermMemory` associated with a real `Note` and queryable from the same graph. Conversational LLM answering and the mobile interface are not required yet.

## 6. Models and cost

- Frequent perception: prefer a local/open-source detector and ASR when suitable; do not make 18 VLM calls to Baseten per frame.
- Complex interpretation, consolidation, and chat: use Meta models available to the account where valuable. Check actual availability before choosing them.
- Do not call paid models or deploy cloud resources without explicit authorization. Milestone 1 tests must use offline/fake implementations.
- Do not send real third-party recordings to `-contributor` mode or use configurations that allow unwanted training/retention; verify data handling and disable storage where appropriate.

## 7. Security and operations

- The `BASETEN_API_KEY` tracked in `master-repo/.env` must be revoked/rotated by someone with the necessary access. Stop tracking it in Git and add `.gitignore` and `.env.example`; history cleanup requires a separate decision. Do not print or copy secrets into reports.
- Keep the teammates' three repositories and their original branches intact. Work on local integration branches; do not `push`, remote-merge, or make destructive changes without permission.
- Maintain reproducible local tests, documented shared configuration, and safe/idempotent graph migrations and adjustments.
- Runtime must distinguish ingestion, memory, and agent failures, preserve provenance, and allow retries without duplicates.

## 8. Agreed roadmap

1. **Milestone 1 — Unified architecture and integration:** contracts/adapters, Context Engine → Episode Writer → Active Agent runtime, unified graph based on Ara + Samu, Dreamer able to read/write the common graph, integration tests, and security/hygiene.
2. **Milestone 2 — Intelligence, memory, and reasoning:** reliable semantic consolidation with Meta, contextual retrieval, entities, natural-language questions, and memory-assisted proactive decisions.
3. **Milestone 3 — Perception and simulator:** real video/audio, detector/ASR, replay, simulated clock, and reproducible data.
4. **Milestone 4 — Demo app:** responsive Next.js app; activity, memories, alerts, chat, explanations, and real-time APIs.
5. **Milestone 5 — Testing, demo, and presentation:** end-to-end validation, safe demo-data reset, real evidence of Meta model usage, recording, and submission.

## 9. Exact scope of Milestone 1

**Included:** documentation of contracts and the single schema, required adapters, working Context Engine + Episode Writer + Agent runtime integration, a shared local FalkorDB instance, Dreamer integrated with that graph (minimum offline path that creates/queries a memory with evidence), unit regression tests and one reproducible cross-component test, configuration/hygiene.

**Not yet included:** real vision/ASR, full video simulator, mobile app, natural-language memory chat, real push notifications, Meta credits usage, multiuser scaling, migration of production data.

**Minimum acceptance criteria:**

- Synthetic normalized audio/video goes through Context Engine and reaches Agent; Agent does not receive external inputs through a parallel runtime path.
- `Note` and `Visit` records are created in one `vixy_<user_id>` graph and are not duplicated on replay.
- IDs, timestamps, GPS, and speaker metadata are preserved when provided; if unknown, mark as `unknown`.
- Samu's implemented functions continue to pass their existing scenarios; all three repositories' test suites pass and Dreamer dependencies are declared.
- Dreamer reads the same history/episodes, writes at least one `LongTermMemory` in the shared graph with a `SUPPORTED_BY` edge pointing to real evidence, and runtime can retrieve it with a deterministic query. If fallback consolidation cannot justify a memory, it must not write it as fact.
- Repeating the test is idempotent. Restarting processes does not erase memory. Results are verified by an automated test, not just by a README.
- No paid-model calls, cloud deployments, remote pushes, or remote merges.

This document records an **agreed architectural decision**, not certification that Milestone 1 is already built. If an architectural decision is impossible under the existing code, the coding agent must document the conflict, propose the smallest viable adjustment, and verify its effect before departing from the decision.
