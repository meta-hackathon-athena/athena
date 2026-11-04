# Vixy Active Agent

This is the part of Veyra that runs in real time. It takes camera frames and audio transcripts and decides, for each one, whether to **notify** the user, **ask** them something, make a **small memory update**, or (by default) **do nothing**. It reads and writes the same FalkorDB graph as `memory/`. It does not do perception, ingestion, or nightly consolidation; those belong to Perception, Ingestion, and the Dreamer.

```bash
uv run python -m agent.cli eval -v        # 14 synthetic scenarios, pass/fail + every decision
uv run python -m agent.cli sim            # ~9 h simulated day at 1 frame/s + metrics
uv run python -m agent.cli add "Dentist appointment" --start 2026-10-27T18:00 --end 2026-10-27T19:00
uv run python -m agent.cli run events.jsonl     # feed real vision/audio JSON (array or JSONL)
uv run python -m agent.cli commitments | questions | answer <question_id> yes
uv run pytest -q
```

## In the integrated runtime (Milestone 1)

`vixy/runtime.py` is the only producer of agent input. Every observation has already been
validated and stored by context-engine, and arrives through `vixy.adapters.agent_observation`:

- `segment_id` / `frame_id` are the context-engine `event_id`.
- `speaker_is_user` comes from the explicit `speaker_is_wearer` flag, or `None`.

The Episode Writer stores each utterance as a `Note` **before** `handle()` runs. That answers
open question 2 below. It also means commitments link `SUPPORTED_BY` to the exact
`Note{source_event_id}`, and reminders exist.

Other changes:

- Decisions and `AgentLog` carry `trigger_event_id`.
- `context_provider` gives the LLM path the recent context-engine snapshot.
- `AgentMemory.long_term_memories()` / `memory_evidence()` read the Dreamer's memories from the same graph `vixy_<user>`.
- The wake word is **"Vixy"**; "Veyra" is still accepted.
- `VIXY_TZ` / `VIXY_LLM_*` are read, with `VEYRA_*` as fallbacks.

Milestone 2 (memory-aware decisions, `agent/recall.py`):

- `ActiveAgent(recall=MemoryRecall(memory, reasoner=None))` adds memory recall after the existing
  audio rules. Without `recall` (the standalone eval and simulator) behaviour is exactly Milestone 1.
- Recall runs on utterances only, behind a cheap in-memory gate (a known person named, or the
  wearer's own caution memory touched), then bounded read-only retrieval (`AgentMemory.retrieve`).
- Deterministic rules decide: *caution* (buying / cooking / bringing / giving something that a
  remembered allergy, diet, avoidance or dislike rules out, with food families) and *plan clash*
  (a new plan with someone at a time they were said to be busy). Only uncertain candidates go to
  an optional reasoner model, which may pick NOTIFY or NO_ACTION for a candidate id and nothing else.
- Messages are rendered from the memory's fields and keep its attribution ("Someone mentioned
  yesterday that …", "I haven't verified it", "Someone else said otherwise", "if it's the same …").
- Decisions use the normal `NotificationGate` (dedup key `recall:<memory id>`, cooldown 12 h,
  rate limits). `Decision.supporting_memories` / `reasoning` are stored on `AgentLog`.

## How it fits

```
Perception ─> Ingestion ─> FalkorDB (Visit, Note, ...) ─> Dreamer (nightly)
                  │                    ▲   │
                  └─> Active Agent ────┘   └─ Question (source=dreamer) ─> Active Agent
                          │  reads small slices, writes only via an allow-list
                          └─> Decision ─> Notifier (delivery is not the agent's job)
```

The agent never feeds the Dreamer. Ingestion already stores what happened. The agent only adds its own `Commitment`, `Question`, and `AgentLog` nodes, and the Dreamer may read those. If the agent is off, nothing about the Dreamer's input changes.

## Pipeline

| Step | File | Cost |
|---|---|---|
| Parse and validate (optional/bad GPS, extra fields kept, bad timestamps rejected) | `models.py` | µs |
| **Frames → triggers**: dedupe frame ids, drop stale/out-of-order frames, drop detections < 0.5 confidence, sliding window of "stable" classes (≥60% of the last 8 frames), place type through `memory.rules.classify_place` with hysteresis (5 frames to settle). Emits `place_entered`, `location_changed` (moved > 60 m), `bus_stop_wait` (≥ 45 s at a stop, not on a bus), `boarded`, and a `tick` once a minute | `context.py` | no DB, no LLM |
| **Audio → interpretation**: dedupe by segment id *and* by content (same words, same moment). Rules first. The LLM is called only if the rules flag the utterance as uncertain (a plan-like sentence they can't classify) | `interpret.py`, `llm.py` | rules µs; LLM optional |
| **Time**: "tomorrow at six" → date + candidate starts `[18:00, 06:00]`, end unknown. Weekday, "next Friday", ranges, "for an hour", "in 20 min" and noon are supported. Ambiguity and assumptions stay as explicit flags | `temporal.py` | deterministic |
| **Decide**: per-trigger handlers query only what they need: one day's commitments, open reminders for one place type, the nearest place | `engine.py`, `memory_tools.py` | a few indexed queries |
| **Execute** in a fixed order: `MEMORY_UPDATE` → `NOTIFY` → `ASK_USER`. Each decision succeeds or fails on its own and records `status` (`done`, `suppressed`, `deferred` or `failed`); there is no false atomicity | `engine.py` | |
| **Gate**: dedup key, per-kind cooldown, hourly budget (low 3, normal 6, high 12), expiry, one open question at a time | `policy.py` | |
| **Audit**: an `AgentLog` node per non-trivial decision. It stores the reason and message, never the raw transcript | `memory_tools.py` | |

Most frames return `[]` and are never evaluated. An evaluation that finds nothing returns `NO_ACTION`.

## Decision output

```json
{"decision_id": "dec_…", "ts_ms": 1792…, "action": "NOTIFY", "priority": "high", "confidence": 0.9,
 "reason": "conflict with a stored commitment",
 "message": "Heads up: Meet Alex tomorrow at 6 PM overlaps Dentist appointment (6 PM–7 PM).",
 "related_memory_ids": ["cmt_51d5…", "cmt_6be1…"], "deduplication_key": "conflict:cmt_51d5…:cmt_6be1…",
 "proposed_memory_update": null, "question_id": null, "expires_ms": 1793…, "trigger": "audio", "status": "done"}
```

One evaluation can return several decisions. For example, a user's own plan that clashes with an existing one gives `MEMORY_UPDATE` (save it as tentative) plus `NOTIFY` (the conflict). Ids are deterministic (`cmt_<hash(title,start)>`, `qst_<hash(key)>`), so a retry after a crash re-runs safely. The dedup keys from the last 7 days are reloaded from `AgentLog` at startup.

## The rules that matter

- **Who said it.** A commitment is written only if `speaker_is_user` is `true`. Even then it is saved as `tentative` (or `confirmed` after a "yes", or when the user gives a "Vixy, add…" command). An unknown speaker's plan is ignored. The exception is when it would clash with something the user really has; then the agent asks once, and says it isn't sure who spoke.
- **Conflicts.** `overlap()` is deterministic and never invents a duration. If both intervals are known, the answer is `conflict` or none. If one end is unknown, it is still `conflict` when one starts inside the other, and `possible` when the starts are less than an hour apart. If an am/pm-ambiguous time conflicts under only one reading, that counts as `possible`.
- **Contradictions.** "My dentist is at 5 actually" when 6 is stored becomes a question, not an edit. On "yes", the time moves, `prev_start_ms`/`prev_end_ms` are kept, and the duration is preserved.
- **Questions.** Every question is stored as a `Question` node (`pending → asked → answered|expired`) with an expiry and the exact allow-listed write to run on "yes":
  - Low-priority questions wait 10 minutes so they don't interrupt the conversation.
  - Only plans in the next 48 h are worth interrupting for.
  - An unanswered question blocks new ones for 20 minutes, then stays answerable in the app.
  - Answers come from a spoken yes/no by the wearer within 5 minutes, or from `agent.answer(question_id, text)`. A text answer can also be a time, for "What time is X?".
  - The Dreamer can drop in `Question {source: 'dreamer', kind: 'dreamer', status: 'pending'}` nodes. The agent asks them when the user is free, and the answer is stored on the node.
- **Writes.** `AgentMemory.apply()` accepts only `record_commitment`, `update_commitment_time` and `complete_reminder`, with validated arguments, MERGE-based ids, and no deletes. The LLM never produces Cypher or chooses an operation; it can only label an utterance (`intent`, `strength`, `title`, `people`).
- **Prompt injection.** Transcripts are data. A command is honoured only if the wearer (`speaker_is_user=true`) says it, it starts with "Vixy" (or the legacy "Veyra"), it is on the allow-list, and it doesn't look like an injection. "Delete/forget/erase …" by voice is always refused politely, and the text sent to the LLM is wrapped as data.
- **Failure.** If FalkorDB is down, the result is `NO_ACTION` with `status: failed`. Nothing is guessed, and the work is queued for `retry_failed()`; dedup keys mean the retry never double-notifies. If the LLM times out or returns bad JSON, the rule result is used. If delivery fails, the key is not recorded, so delivery can be retried.
- **Places.** Reminders trigger on the camera's settled place type, or on a known nearby `Place` from GPS. Sensitive places are never named in messages.
- **Transit.** Arrival times come only from a provider. Only `MockTransitProvider` exists, and its output is labelled "(mock data)". Responses are validated, and the agent says nothing once you're on the bus.

## Measured (synthetic, not a benchmark)

These numbers come from `agent.cli sim` on a dev laptop with embedded FalkorDB (falkordblite). The simulated day is 30,420 frames and 11 utterances (8.9 h). Re-run to check:

| | value |
|---|---|
| frames never evaluated | 29,905 / 30,420 (the rest are once-a-minute ticks and 16 real triggers) |
| per-frame cost, no trigger | p50 0.009 ms |
| audio decision (rules + DB) | p95 ≈ 2 ms |
| DB queries | ≈ 0.02 per observation |
| LLM calls | 2 for the whole day (FakeLLM in the sim) |
| notifications + questions delivered | 7 (≈ 0.8/h) |

Targets for real hardware (not yet measured): frame filtering < 5 ms p95, rule-path audio decision < 100 ms p95 against a networked FalkorDB, LLM path < 2 s with a 4 s hard timeout (`llm.py`). `ActiveAgent.summary()` reports the same counters and latency percentiles live, so the targets can be checked on real data.

## Configuration

- `VIXY_TZ` (legacy `VEYRA_TZ`, default `Asia/Singapore`) is used to resolve "tomorrow".
- `VIXY_LLM_MODEL`, `VIXY_LLM_BASE_URL` (default Baseten's OpenAI-compatible URL) and `VIXY_LLM_API_KEY` (falls back to `BASETEN_API_KEY`) configure the LLM; the `VEYRA_LLM_*` names still work. The integrated runtime (`vixy/`) never builds an LLM from the environment by itself; pass an interpreter explicitly. With no model set, the agent runs on rules only. The live LLM path has not been tested against a real endpoint yet.
- Thresholds live in `AgentConfig`, `TrackerConfig` and `PolicyConfig`.

## Limits and open questions for the team

1. **Speaker identification.** Without `speaker_is_user`, the agent (correctly) saves almost nothing from speech. Can audio give us at least a wearer/not-wearer flag?
2. **Who stores transcripts.** The agent assumes Ingestion stores every transcript as a `Note` (it links commitments to them), and that "remind me …" reminders come from `MemoryStore.add_note`. Should Ingestion keep other people's speech at all?
3. **Where existing appointments come from**: a calendar sync or the app? Today it's `record_commitment(source='app')` / `agent.cli add`.
4. **The Dreamer contract.** It reads `AgentLog`, answered `Question`s and `tentative` commitments, and writes `Question {source:'dreamer'}` for the agent to ask. Does that work for the Dreamer's owner?
5. **The notification service.** It must implement `deliver(decision) -> bool`, and answers come back through `ActiveAgent.answer(question_id, answer)`.
6. **GPS.** Is it inside each frame (supported) or a separate stream?
7. **LLM.** We need a model and an endpoint on Baseten. Should the LLM also phrase messages? Today all messages are templates.
8. **Transit API.** Is a real provider approved (e.g. LTA DataMall)? Only the mock exists.

Other known limits:
- Voice answers are yes/no only.
- The rules are English-only, and the patterns are tuned on synthetic sentences.
- Tracker state is held in memory, one process per user.
- A plan more than 48 h away is not asked about (the note is still stored by Ingestion).


### M3 shared model configuration

The standalone `agent.cli run` interpreter uses `MODEL_API_KEY` from the same shared loader as Dreamer, with Meta's chat-completions endpoint. It remains disabled unless `VIXY_LLM_MODEL` (or the legacy model-name alias) is explicitly set. Contributor models are refused. The old Baseten credential fallback and environment endpoint/key overrides are removed from this factory; `OpenAICompatibleLLM` remains directly injectable for explicit custom providers and tests. Integrated MP4 replay uses the existing rules-only interpreter and M2 recall, so installing a credential cannot enable paid calls.
