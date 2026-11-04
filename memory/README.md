# Vixy Memory (formerly Veyra)

Turns the vision model's per-frame output (plus GPS and spoken notes) into a memory graph in FalkorDB. That graph can answer:

- "Where did I go yesterday?"
- "Have I been here before?"
- "Why do you think I like Koufu?"
- "Remind me to buy milk at the supermarket."

## Run it

```bash
uv sync
uv run python -m memory.cli demo        # simulated week → visits → facts → example questions
uv run pytest -q                        # tests
```

No Docker needed: by default it runs an **embedded FalkorDB** (`falkordblite`) stored in `data/veyra.rdb`. To use a real server, set `FALKORDB_HOST` (plus `FALKORDB_PORT` and `FALKORDB_PASSWORD`). The code is identical either way.

```bash
uv run python -m memory.cli ingest frames.jsonl --gps gps.jsonl   # vision output (+ optional GPS track)
uv run python -m memory.cli note "remind me to buy milk at the supermarket"
uv run python -m memory.cli consolidate                            # episodic → semantic ("nightly")
uv run python -m memory.cli day 2026-10-26
uv run python -m memory.cli facts | explain <fact_id> | last-seen laptop
uv run python -m memory.cli context --lat 1.3467 --lon 103.68      # what the planner would get
uv run python -m memory.cli name-place <place_id> "Koufu"
uv run python -m memory.cli forget-visit <visit_id>
```

All commands accept `--user <id>`. Each user gets their own graph (`vixy_<id>`; set `VIXY_GRAPH_PREFIX=veyra` to open graphs created before the rename), so a query can't reach another user's memory. In the integrated runtime this same graph also holds the Dreamer's beliefs; see `docs/GRAPH_SCHEMA.md`. Notes are written by the Episode Writer (`vixy/episode_writer.py`) from context-engine events.

## See the graph

```bash
uv run python -m memory.cli viz     # writes data/graph_<user>.html and opens it in your browser
```

An interactive view of the whole memory graph:

- **Colours:** blue = episodic (visits, notes), green, grey and orange = semantic (places, tags, facts).
- **Borders:** red = sensitive place. Dashed = inactive fact.
- **Click** a node to see its properties (times shown in local time) and its connections, grouped by relationship. Click a connection to jump to it.
- **Double-click** to focus on a node's neighbourhood; double-click again to expand one more hop. For example, double-click a Fact to see the visits that support it.
- **Filter** with the layer checkboxes. Tags are hidden by default because they connect to almost everything.
- **Search** box: e.g. `koufu`, `laptop`, `eating`.

The page is a snapshot. Re-run `viz` after ingesting new data.

## Input

This is the vision model's current output, used as-is:

```json
{"frame_id": "99b232d8abc9", "timestamp": "2026-10-26T09:38:46.744729+00:00",
 "classes": [{"class": "person", "count": 2, "confidence": 0.94},
             {"class": "cup", "count": 4, "confidence": 0.72}]}
```

- **`confidence`** filters weak detections: a class needs at least 0.5 to count in a frame.
- **`count`** is kept as the most instances seen in any single frame. It's stored on the `SAW` edge as `max_count`, and on the visit as `people_max`, an anonymous headcount: never identities.
- **Older formats still parse:** `"classes": ["person", "cup"]` (each class gets confidence 1.0), or `{"label", "confidence"}` objects. An `image` path, if sent, is dropped, because memory keeps classes, not pictures.
- **GPS** can be inline (`"gps": {"lat","lon","accuracy_m"}`) or a separate track file, joined to each frame by nearest timestamp (within 30 s).
- **Spoken notes** come from audio transcripts via `store.add_note(text, ts_ms)`.

## How it works

```
frames (1 every few s) ──segmenter──▶ Visit (episode) ──consolidate──▶ Fact (belief)
                                         │                               │
                                         ├─AT──▶ Place (GPS)  ◀──ABOUT───┤
                                         ├─SAW─▶ Tag (class)             └─SUPPORTED_BY─▶ Visit / Note
                                         └─NEXT─▶ next Visit
spoken note ──▶ Note ──DURING──▶ Visit        "I love X" → preference Fact,  "remind me…" → reminder
```

The graph has two layers:

- **Episodic:** what happened and when. Made of `Visit` and `Note` nodes, both timestamped.
- **Semantic:** what we believe. Made of `Place`, `Tag` and `Fact` nodes.

Both live in one graph, and each Fact links to the episodes behind it, so "why?" is a single graph hop.

**1. Segmenter** (`segmenter.py`). Groups consecutive frames into one Visit, so 30 frames at the food court become one visit, not 30. A new visit starts when any of these happens:

- no frames for 5 min
- GPS moves more than 120 m from the visit's centre (ignored on a bus or train)
- the classes stop matching for 3 frames in a row; one odd frame doesn't split a visit

Blips shorter than 3 frames or 30 s are dropped. A class becomes a visit tag only if it appears in at least 20 % of the visit's frames, which filters one-frame false positives.

**2. Rules** (`rules.py`). Tags are turned into a `place_type` (food_court, classroom, library, gym, supermarket, transit, home…) and an `activity` (eating, studying, working, exercising, commuting…).

- Scene classes like "food court" weigh more than objects like "plate".
- With no scene class, the place stays `unknown`. The Koufu sample frame is an example: the rules don't guess.
- Every cue word exists in `yolomodelvision/classes.txt`.

**3. Places.** Places come from GPS. A visit joins an existing place within 75 m of the same type. A café next to a food court stays a separate place. Names come from the user (`name-place`), never from a name looking similar.

**4. Consolidation** (`store.consolidate`). This is the episodic-to-semantic step:

- `frequent_place`: visited on at least 3 **distinct days** in the last 28 days. Ten visits on one day count once. Confidence is `days / (days + 2)`.
- `usual_activity`: the same activity at a place on at least 60 % of those days.
- Facts that lose support become `inactive`. They aren't deleted, so history is kept.
- **Sensitive places** (clinic, hospital, place of worship) are stored as visits but **never** become facts.

**5. Notes.** These come from audio transcripts.

- `remind me … <place type>` becomes an open reminder, triggered when the context is that place type.
- `I like / love / hate …` becomes a user-stated preference fact (confidence 0.98). User statements outrank inferences.

**6. Deletion.** `forget-visit` really deletes the visit and its links, then re-runs consolidation, so facts that depended on it lose support.

## Graph schema

| Node | Key properties |
|---|---|
| `Visit` | `id, kind (stay/transit), start_ms, end_ms, local_date, dow, start_min, place_type, activity, tags[], frames, sensitive, people_max?, lat?, lon?` |
| `Note` | `id, text, ts_ms, local_date, kind (note/reminder), status?, trigger_place_type?` |
| `Place` | `id, place_type, name?, lat, lon, geo (point), visits, sensitive` |
| `Tag` | `name` (a vision class) |
| `Fact` | `id, kind (frequent_place/usual_activity/preference), source (inferred/user), status, confidence, support_days?, statement?` |

Relationships: `AT`, `SAW {ratio, conf, max_count}`, `NEXT {gap_min}`, `DURING`, `ABOUT`, `SUPPORTED_BY`.

**FalkorDB gotchas that shaped this design:**

- Times are epoch-ms integers, because temporal types can't be range-indexed.
- Properties are never null. Missing values are left out.
- Writes use `MERGE`, so re-ingesting the same frames creates nothing new.

## Limits and next steps

- **Thresholds are tuned on simulated data** (`demo.py`), not real wearable footage. Expect to adjust `SegmenterConfig` and the rules once real frames arrive.
- **Without GPS, visits have no Place.** They still get a place type, activity and tags, but "have I been here before?" needs GPS.
- **Note parsing is keyword-based.** An LLM could turn transcripts into notes, reminders and preferences more robustly. The node types would stay the same.
- **Possible additions:**
  - time-of-day routines (e.g. "leaves campus around 16:30"); `usual_arrival_min` is already stored
  - vector search over visit descriptions
  - a small API for the app or planner
