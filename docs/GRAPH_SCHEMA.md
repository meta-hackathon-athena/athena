# Vixy unified memory graph — `vixy.graph.v2`

> **Milestone 2 (`vixy.graph.v2`)** is an additive extension of `vixy.graph.v1`: structured
> claims on `LongTermMemory`, conservative `Person` resolution, belief relations
> (`SUPERSEDES`, `CONFLICTS_WITH`, `POSSIBLY_SAME_AS`), the `superseded` status, the
> Dreamer's `(:Extraction)` cache, and decision explanations on `AgentLog`. Nothing in v1 was
> renamed or removed. A v1 graph is refused until `python -m vixy migrate` upgrades it
> (indexes + version marker only; no node is rewritten). See "Milestone 2 additions" below.

One FalkorDB graph per user, named `vixy_<user_id>`. The Active Agent, the Episode Writer and
the Dreamer all read and write this one graph, on one shared FalkorDB server
(`docker compose up -d falkordb`).

The graph combines two existing designs:

- **Semantic layer (Ara's Dreamer)**: `Day`, `Pattern`, `LongTermMemory`, `SUPPORTED_BY` evidence, and the Dreamer's tools.
- **Episodic and operational layer (Samu's memory and agent)**: `Visit`, `Note`, `Place`, `Tag`, `Fact`, `Commitment`, `Question`, `AgentLog`.

Code:

- Versioning and indexes: `vixy/schema.py`.
- Episodic writes: `memory/store.py` and `vixy/episode_writer.py`.
- Operational writes: `agent/memory_tools.py`.
- Belief writes: `prod_dreamer_model/graph.py` (unified layout) and `prod_dreamer_model/consolidate.py`.
- Reads: `vixy/memory_api.py`.

Conventions (inherited from both designs):

- Times are integer epoch milliseconds (`*_ms`); temporal types can't be range-indexed. Observed nodes also carry UTC ISO strings (`timestamp`, `start_time`, `end_time`). `local_date` is in `VIXY_TZ`.
- There are no null properties. An optional property is simply absent, and an unknown speaker is the string `"unknown"`.
- Every write is a MERGE on a stable id, so replays and re-runs are idempotent.
- Every node has an `id` property, which is the common key used by `AgentLog-[:CONCERNS]` and `SharedMemory.evidence`.

## Layers and write ownership

| Layer | Labels | Who writes | Notes |
|---|---|---|---|
| observed | `Note`, `Visit`, `Day` | **Episode Writer** only | What the sensors captured. Always carries context-engine event ids. |
| entity | `Place`, `Tag` | Episode Writer (via `add_visit`) | `Place` needs GPS. `Tag` = detector class (one label; replaces the Dreamer's `Class`). |
| entity | `Person` | **Dreamer** | Identity = the name as heard, resolved conservatively in v2 (see below). |
| operational | `Commitment`, `Question`, `AgentLog` | **Active Agent** only (allow-listed ops) | The agent also sets `Note.status`/`done_ms` when a reminder completes. |
| inferred | `Fact` | `source=user` (preference): Episode Writer at ingestion; `source=inferred`: Dreamer job (`MemoryStore.consolidate`) | Structured, rule-derived attributes |
| inferred | `Pattern`, `LongTermMemory` | **Dreamer** only | The consolidated memories that retrieval and Q&A use |
| meta | `VixyMeta`, `DreamRun`, `Extraction` | runtime / Dreamer | Schema version and owner; consolidation audit; model-extraction cache |

The Dreamer, in the unified layout, refuses `reset()` and `load_bundle()` (which would create `Frame` nodes). It writes no label outside its own. The agent cannot run arbitrary Cypher: `AgentMemory.apply` allows only `record_commitment`, `update_commitment_time` and `complete_reminder`. `SharedMemory` is read-only and runs through `GRAPH.RO_QUERY`.

## Nodes

### `(:VixyMeta {key: 'graph'})`

`schema_version` (`'vixy.graph.v2'`), `user_id`, `created_ms`, and after a migration `migrated_from`, `migrated_ms`. It's created by `vixy.schema.bootstrap`. A graph with a different version or owner is refused (`SchemaMismatch`). There is no silent migration: `vixy.schema.migrate` (CLI `python -m vixy migrate`) upgrades `vixy.graph.v1` explicitly.

### `(:Day)`: Ara's concept, now shared

| property | |
|---|---|
| `day_id` | `'YYYY-MM-DD'` local date (unique per user graph) |
| `date` | same |
| `user_id`, `created_ms` | |

Linked as `(Note|Visit)-[:ON_DAY]->(Day)`. A Day is an aggregate, not evidence: unified-layout beliefs never cite a Day.

### `(:Note)`: one utterance (observed)

| property | |
|---|---|
| `id` | `note_<sha1(event_id)[:16]>`, the canonical graph key (Samu's `Note.id`) |
| `note_id` | the context-engine `event_id` (Ara's `Note.note_id` convention, kept so her tooling resolves) |
| `source_event_id` | the context-engine `event_id` (explicit provenance) |
| `session_id`, `user_id` | |
| `text` | the finalized transcript |
| `ts_ms` = `start_ms`, `end_ms`, `timestamp` (UTC ISO of start), `local_date`, `day_id` | |
| `kind` | `note` or `reminder` |
| `status?` | reminders only: `open` or `done` (set once; a replay never reopens a completed reminder) |
| `trigger_place_type?` | reminders only |
| `intent_hint?` | `reminder` or `preference` when someone other than the wearer used that phrasing |
| `speaker_id` | as given by ASR, or `'unknown'` |
| `speaker_role` | `wearer` or `other` from an explicit upstream flag, else `unknown`. Never inferred. |
| `confidence?`, `lat?`, `lon?` | |
| `payload_sha256`, `ingest_seq`, `event_schema` | provenance from context-engine |
| `source` (`'voice'`), `schema_version`, `layer` (`'observed'`) | |

`Note.id` vs `Note.note_id` (resolved): Samu's agent keys Notes by `id`; Ara keyed them by `note_id = event_id`. Both are kept, with fixed meanings: `id` is the graph key, and `note_id`/`source_event_id` is the CE event id. Agent queries (`open_reminders`, `complete_reminder`, `CONCERNS`) and Dreamer evidence resolution (`_link_evidence` accepts `Note.id` or `Note.note_id`) both work unchanged.

### `(:Visit)`: one stay or transit episode (observed; Samu's segmentation)

These are Samu's properties (`kind`, `start_ms`, `end_ms`, `local_date`, `dow`, `start_min`, `frames`, `place_type`, `activity`, `tags`, `sensitive`, `people_max?`, `lat?`, `lon?`; see `memory/schema.cypher`), plus integration provenance:

| property | |
|---|---|
| `id` | `vis_<sha1(user_id:first_event_id:start_ms)[:16]>` (deterministic) |
| `first_event_id`, `last_event_id` | context-engine video event ids of the first and last frame |
| `sample_event_ids` | up to 5 frame event ids spread over the visit |
| `session_id`, `user_id`, `start_time`, `end_time` (UTC ISO), `day_id`, `schema_version`, `layer` | |

Raw frames are **not** graph nodes; they stay in context-engine SQLite. A visit's complete frame list is the session's video events in `[start_ms, end_ms]`, which `ContextEngine.export_events` can read back. The Dreamer's former `(:Frame)-[:SAW]->(:Class)` (about 1.2M nodes per 14 days at 1 fps) is replaced by `(:Visit)-[:SAW]->(:Tag)`.

### `(:Place)`, `(:Tag)`, `(:Fact)`

These are unchanged from `memory/schema.cypher`. `Fact.kind` is one of `frequent_place`, `usual_activity` or `preference`. `Fact.source = 'user'` means stated by the wearer; `'inferred'` means computed from at least 3 days of visits. Preference facts are created only when `speaker_role = wearer`.

### `(:Commitment)`, `(:Question)`, `(:AgentLog)`: operational (Samu's agent)

These are unchanged (see `memory/schema.cypher` §4), plus:

- `AgentLog.trigger_event_id`: the context-engine event whose handling produced the decision.
- `Commitment-[:SUPPORTED_BY]->Note`: resolved by exact `Note.source_event_id = evidence_segment`. The old text-and-±2-minute match is now used only for legacy notes that have no `source_event_id`, such as simulator notes.

### `(:Pattern)`, `(:LongTermMemory)`: inferred (Ara's Dreamer)

These keep Ara's properties (`pattern_id`/`memory_id`, `statement`, `kind`, `confidence`, `status`, `time_window?`, `days?`, `created_ms`, `updated_ms`), plus:

| property | |
|---|---|
| `id` | same as `pattern_id` / `memory_id` (the common key) |
| `layer` | `'believed'` |
| `status` | `active`, `inactive`, or **`unconfirmed`** (no evidence edge: never presented as known) |
| `basis?` | `episodes`, `fact` or `reported_speech` |
| `subject?`, `reported_by?`, `support_count?`, `support_days?`, `place_type?`, `fact_kind?` | |
| `derived_by` | `vixy.offline_consolidation.v1` for offline consolidation |

Ids are derived from content, so a re-run updates the same node:

| kind | id |
|---|---|
| routine pattern | `routine-<place_type>` |
| routine memory | `ltm-routine-<place_type>` |
| fact memory | `ltm-<fact_id>` |
| person statement | `ltm-person-<name>-<sha1(normalized statement)[:10]>` |

### `(:Person)`

`id` = `per_<slug(name)>`, plus `name`, `source` (`'dreamer'`) and `created_ms` (v2 adds resolution properties; see below).

### `(:DreamRun)`

`id` = `dream_<sha1(rules version + input ids)>`, plus `rules_version`, `visits_read`, `notes_read`, `patterns_written`, `memories_written`, `notes_unverified`, `history_checked` and `finished_ms`. The same inputs give the same run node. v2 adds `extractor_version`, `extractor`, `new_notes`, `watermark_ms` (newest observation consolidated), `claims`, `conflicts`, `superseded`, `persons`, `ambiguous_persons`, `model_calls`, `model_claims`, `model_errors`.

## Milestone 2 additions (`vixy.graph.v2`)

### `(:LongTermMemory)`: structured claims (Dreamer, `vixy.semantic.v2`)

Claims extracted from verified utterances are LongTermMemory nodes with these extra
properties (all optional, so v1 memories stay valid):

| property | |
|---|---|
| `subject`, `subject_id` | display name / `Person.id`, or `'user'` for the wearer |
| `predicate` | `allergic_to`, `diet`, `avoids`, `likes`, `dislikes`, `relationship`, `lives_in`, `works_at`, `studies_at`, `has_event`, `states` (the Milestone 1 generic statement) |
| `object`, `object_text`, `polarity` | normalized value, value as said, `false` for "is not" |
| `category`, `caution` | e.g. `health`; `caution = true` when acting against it matters (allergies, diets, dislikes) |
| `claim_key`, `claim_value` | the slot (`subject_id|predicate[|object]`) and its value: one memory per (slot, value) |
| `epistemic_status` | `reported` (someone said it; unverified), `self_reported` (wearer about themself), `hypothesis` (hedged; confidence ≤ 0.45), `inferred` (from episodes) |
| `reported_by`, `speakers` | speaker roles (`wearer`/`other`/`unknown`) and diarization ids of the supporting notes |
| `support_count`, `first_stated_ms`, `last_stated_ms`, `valid_from_ms`, `valid_until_ms?` | reinforcement and valid time; `valid_until_ms` is set when superseded |
| `event_start_ms?`, `event_has_time?`, `time_text?` | `has_event`: when the plan is, resolved against when it was said |
| `contested` | `true` when another value for the same slot was stated without a change marker |
| `extracted_by` | list: `rules:vixy.semantic.v2` and/or `model:<model id>:vixy.extract.v1` (`fake:*` in tests) |
| `keywords` | stems used by retrieval; `derived_by` = `vixy.offline_consolidation.v2` |

Ids: `ltm-claim-<sha1(claim_key|claim_value)[:16]>`; the generic `states` claim keeps the
Milestone 1 id `ltm-person-<name>-<sha1(statement)[:10]>`. Routine and Fact memories also get
`epistemic_status` (`inferred` / `self_reported`), `category` and (Facts) `subject = 'user'`.

Statuses: `active`, `inactive` (retired: no longer produced from verified evidence; Milestone 1
generic memories that a structured claim now covers are retired this way, with a
`SUPERSEDES {basis: 're-extracted'}` edge from the new claim), `unconfirmed` (no evidence), and
**`superseded`** (a later statement of change replaced it; kept for history).

### `(:Person)`: conservative resolution

| property | |
|---|---|
| `id` | `per_<first>` for a bare first name (the Milestone 1 id), `per_<first>-<last>` for a full name |
| `name`, `first_name`, `last_name?`, `aliases`, `mention_count` | |
| `status` | `resolved`; `provisional` (bare name, one full-name candidate); `ambiguous` (several) |
| `resolution` | `name_as_heard.v2` |

A bare first name is never merged into a full name; it gets `POSSIBLY_SAME_AS` edges instead.

### `(:Extraction)`: model-extraction cache (Dreamer)

`id` (`ext_<sha1(extractor|note_id)>`), `extractor`, `note_id`, `claims` (validated JSON),
`claim_count`, `created_ms`; `(:Extraction)-[:FROM]->(:Note)`. Only written when a model call
for that note succeeded, so a note is never sent to the model twice and failures are retried.

### `(:AgentLog)`: decision explanations

`reasoning` (`rules:vixy.recall.v1`, `model:<id>`, or absent for Milestone 1 rules),
`supporting_memory_ids`, `supporting_json` (the supporting memories' statement, epistemic
status and evidence ids at decision time, as JSON text: FalkorDB stores no nested maps).
`CONCERNS` edges point at the memories and their evidence Notes.

## Relationships

```text
(Note)-[:DURING]->(Visit)                 utterance happened during that visit (time containment)
(Note|Visit)-[:ON_DAY]->(Day)
(Visit)-[:AT]->(Place)   (Visit)-[:SAW {ratio, conf, max_count}]->(Tag)   (Visit)-[:NEXT {gap_min}]->(Visit)
(Fact)-[:ABOUT]->(Place)                  (Fact)-[:SUPPORTED_BY]->(Visit|Note)
(Commitment)-[:SUPPORTED_BY]->(Note)      exact event; see above
(Question)-[:ABOUT]->(Commitment|Note)
(AgentLog)-[:CONCERNS]->(Commitment|Note|Question|Place|Fact|Visit|LongTermMemory|Pattern)
(Pattern|LongTermMemory)-[:SUPPORTED_BY]->(Visit|Note)      evidence: real records only
(Pattern|LongTermMemory)-[:ABOUT]->(Tag|Person)
(LongTermMemory)-[:DERIVED_FROM]->(Pattern|Fact)
(LongTermMemory)-[:SUPERSEDES {basis}]->(LongTermMemory)       v2: stated change (or re-extraction)
(LongTermMemory)-[:CONFLICTS_WITH {basis}]->(LongTermMemory)   v2: different value, no change stated
(Person)-[:POSSIBLY_SAME_AS {basis}]->(Person)                 v2: same first name, never merged
(Extraction)-[:FROM]->(Note)                                   v2: model output cache
```

`ABOUT` and `SUPPORTED_BY` previously had different targets in the two schemas. Now they have one meaning each: `ABOUT` points to the entity a belief concerns, and `SUPPORTED_BY` points to the observation that justifies it.

## Provenance chain (example from the demo)

```text
(:Person {name: 'Marta'})
     ^ ABOUT
(:LongTermMemory {statement: 'Marta has a presentation on Thursday at 9 AM.', basis: 'reported_speech'})
     | SUPPORTED_BY
     v
(:Note {id: 'note_…', speaker_role: 'other', source_event_id: 'asr:rec-2026-11-02:seg001', payload_sha256: '1dee…'})
     |  ContextEngine.get_event_record('asr:rec-2026-11-02:seg001')  -> same text, same sha256
     v
context-engine SQLite `events` row (canonical observation)
```

Rules for evidence:

- Evidence is always the ids of the exact records a belief was computed from.
- Nothing is linked because it falls inside a time window: the Dreamer's `backfill_evidence` is off by default (`DREAMER_ALLOW_INFERRED_EVIDENCE`).
- A belief left without evidence becomes `unconfirmed`.
- A Note is consolidated only after it has been verified against the original context-engine event.

## Indexes

They are listed in `vixy/schema.py` (`INDEXES`). The set is the union of `memory.store.INDEXES`, `agent.memory_tools.AGENT_INDEXES`, `prod_dreamer_model.graph.UNIFIED_INDEXES`, and `Note.source_event_id`, `Note.note_id`, `Note.local_date`, `Day.day_id` and `VixyMeta.key`. `tests/test_integration.py` checks that every indexed label is documented here.

## Conflicts resolved

| Conflict | Resolution |
|---|---|
| Graph names `veyra_<user>` vs `vixy_dreamer` | `vixy_<user_id>` for everything. `VIXY_GRAPH_PREFIX=veyra` opens old graphs. The Dreamer's standalone `vixy_dreamer` stays available as `graph_layout="legacy"`. |
| `Note.id` vs `Note.note_id`, incompatible Note properties | Both kept with fixed meanings (see Note); one writer (the Episode Writer) |
| `Class` vs `Tag` | `Tag` in the unified graph; the Dreamer's `about_label` follows the layout |
| `Frame` per frame vs `Visit` episodes | Visits in the graph; frames stay in SQLite, referenced by event id |
| `Fact` vs `LongTermMemory` | `Fact` = structured, rule-derived attribute. `LongTermMemory` = the consolidated memory surface: every active Fact is promoted (or merged into the routine memory for the same place type) with `DERIVED_FROM` and the Fact's evidence. A retired Fact retires its memory. |
| Timestamps (naive-as-local vs offset required) | Offset required everywhere; `memory.frames.parse_ts` now rejects naive strings |
| User/session identity | `VixyMeta.user_id`, and `session_id`/`user_id` on every observed node (see CONTRACTS.md) |
