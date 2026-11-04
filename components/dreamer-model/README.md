# prod_dreamer_model

The production **Vixy dreamer**: it studies a wearable user's day and turns raw
observation into durable, evidence-linked beliefs about them, stored in
FalkorDB.

This package fuses two earlier pieces:

- **context-engine** — a separate package/repo: the ingestion and short-term
  context layer. It owns the typed event contracts (`vixy.audio_segment.v1`,
  `vixy.video_observation.v1`), durable SQLite history, audio↔video alignment,
  and the chronological `vixy.dreamer_event.v1` export this package consumes.
  It is **optional here**: only `seed` needs it, because the dreamer reads the
  graph, not the raw sensors.
- **a prototype dream loop** — a Muse (Meta Responses API) agent that wrote
  Cypher against FalkorDB and stored `:LongTermMemory` nodes. That idea is
  productionised here: read-only Cypher plus validated belief writers, multiple
  focus rounds, mid-round budget nudges, and a deterministic fallback.

The graph schema is deliberately small and self-contained.

## Pipeline

```
 mock wearable frames + spoken notes          (mock_data.py)
        │
        ▼
 context-engine  ── validate, dedupe, persist in SQLite, align   (bridge.py)
        │            export vixy.dreamer_event.v1 (half-open day range)
        ▼
 FalkorDB graph  ── :Day / :Frame / :Class / :Note   (observed)   (graph.py)
        │
        ▼
 Muse dream loop ── 4 focus rounds, read-only Cypher +            (dreamer.py)
        │            validated writes, graph state accumulates
        ▼
 :Pattern and :LongTermMemory  ── with SUPPORTED_BY / ABOUT /     (report.py)
                                   DERIVED_FROM evidence links
```

The dreamer never reads raw frames or transcripts straight off disk. It sees
what context-engine committed; sending the same frames twice is a no-op.

## Install

> **Monorepo:** this package lives at `components/dreamer-model/` in
> [meta-hackathon-athena/vixy](../../README.md). From the repository root, `uv sync` installs it together
> with Context Engine and the runtime (one uv workspace, one `uv.lock`), and `uv run pytest -q
> components/dreamer-model` runs its tests. The commands below still work from this directory.

This is a standalone package. `falkordblite` (embedded FalkorDB, Python >= 3.12)
is a declared dependency. `context-engine` is an optional extra (`[context-engine]`)
because it is not on PyPI. It is needed to ingest perception data (`seed`), to load a
context-engine JSONL export (`load`), and to verify notes against the shared event
history in the unified layout. In the monorepo it resolves to the workspace member
`components/context-engine` (`[tool.uv.sources]`), and the root `uv.lock` pins everything.

```bash
uv sync                       # from the repository root: the whole workspace (.venv at the root)
cp .env.example .env          # repository root; FALKORDB_URL (and MODEL_API_KEY only if dreaming online)
```

Outside uv, install context-engine yourself, or point at a checkout:

```bash
uv pip install -e ../context-engine        # components/context-engine
export CONTEXT_ENGINE_SRC=/path/to/context-engine/src
```

The search order is: an installed `vixy-context-engine`, then
`$CONTEXT_ENGINE_SRC`, then a sibling `context-engine/src`. If none is found,
only `seed` fails, with a message saying exactly what to install.

No FalkorDB? Omit `FALKORDB_URL` and it uses an embedded `falkordblite` file
(`data/prod_dreamer.rdb`) — no server needed. The same code runs either way.

## Quickstart

```bash
# everything: reset, generate 14 mock days, ingest, load, dream, report
uv run python -m prod_dreamer_model.cli all

# or step by step
uv run python -m prod_dreamer_model.cli seed --days 14
uv run python -m prod_dreamer_model.cli dream --rounds 4
uv run python -m prod_dreamer_model.cli report
```

The installed console script is equivalent:

```bash
uv run vixy-dreamer all
```

`dream` calls Muse and needs `MODEL_API_KEY` (and usually `FALKORDB_URL`).
Without a model, or when the model stores nothing, the deterministic rules in
`heuristic.py` run so the pipeline always ends with a result:

```bash
uv run python -m prod_dreamer_model.cli dream --offline   # no network at all
```

If `uv run` is unavailable, run the module straight from the source tree:

```bash
PYTHONPATH=src python -m prod_dreamer_model.cli report
```

Outputs land in `dream_output/`:

| file | contents |
|---|---|
| `dream_readable.txt` | **start here** — what was provided, then what was concluded |
| `dream_report.md` | the same conclusions as markdown |
| `dream_output.json` | everything: coverage, patterns, memories, evidence |
| `dreamer_export.jsonl` | the context-engine export the dreamer consumed (gitignored, ~25 MB) |

## What "multi-step dreaming" means here

One `dream()` call runs up to four **focus rounds**, each a fresh Muse
conversation that can make as many tool calls as it likes (default 8 turns):

1. **Temporal routines** — when does the user do things (`local_hour`,
   `local_min`, `dow`)?
2. **Places and movement** — where do they go, how do they move between places?
3. **Activities, objects and what was said** — co-occurring classes and notes;
   stated preference outranks inference.
4. **Consolidate and remember** — merge duplicates, retract weak patterns,
   promote stable ones into `:LongTermMemory`, produce the final list.

The graph is the memory between rounds, so later rounds refine earlier ones
rather than repeating them.

## Tools the model is given

| tool | kind | notes |
|---|---|---|
| `run_cypher` | read-only | Any openCypher `MATCH`/`WITH`/`UNWIND`/`RETURN`/read `CALL`. Writes are rejected by `safe_cypher` (comments and string literals are scrubbed first, so `CONTAINS 'delete me'` is fine). Rows are capped. |
| `write_pattern` | write | Upserts a `:Pattern` (a recurring behaviour) and links evidence. Idempotent on `pattern_id`. |
| `write_memory` | write | Upserts a `:LongTermMemory` (a durable fact Vixy should remember), optionally linked to the patterns it was distilled from. |
| `finish` | control | Ends the round and records the round summary. |

Writes are validated in `tools.py`: empty statements are refused, kinds are
constrained, confidence is clamped to `[0,1]`, ids are slugified, and evidence
ids are resolved against exactly one label (`day_id` links the `:Day`, not every
frame of that day).

After the rounds, two hygiene steps make sure nothing is unsupported:

- `propagate_evidence` — a `:LongTermMemory` inherits the evidence of every
  `:Pattern` it was `DERIVED_FROM`.
- `backfill_evidence` — a pattern the model left without provenance is linked to
  frames matching its own `time_window`, `days` and `about` claims. Because that is
  *inferred*, not recorded, provenance, it is **off by default**
  (`DREAMER_ALLOW_INFERRED_EVIDENCE=1` turns it on).
- `enforce_evidence` — any belief still without a `SUPPORTED_BY` edge is set to
  `status: "unconfirmed"` and never presented as known.

These steps run after every dream, including the deterministic fallback. Previously the
fallback skipped them because its writes didn't reach the tool counters, which left 7 of
11 offline memories without evidence.

## Unified Vixy graph (`--layout unified`)

In the integrated system, the dreamer consolidates the shared per-user graph `vixy_<user_id>`
written by the master-repo runtime. Its schema is `vixy.graph.v1`; see master-repo
`docs/GRAPH_SCHEMA.md`. In this layout:

- **Reads.** Observations are `(:Visit)` episodes and `(:Note)` utterances, written by the
  runtime's Episode Writer. The dreamer never loads `(:Frame)` nodes; raw frames stay in
  context-engine SQLite.
- **Writes.** Beliefs keep this package's model: `Pattern`, `LongTermMemory`, `SUPPORTED_BY`
  and `DERIVED_FROM`. `ABOUT` points at `(:Tag)` (the detector class) or `(:Person)`. Evidence
  must be a real `Visit.id`, `Note.id` or `Note.note_id` (= the context-engine event id).
- **History.** It is read from the *shared* context-engine store (`--history-db`, an
  in-process engine, or `--history-jsonl`), never from a private copy.
- **Consolidation (every dream).** `consolidate.unified_dream` derives:
  - routines (the same place type on 3 or more days) → `Pattern` + `LongTermMemory`;
  - structured `Fact`s → `LongTermMemory`;
  - **semantic claims** (Milestone 2, `semantic.py`): each verified utterance is parsed into
    `subject / predicate / object / polarity / valid time` claims (allergies, diets, likes and
    dislikes, relationships, where someone lives / works / studies, someone's plans with a
    resolved time, and the Milestone 1 generic statement as fallback). Claims about the same
    thing become **one** `LongTermMemory` (reinforced, with all their evidence). A different
    value for the same slot becomes `SUPERSEDES` when a change was stated ("moved to", "now",
    "anymore": the old memory is kept as `superseded` with `valid_until_ms`) or `CONFLICTS_WITH`
    when it wasn't (both kept, both `contested`).
  - `epistemic_status` on every memory: `reported` (someone said it; never treated as
    verified), `self_reported` (the wearer about themself), `hypothesis` (hedged, confidence
    capped at 0.45) or `inferred` (from episodes).
  - **people** (`entities.py`): one `(:Person)` per name as heard (`per_marta`); full names are
    their own entity; a bare first name is never merged into a full name, it gets
    `POSSIBLY_SAME_AS` edges and status `provisional`/`ambiguous`.

  A note is used only after it matches its original context-engine event (same text, same
  hash). There is no mock-tuned vocabulary. Re-runs are idempotent (MERGE on content ids); a
  `watermark_ms` and `new_notes` are recorded on each `(:DreamRun)`.
- **Model-assisted extraction (opt-in, `--model-extraction`).** `model_extract.ModelExtractor`
  asks the Meta model (default `muse-spark-1.3`, non-contributor; `store: false`) for claims the
  rules missed, through one forced function call. Output is schema-checked and every claim must
  cite a note in its batch, name a person and an object that the note's text actually contains;
  the rest is rejected and counted. Accepted output is cached per note in `(:Extraction)`, so a
  note is never sent twice. Calls are capped (`DREAMER_EXTRACT_MAX_CALLS`), time out
  (`DREAMER_EXTRACT_TIMEOUT_S`), and any failure leaves the rule result in place.
  `FakeStructuredModel` gives deterministic offline tests; its output is labelled `fake:*`.
- **Guards.** `reset`, `all` and `load_bundle` refuse to run, so the agent's operational
  records are never touched. `run_cypher` uses `GRAPH.RO_QUERY` when available.

```bash
FALKORDB_URL=redis://localhost:6390 uv run python -m prod_dreamer_model.cli \
    --layout unified --user demo dream --offline --history-db data/demo/vixy_events.sqlite3   # from the repo root
uv run python -m prod_dreamer_model.cli load export.jsonl     # legacy layout: CE JSONL -> graph
```

## Graph schema

**Observed** — written by the loader, read-only to the model:

```
(:Day  {day_id, date, session_id, frame_count, note_count, first_ms, last_ms})
(:Frame {frame_id, ts_ms, timestamp, local_date, local_hour, local_min, dow,
         description, session_id, day_id, lat?, lon?})-[:ON_DAY]->(:Day)
(:Frame)-[:SAW {count, confidence}]->(:Class {name})
(:Note  {note_id, ts_ms, timestamp, text, kind, session_id, day_id})
        -[:ON_DAY]->(:Day)
```

`count` is the number of instances of a class **in that single frame**, so
"4 cups on the table" survives. It travels from the vision frame through
context-engine inside the video observation's free-form `source.counts`
metadata, because v1's `Detection` carries only a label and a confidence.

**Believed** — written by the dreamer:

```
(:Pattern {pattern_id, statement, kind, confidence, status, time_window?, days?,
           created_ms, updated_ms})
(:LongTermMemory {memory_id, statement, kind, confidence, status,
                  created_ms, updated_ms})
(:Pattern|:LongTermMemory)-[:SUPPORTED_BY]->(:Frame|:Note|:Day)
(:Pattern|:LongTermMemory)-[:ABOUT]->(:Class)
(:LongTermMemory)-[:DERIVED_FROM]->(:Pattern)
```

FalkorDB conventions are honoured: epoch-millisecond integers for time (temporal
types cannot be range-indexed), no null properties (optional ones are omitted
from the `SET`), and MERGE-based idempotent writes.

## Example result

After two mock weeks the dreamer reports, among others:

```
* Takes the bus to campus in the morning around 08:15 on weekdays,
  arriving on campus around 08:42.
* Eats lunch at the food court around 11:35 on weekdays.
* Studies in the library in the afternoon from about 12:37 on weekdays.
* Goes to the gym in the late afternoon around 16:31 on Tue, Thu.
* Does a supermarket run around 18:14 on Fri.
* Really likes the noodles at the canteen here.
* Remind me to buy milk at the supermarket.
* Was seen at a clinic on 1 day(s) — a one-off, not part of a routine.
```

The last line matters: the dreamer is instructed (and the rules enforce) that a
single occurrence, especially a medical or religious visit, is an `anomaly`,
never a habit.

## Configuration

Environment variables, read in this order (first file wins):
`$DREAMER_ENV_FILE`, `<package>/.env`, `<repo>/.env`, then the parent directory's
`.env` as a monorepo convenience.

| variable | default | meaning |
|---|---|---|
| `FALKORDB_URL` | — | `falkor://user:pass@host:port`; if unset, embedded `falkordblite` is used |
| `DREAMER_GRAPH_NAME` | `vixy_dreamer` (legacy) / `vixy_<user>` (unified) | graph name |
| `DREAMER_GRAPH_LAYOUT` | `legacy` | `unified` = the shared Vixy graph |
| `DREAMER_SESSION_ID` / `DREAMER_USER_ID` (or `VIXY_USER_ID`) | `wearer-01` | session identity |
| `DREAMER_ALLOW_INFERRED_EVIDENCE` | off | allow window-matched `backfill_evidence` |
| `DREAMER_EVENTS_DB` | `data/prod_dreamer_events.sqlite3` | context-engine SQLite file |
| `DREAMER_DB_PATH` | `data/prod_dreamer.rdb` | embedded FalkorDB file |
| `DREAMER_TZ` | `Asia/Singapore` | local timezone for hour/dow/time-of-day |
| `MODEL_API_KEY` | — | Meta key for the Responses API |
| `MUSE_MODEL` | `muse-spark-1.3` | model id. `-contributor` models (Meta may train on the content) are refused unless `DREAMER_ALLOW_CONTRIBUTOR_MODEL=1`, and only ever for synthetic data |
| `MUSE_STORE_RESPONSES` | on | `store: true` is needed for `previous_response_id` chaining |
| `MUSE_URL` | `https://api.meta.ai/v1/responses` | endpoint |
| `CONTEXT_ENGINE_SRC` | — | path to `context-engine/src` when it is not installed (only `seed` needs it) |

Note: tool-call chaining via `previous_response_id` requires
`store: true` (the default), so Meta stores the dream conversations.

## Mock data

`mock_data.py` generates a deterministic wearable week: home, the bus at ~08:12,
classes, lunch at the food court, the library, the bus home, gym on Tue/Thu, a
Friday supermarket run, weekend park and cafe, one clinic visit that is *not* a
habit, and six spoken notes. Class names are drawn from the vision model's
vocabulary so the mock matches real detection labels.

## Tests

```bash
uv run pytest -q          # or: python -m pytest -q
```

No network and no FalkorDB server are needed: frame parsing, the context-engine
round trip, the Cypher guard, the dream loop, the fallback, the readable diary
and report rendering are all covered with in-memory doubles. `test_unified.py` and
`test_evidence.py` run against a real embedded FalkorDB and context-engine.

## Layout

```
src/prod_dreamer_model/
  frames.py      vision-frame parsing (classes/count/confidence) -> v1 observation
  mock_data.py   synthetic wearable week
  bridge.py      context-engine ingestion + dreamer_event.v1 export
  graph.py       FalkorDB schema, loading, read-only Cypher guard, belief writes
  tools.py       the model's toolbox + validation
  dreamer.py     Muse Responses API loop, focus rounds, nudges
  heuristic.py   deterministic fallback dreamer (legacy Frame graph)
  consolidate.py deterministic fallback for the unified Vixy graph
  report.py      report/JSON rendering
  readable.py    the human-readable dream_readable.txt
  cli.py         seed | load | dream | report | reset | all
tests/           no network, no server
```

## Limits

- **Single user per graph.** Run one graph per person (`DREAMER_GRAPH_NAME`).
- **No retrieval API yet.** The app-facing question ("what do you know about
  me?") reads the graph directly; a small API over `:LongTermMemory` is the
  obvious next step.
- **Muse stores the conversations** (`store: true`) so tool chaining works.
- **Rules are tuned on mock data.** `heuristic.py` hour windows should be
  revisited once real footage replaces the simulator.
- **No forgetting yet.** Patterns can be set `inactive` but nothing prunes.
  `memory/`'s `forget-visit` is the reference design for that.


### Shared Vixy configuration (M3)

The runtime and standalone Dreamer share `MODEL_API_KEY` from the repository-root `.env` of the monorepo. Explicit process values (including an empty string for offline tests) win; `VIXY_ENV_FILE` selects a shared file, then the legacy `DREAMER_ENV_FILE` override and standalone fallback files apply. Model settings reload the same loader, without printing the key or enabling inference automatically. Unified wearable configurations reject contributor models even if the legacy synthetic-only override is set. The default remains non-contributor `muse-spark-1.3`; generation is opt-in.

Canonical project documentation and M3 evidence live in the monorepo's `docs/` (`../../docs/`), including `reports/VIXY_M3_IMPLEMENTATION_REPORT.md`.

The Dreamer CLI now defaults to offline even when a key is installed. Explicit `dream --online` or `all --online` enables the paid Muse tool loop; `--model-extraction` separately opts into budgeted paid semantic extraction. `--offline` remains compatible and disables the tool loop. No model flags are enabled by the MP4 process/replay commands.
