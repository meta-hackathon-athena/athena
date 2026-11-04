# Vixy: Milestone 2 implementation report — intelligence, memory and reasoning

**Date:** 2026-11-04. **Builds on:** Milestone 1 (`VIXY_M1_IMPLEMENTATION_REPORT.md`), target design
`VIXY_TARGET_ARCHITECTURE_EN.md`.

**Status:** implemented and tested; every regression suite and demo passes (§9). The work is on local
`integration/m2-intelligence` branches, created from the M1 commits. It is **not committed**,
because no commit was authorized. Nothing was pushed, merged or billed, and no paid model was called.

## 1. Summary

Vixy now learns from what it hears and applies it on a later day:

```text
Mon  someone: "Alex is allergic to peanuts."  ─► Note (Context Engine event, sha256)
night  Dreamer ─► LongTermMemory {subject Alex, predicate allergic_to, object peanut,
                                  epistemic_status reported, reported_by [other]} ─ABOUT→ Person per_alex
                                  ─SUPPORTED_BY→ that Note
Tue  wearer: "I'm buying peanuts for Alex."  ─► Active Agent: gate (known person) → bounded retrieval
                                  → caution rule (buy × allergen) → NotificationGate
     NOTIFY "Someone mentioned yesterday that Alex is allergic to peanuts. I haven't verified it.
             You may want to check with Alex before buying."   (AgentLog: reasoning, supporting memory, evidence)
     "I'm buying chocolate for Alex too." → NO_ACTION (possibly relevant; rules can't tell; no model → silent)
     "I'm buying peanuts for Jordan."     → NO_ACTION (no one known)
     "I'm buying peanuts for Alex." again → suppressed (same memory, dedup + 12 h cooldown)
     "I'll meet Marta on Thursday at 9am" → saved as tentative + NOTIFY: Marta was said to present then
/ask "Why did you remind me about this?" → the decision, its memory, the original utterance, re-checked
```

Delivered:

- **Dreamer** (Ara):
  - structured semantic extraction (`semantic.py`);
  - conservative entity resolution (`entities.py`);
  - consolidation that reinforces repeated claims, supersedes stated changes, keeps conflicts explicit, and stays idempotent and incremental;
  - an optional, budgeted, validated and cached Meta-model extractor (`model_extract.py`).
- **Retrieval** (`vixy/retrieval.py`): explainable graph retrieval with bounded queries and provenance.
- **Memory-aware Active Agent** (`agent/recall.py`, Samu's agent extended, not replaced):
  - deterministic relevance rules;
  - an optional model reasoner, used only for uncertain cases, which can never write text or bypass the gate.
- **`/ask`** (`vixy/ask.py`):
  - question types, time references and people;
  - read-only queries;
  - sources re-checked against Context Engine;
  - an "insufficient evidence" answer when nothing supports one;
  - follow-ups that persist across restarts.
- **Versioned `/v1` HTTP API** (`vixy/api.py`, stdlib): ask, memory detail with provenance, people, timeline, decisions with supporting memories, event ingestion, and dream.
- **Graph schema `vixy.graph.v2`**: additive over v1, with an explicit `python -m vixy migrate`.
- **Multi-day demo** `python -m vixy demo-m2`: 18 self-checks, on the real runtime, embedded and server.

## 2. Repository and file changes

| Repo | Branch | Base (M1) | Change |
|---|---|---|---|
| `master-repo` | `integration/m2-intelligence` | `a399156` | 19 files modified, 7 added (+2.8k lines incl. tests and docs) |
| `dreamer-model` | `integration/m2-intelligence` | `68ce46e` | 6 modified, 4 added (+1.5k lines incl. tests) |
| `context-engine` | `integration/m1-unified` (unchanged) | `6854132` | no changes needed, so no M2 branch |

### dreamer-model (Ara)

| file | what |
|---|---|
| `semantic.py` (new) | `Claim`/`Mention`; extraction rules for allergies, diets, avoidances, likes/dislikes, relationships (only from the wearer's "my …"), lives/works/studies, someone's plans with resolved time; negation, hedges, change markers; first-person claims only from the wearer; M1 `person_statements` kept as the `states` fallback |
| `entities.py` (new) | Person resolution: `per_<first>` (M1 id), `per_<first>-<last>`; never merges a bare name into a full name; `provisional`/`ambiguous` + `POSSIBLY_SAME_AS` |
| `model_extract.py` (new) | `StructuredModel` interface; `ResponsesStructuredModel` (Meta Responses API, forced function call, `store:false`, contributor guard, timeout); `BudgetedModel` (call cap, schema validation, usage counters); `FakeStructuredModel`; `ModelExtractor` (every claim must cite a note in its batch and name a person and object present in its text) |
| `consolidate.py` | `_semantic`: verified notes → claims (rules + cached model) → one memory per (slot, value) → reinforcement, `SUPERSEDES` (stated change, `valid_until_ms`), `CONFLICTS_WITH` (both `contested`); retires memories no longer produced (incl. M1 generic ones a structured claim now covers, with `SUPERSEDES`); `DreamRun` watermark / `new_notes`; `epistemic_status` on routine and fact memories; rules `vixy.offline_consolidation.v2` |
| `graph.py` | v2 indexes; `superseded` status; `upsert_person`, `link_persons`, `relate`, `cached_extractions`, `store_extraction` |
| `dreamer.py` | unified layout always runs the deterministic consolidation (the Muse loop can only add); `extractor` parameter |
| `cli.py`, `tools.py`, `README.md` | `--model-extraction`; v2 schema brief for the Muse loop; docs |
| `tests/test_semantic.py` (new) | 12 tests (A–E, K) |

### master-repo (Samu + integration)

| file | what |
|---|---|
| `vixy/retrieval.py` (new) | `Cues`, `MemoryRetriever`: people → Person (+ candidates), memories by entity / keywords, patterns, commitments, reminders, notes in a time window; per-query LIMITs, final cap, named score reasons, excludes the triggering event |
| `agent/recall.py` (new) | `MemoryRecall`: cheap gate → retrieval → caution / plan-clash rules (with food families) → optional reasoner (per-hour budget, candidate ids only) → deterministic message with attribution and doubt |
| `agent/engine.py`, `models.py`, `memory_tools.py`, `policy.py` | `ActiveAgent(recall=…)` after the existing audio rules; `Decision.supporting_memories/reasoning`; `AgentMemory.retrieve/recall_index`; AgentLog `reasoning`, `supporting_memory_ids`, `supporting_json`; `recall` cooldown 12 h |
| `vixy/ask.py` (new) | `AskService`, `vixy.ask.v1` |
| `vixy/api.py` (new) | `VixyAPI` + `serve()` (`/v1`) |
| `vixy/runtime.py` | recall wired in; `ask`, `dream`, `clock_ms`; optional `reasoner` / `answer_model` |
| `vixy/memory_api.py` | `timeline`, `person`, `decisions`, `decision`; richer `explain`; nested-null cleaning |
| `vixy/schema.py`, `contracts.py` | `vixy.graph.v2`, `migrate()`, new indexes, ownership of `Extraction` |
| `vixy/dream_job.py`, `vixy/cli.py`, `vixy/demo_m2.py` (new) | extractor pass-through; `demo-m2 ask serve decisions decision timeline migrate`; Day 1 / Day 2 synthetic perception data |
| `docs/API.md` (new), `ARCHITECTURE.md`, `GRAPH_SCHEMA.md`, `CONTRACTS.md`, `README.md`, `agent/README.md`, `memory/schema.cypher` | documentation |
| `tests/test_m2.py` (new) | 15 test functions (21 with server variants) |

## 3. Final reasoning architecture

```text
 new observation ─► Context Engine (validate, SQLite) ─► Episode Writer (Note/Visit) ─► ActiveAgent.handle
   frames  ─► ContextTracker → place/tick triggers → M1 rules                       (no LLM, no retrieval)
   speech  ─► interpret (rules; LLM only if uncertain and configured) → M1 rules
           └► MemoryRecall: gate (known person / own caution word, in-memory index)
                  → MemoryRetriever (≤ 2 bounded RO queries + cap 8, with reasons)
                  → rules: caution | plan clash → NOTIFY;  possibly relevant → reasoner (optional) or NO_ACTION
                  → message from memory fields (attribution, "not verified", contested, ambiguous)
           ─► NotificationGate (dedup recall:<id>, cooldown, rate limits, expiry) ─► Notifier
           ─► AgentLog {reasoning, supporting_memory_ids, supporting_json} -CONCERNS→ memory, Note
 FalkorDB vixy_<user> (vixy.graph.v2) ◄── Dreamer job: routines · Facts · claims · entities · relations
 /v1 API ─► AskService: interpret → RO queries → re-check sources in Context Engine → grounded answer
```

Three levels, in order of cost:

1. **Deterministic rules.** Every frame and every utterance.
2. **Graph retrieval.** Only for utterances that name a known person (or touch the wearer's own caution memory), and for `/ask`.
3. **Model reasoning.** Only for uncertain recall candidates, opt-in Dreamer extraction, and opt-in `/ask` rephrasing. Never per frame.

On Day 2 of the demo, 5 of the 7 utterances reached retrieval and 2 were gated out without a query (agent `recall` stats in the demo output: `gated_out 2, retrievals 5, rule_hits 3`). There were 0 LLM calls.

## 4. Semantic extraction and consolidation

**Claims.**

- **Fields:** `subject`, `subject_id`, `predicate`, `object`, `polarity`, `event_start_ms`/`event_has_time` (for plans; resolved against when it was said), `time_text`, `hedged` and `change`.
- **Epistemic status:**
  - `reported`: someone said it; never verified;
  - `self_reported`: the wearer about themself;
  - `hypothesis`: hedged; confidence ≤ 0.45;
  - `inferred`: from episodes.
- **Also kept on every claim:** `reported_by` (speaker roles), `speakers` (diarization ids), `extracted_by` (`rules:vixy.semantic.v2` and/or `model:<id>:vixy.extract.v1`), and `SUPPORTED_BY` to the exact Notes.

**Provenance.**

- A Note is used only after its text and SHA-256 match the Context Engine event; otherwise it is counted as `notes_unverified`.
- A claim with "I" is attributed to the user only when the wearer said it.
- "my sister Marta" means the user's sister only when the wearer said it.
- No inferred (time-window) evidence: `inferred = 0` in every run.

**Consolidation.**

- **Reinforcement.** One memory per (slot, value). Repeating it adds evidence and `support_count`; confidence grows by 0.1 per extra note, capped at 0.9.
- **Change over time.** A different value in the same slot becomes:
  - `SUPERSEDES`, if the newer statement signals a change ("moved to", "now", "anymore", "rescheduled"). The old memory becomes `superseded` with `valid_until_ms` and is kept as history.
  - `CONFLICTS_WITH`, if no change was stated. Both memories stay active, both are marked `contested`, and every answer or notification says so.
- **Repeated runs.** Recomputed from all verified notes on every run with content-derived ids, so runs are idempotent. `DreamRun.watermark_ms` and `new_notes` make incremental runs observable, and model output is cached per note.
- **Milestone 1 memories.** M1 generic statement memories keep their ids. Where a structured claim now covers the same note, the M1 memory is retired (`inactive`) with a `SUPERSEDES {basis: re-extracted}` edge; nothing is deleted.

**Entities.**

- All bare "Marta" mentions resolve to `per_marta`.
- "Marta Lopez" and "Marta Chen" are separate people. The bare "Marta" becomes `ambiguous`, with `POSSIBLY_SAME_AS` edges to both, and is never merged.
- Retrieval, `/ask` ("I know more than one person called Marta …") and recall ("if it's the same Marta", confidence × 0.8) all expose this.

## 5. Retrieval methods

Graph retrieval with structured matching; no embeddings were needed. The tests and demo questions are all answered by structure plus keywords.

- **Entity match:** `ABOUT → Person`, including `POSSIBLY_SAME_AS` candidates.
- **Keywords:** stemmed against `LongTermMemory.keywords` and the statement text.
- **Other record types:** patterns by place type; commitments by people or title; open reminders; recent Notes in a time window.
- **Filters:** status (`active` by default; `/ask` also includes `superseded` as history) and minimum confidence. The utterance being handled is never recalled from itself.
- **Scoring:** entity +3, named +2, keyword +1 each (up to 3), caution +0.5, confidence, upcoming plan +0.5, past plan −1, recency, contested −0.3, hypothesis −0.3. The named reasons are returned with each item.
- **Bounds:** every query has a LIMIT, the final list is capped, and empty cues return nothing (never "everything"). All queries are fixed, parameterized `GRAPH.RO_QUERY` Cypher.

## 6. Active Agent decision flow

New observation → Context Engine → existing triggers and rules (unchanged) → `MemoryRecall` for speech. The steps, each with a test:

| Step | Rule or behaviour | Test |
|---|---|---|
| Gate | No known person and no own-caution word: return without querying | Jordan case |
| Retrieve | ≤ 8 LongTermMemory candidates for the named people | — |
| Caution | Positive caution memory (allergic_to / diet / avoids / dislikes) + an action verb (buy, cook, bring, give, order, …) + an object match, literal or by family (vegetarian ⇒ meat and fish; nut ⇒ peanut, almond, …): NOTIFY, priority normal | — |
| Plan clash | A new plan (agent commitment) with that person within 90 min of a remembered `has_event`: NOTIFY | — |
| Possibly relevant | e.g. a caution memory and an action verb but no object match: the reasoner if configured, else NO_ACTION "stayed silent" | — |
| Reasoner constraints | Gets the utterance and the candidates as data. Returns `notify`/`no_action` and a candidate id; anything else is ignored. Its notifications are LOW priority, confidence ≤ 0.6, rendered from the memory | Fake model asking for "CALL 911" and `op: delete_all`: neither reaches anything |
| Gate always applies | Dedup `recall:<memory id>` (12 h), per-priority rate limits, expiry; the history is restored from AgentLog after a restart | Model-chosen recall still `suppressed` |
| Uncertainty kept | "Someone mentioned …", "I haven't verified it", "Someone else said otherwise" (contested, confidence × 0.7), "if it's the same …" | — |
| Failure | If memory is unavailable, recall is skipped (no guess); a reasoner failure gives NO_ACTION | — |
| Not affected | M1 behaviours (reminders, conflicts, questions) are unchanged. The standalone agent (eval, simulator) runs without recall: 14/14, and the simulator numbers are identical | — |

## 7. `/ask` API contracts

Full reference: `master-repo/docs/API.md`.

| Route | Purpose |
|---|---|
| `POST /v1/ask {question, conversation_id?, now?}` | Answers a question; returns `vixy.ask.v1`: `status` (`answered`, `insufficient_evidence`, `ambiguous`, `unsupported`), `answer`, `interpretation` (type, people, topic keywords, time range, follow-up), `items` (with `epistemic_status`), `sources` (`verified` = re-checked against Context Engine), `answered_by`, `model_usage` |
| `GET /v1/memories`, `/v1/memories/{id}` | Memory lookup and detail, with derivation, relations and evidence, each with its original CE record and `matches` |
| `GET /v1/people/{id}` | One person, with possible duplicates and memories |
| `GET /v1/timeline?date=` or `?from=&to=` | Visits and Notes in a range |
| `GET /v1/decisions`, `/v1/decisions/{id}` | Agent decisions, with supporting memories and evidence |
| `POST /v1/events/audio`, `/v1/events/video` | Event ingestion for the simulator |
| `POST /v1/dream` | Runs the Dreamer |
| `GET /v1/health`, `/v1/status` | Liveness and runtime status |

Supported questions (rules):

- "what did I do / eat / where did I go <when>";
- "when am I meeting X";
- "what did X tell me about Y" (always states that it cannot confirm who spoke: diarization ids are not names);
- "what do you remember about X";
- "why did you remind me about this";
- "what evidence supports that memory";
- keyword search.

Time references: today, yesterday, N days ago, last/this week, weekdays, ISO dates and "2 Nov". Follow-ups use a per-conversation context in the runtime ledger, so they survive a restart.

## 8. Meta model configuration

- **Model IDs** (from OpenClaw's provider page, docs.openclaw.ai/providers/meta: a secondhand source, because no `MODEL_API_KEY` exists in this workspace):
  - `muse-spark-1.3` (the default, and our default);
  - `muse-spark-1.3-contributor`, `muse-spark-1.2`, `muse-spark-1.2-contributor`, `muse-spark-1.1`;
  - `muse-wonder-1.0`.
- **API:** `https://api.meta.ai/v1/responses`, with tool calling, reasoning effort `minimal`–`xhigh`, and `store:false` supported.
- **Structured output:** JSON-schema response formats are **not documented**, so structured output is one forced function call, validated locally either way.
- **Not verified live:** whether the API accepts `tool_choice: {"type":"function","name":…}`. If it doesn't, the call fails cleanly (`ModelError`) and the deterministic path stands.
- **Configuration:** `MODEL_API_KEY`; `DREAMER_EXTRACT_*` and `VIXY_MODEL_*` (`_ID`, `_MAX_CALLS` = 20, `_TIMEOUT_S` = 30, `_MAX_OUTPUT_TOKENS` = 1200, `_REASONING` = low). The agent reasoner is also capped at 6 calls per hour.
- **Defaults:** contributor models are refused unless `DREAMER_ALLOW_CONTRIBUTOR_MODEL=1`. The defaults make **no** model calls; models are opt-in (`--model-extraction`, `VixyRuntime(reasoner=…, answer_model=…)`).
- **Usage reporting:** calls, failures, budget refusals, tokens and by-purpose counts appear in dream results, `/ask` `model_usage` and `/v1/status`.

**Measured live model usage: none.** No live call was authorized or possible (no key). All model paths were tested with `FakeStructuredModel` and a stubbed HTTP transport.

## 9. Tests executed and exact results

All runs had `BASETEN_API_KEY`, `MODEL_API_KEY`, `VEYRA_LLM_MODEL`, `VIXY_LLM_MODEL` and `FALKORDB_HOST` blanked.

| Suite | M1 baseline (re-run today) | After M2 |
|---|---|---|
| context-engine `pytest` (`uv run --isolated --extra api --extra dev`) | 53 passed | **53 passed** (unchanged repo) |
| dreamer-model `uv run --frozen pytest` | 50 passed | **62 passed** (50 + 12 new) |
| master-repo `pytest`, embedded + server (`VIXY_TEST_FALKORDB_URL=redis://localhost:6390`) | 73 passed | **94 passed** (73 + 21 new) |
| master-repo `pytest`, embedded only | — | 79 passed, 15 skipped (server variants) |
| `python -m agent.cli eval` | 14/14 | **14/14** |
| `python -m agent.cli sim` | 7 delivered / 30,420 frames / 2 LLM calls (FakeLLM) | identical |
| Dreamer legacy `all --offline --days 14`, `context_engine.demo`, `memory.cli demo` | exit 0, 11 memories | exit 0, 11 memories |
| `python -m vixy demo --fresh` (M1), embedded and server (scratch user) | 9/9 | **9/9** |
| `python -m vixy demo-m2 --fresh`, embedded and server (scratch user) | — | **18/18** |

Coverage of the requested M2 checks:

| Check | Where |
|---|---|
| A. Extraction from several observations and transcripts | `test_rules_extract_structured_claims…`, `test_dreamer_turns_day1_conversations…` |
| B. Entity resolution and ambiguity | `test_same_first_name_links…never_merged`, `test_ambiguous_names_are_flagged_not_merged` |
| C. Duplicates and contradictions | `test_repeated_statements_reinforce…`, `test_contradiction…contested`, `test_stated_change_supersedes…`, `test_contested_memories_keep_their_doubt…` |
| D. Evidence and original events | memory → Note → CE record (same text and sha256) in Dreamer, agent, `/ask` and API tests |
| E. Incremental and idempotent consolidation | `test_incremental_runs_are_idempotent…`, M1 retirement test, demo check |
| F, G. Retrieval from previous days; memory-dependent decisions | `test_agent_uses_a_previous_day_memory…`, `test_retrieval_is_bounded…` |
| H. Correct NO_ACTION | chocolate (possible, silent), Jordan (unknown), Lena (irrelevant) |
| I, J. Grounded `/ask` and refusal to invent | `test_ask_answers_grounded…` (graph unchanged afterwards), `test_ask_says_when_there_is_no_evidence` |
| K. API and model failures | timeouts, HTTP 500, non-JSON, malformed, schema-invalid, budget exhausted, ungrounded rephrase, HTTP 400/404 |
| L. No bypass of limits or policy | `test_model_suggestions_cannot_bypass…`, dedup suppression |
| M. Persistence and restart | `test_memories_conversations_and_dedup_survive_a_restart` (both backends) |
| N. M1 compatibility | full M1 suites; `test_milestone_demos_run_end_to_end` (M1 and M2 demos as subprocesses) |
| Schema migration | `test_v1_graph_is_refused_until_explicitly_migrated` |

## 10. Limitations

1. **Extraction rules are English and pattern-based.**
   - They cover common phrasings of allergies, diets, preferences, relationships, places and plans. Anything else about a named person is kept as a generic `states` statement.
   - Pronouns ("she's allergic") are not resolved.
   - The model extractor covers the gaps, but it has not been run live.
2. **Entity identity is the name as heard.**
   - There is no speaker-to-name mapping, no face or voice identity, and no user-confirmed merge.
   - Diarization ids (`spk_1`) are not linked to people, so "what did Marta tell me" can never be confirmed.
3. **Conflict handling is per slot** and compares only consecutive values. It does not reason about which source is more reliable, beyond confidence by speaker role and support.
4. **Recall rules are a deliberately small set** (caution, plan clash). Other cases (forgotten commitments, preferences as suggestions) reach the reasoner only as "possibly relevant", and offline they stay silent.
5. **The Dreamer recomputes from all notes** on each run. That is fine at hackathon scale; a watermark-only pass is the next step for long histories.
6. **The Meta API's forced `tool_choice` and token usage fields are unverified live.** Budgets count tokens only if the API returns `usage`.
7. **The API is development-grade.** No authentication, one runtime per process, requests serialized by a lock, stdlib HTTP server.
8. **A Visit is visible only after it closes** (unchanged from M1). The Day 2 supermarket visit, for example, enters the graph when the next gap starts.
9. **A Visit's `verified` flag** means only that its first frame event exists in Context Engine; there is no text hash to compare.
10. **Existing M1 graphs (including `vixy_demo`) are refused by the M2 runtime** until `python -m vixy migrate`, which I did not run on them.

## 11. Commands to run the M2 demo

```bash
cd master-repo
uv sync
docker compose up -d falkordb                         # optional: shared server on :6390
export FALKORDB_URL=redis://localhost:6390            # optional; omit for the embedded file
uv run python -m vixy demo-m2 --fresh                 # user demo_m2, graph vixy_demo_m2; 18 checks; exit 0
uv run python -m vixy --user demo_m2 --state-dir data/demo_m2 ask "Why did you remind me about this?" \
    --conversation c1 --now 2026-11-03T21:00:00+08:00
uv run python -m vixy --user demo_m2 --state-dir data/demo_m2 serve --port 8765
curl -s localhost:8765/v1/ask -d '{"question":"What do you remember about Alex?","now":"2026-11-03T21:00:00+08:00"}'
curl -s localhost:8765/v1/decisions?delivered=1
uv run python -m vixy --user demo_m2 --state-dir data/demo_m2 decisions --limit 10
# Dreamer as its own process with opt-in (paid) model extraction:
cd ../dreamer-model && MODEL_API_KEY=… FALKORDB_URL=redis://localhost:6390 uv run python -m prod_dreamer_model.cli \
    --layout unified --user demo_m2 dream --offline --model-extraction --history-db ../master-repo/data/demo_m2/vixy_events.sqlite3
```

## 12. Remaining work for Milestone 3

- Real perception:
  - a local detector and ASR with VAD and diarization;
  - a speaker-identity contract (`speaker_is_wearer` and ideally named speakers);
  - Context Engine v1.1 with first-class `count` / `speaker_is_wearer`.
- A video and audio replay simulator driving `POST /v1/events/*` with a simulated clock.
- A live, authorized Meta model run to:
  - verify forced function calling and usage fields;
  - measure extraction quality against the rules, cost and latency.
- Pronoun resolution, and speaker-to-person linking once diarization is real.
- User-confirmed entity merges and corrections (an allow-listed operation, possibly a Dreamer `Question`).
- Incremental (watermark-only) consolidation and a scheduled Dreamer.
- More recall rule families (forgotten commitments, gift and preference suggestions), each with silent-case tests.

## 13. Git branch and working-tree state

| Repo | Current branch | HEAD | Original branches (untouched) | Working tree |
|---|---|---|---|---|
| master-repo | `integration/m2-intelligence` (local, from `a399156`) | `a399156` | `integration/m1-unified` @ `a399156`, `feature/active-agent`, `main` | M2 changes uncommitted: 19 modified, 7 untracked files |
| dreamer-model | `integration/m2-intelligence` (local, from `68ce46e`) | `68ce46e` | `integration/m1-unified` @ `68ce46e`, `main` | M2 changes uncommitted: 6 modified, 4 untracked files |
| context-engine | `integration/m1-unified` | `6854132` | `main` | clean (no M2 changes) |

- Nothing was pushed, merged, rebased, reset or force-pushed, and no history was rewritten.
- The only index operations were an accidental `git add -N` (intent-to-add, used to count lines), undone with `git restore --staged`. The index is as it was.
- Data preserved:
  - the `vixy_demo` graph (still `vixy.graph.v1`, 91 nodes, never opened by the M2 runtime);
  - the `vixy_vixy_falkordb_data` volume;
  - container `vixy-falkordb-1`;
  - the unrelated Redis on 6379.
- Tests and demos used unique scratch users, and their graphs were deleted afterwards. `docker compose down -v` was never run.
- `master-repo/demo_output/` (two files written by the Context Engine demo regression run) was removed; it was never tracked.
- The compromised Baseten key was not read, printed or used. It still needs rotation by its owner (see the M1 report).
- To keep this work: commit on the two `integration/m2-intelligence` branches (not done, because it wasn't authorized).

## 14. Definition of done

| # | Criterion | Status |
|---|---|---|
| 1 | Dreamer extracts meaningful structured semantic information | ✅ §4, tests A |
| 2 | Consolidated memories have genuine evidence | ✅ verified Notes only; no inferred evidence |
| 3 | Entities resolved conservatively | ✅ never merged; ambiguity exposed |
| 4 | Conflicts handled explicitly | ✅ `SUPERSEDES` / `CONFLICTS_WITH` / `contested` |
| 5 | Repeated consolidation is idempotent | ✅ tests E, demo |
| 6 | Agent retrieves relevant Dreamer memories | ✅ `MemoryRecall` → `MemoryRetriever` |
| 7 | At least one genuine memory-dependent decision | ✅ allergy caution; plan clash |
| 8 | Existing deterministic behaviours intact | ✅ eval 14/14, simulator identical, M1 tests |
| 9 | `/ask` answers from the unified graph | ✅ |
| 10 | Answers and notifications expose evidence | ✅ sources re-checked; AgentLog supporting memories |
| 11 | Missing information gives no fabricated answer | ✅ `insufficient_evidence` tests |
| 12 | Multi-day scenario runs | ✅ `demo-m2` 18/18, embedded and server |
| 13 | All M1 regression tests pass | ✅ §9 |
| 14 | New M2 tests pass | ✅ 12 Dreamer + 21 master-repo |
| 15 | Works locally without paid calls | ✅ no key needed; 0 model calls by default |
| 16 | History and user data preserved | ✅ §13 (work left uncommitted pending authorization) |
