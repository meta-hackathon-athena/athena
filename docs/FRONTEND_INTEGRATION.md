# Frontend integration (Vixy app ↔ backend)

For Ara's app. This describes the backend API in the [Vixy monorepo](../README.md) as it exists at release M3: `vixy/api.py` (routes) and `vixy/ask.py` (questions). Nothing here needs a backend change. The field-level reference is [API.md](API.md), and the samples below are real responses captured from the M3 release.

The short version:

- Plain JSON over HTTP at `http://127.0.0.1:8765/v1`.
- One backend process serves one user.
- No streaming: poll.
- No authentication and no CORS headers.

## 1. Start the backend

The first time, from a fresh clone (see the [README](../README.md#install)). Every component is in this one repository:

```bash
git clone https://github.com/meta-hackathon-athena/vixy.git && cd vixy
make install                      # = ./scripts/bootstrap.sh: uv env, .env, FalkorDB on :6390, smoke test
```

Then pick the data the app should show.

**A. Ready-made two-day demo.** No video and no models needed. This gives memories, three notifications, one question and a timeline:

```bash
make demo-data VIXY_USER=app_demo   # = uv run python -m vixy --user app_demo --state-dir data/app_demo demo-m2 --fresh
make serve VIXY_USER=app_demo       # = uv run python -m vixy --user app_demo --state-dir data/app_demo serve --port 8765
```

The `make` targets set `FALKORDB_URL=redis://localhost:6390` and refuse `VIXY_USER=demo`. Run the raw commands from the repository root.

**B. Video simulator.** MP4 → YOLO/Whisper → the real backend. `serve` and `replay` run as two processes that share the FalkorDB server, so the app watches decisions appear while the replay runs:

```bash
make install-perception                                     # once: perception extra + ffmpeg check + MP4 smoke
uv run --extra perception python -m vixy fixture --directory demo/inputs/app_fixture --fixture-user m3_app
uv run --extra perception python -m vixy process --scenario demo/inputs/app_fixture/scenario.json
uv run --extra perception python -m vixy reset-replay --scenario demo/inputs/app_fixture/scenario.json
# terminal 1: the API (the sessions are the scenario's per-recording session ids)
VIXY_SESSION_IDS=m3_app_day1,m3_app_day2 FALKORDB_URL=redis://localhost:6390 \
  uv run python -m vixy --user m3_app --state-dir demo/outputs/m3_app/state serve --port 8765
# terminal 2: the simulator
FALKORDB_URL=redis://localhost:6390 uv run --extra perception python -m vixy replay \
  --scenario demo/inputs/app_fixture/scenario.json --speed 4
```

**C. An empty user**, which the app fills through `POST /v1/events/*` (section 9):

```bash
FALKORDB_URL=redis://localhost:6390 uv run python -m vixy --user app_live --state-dir data/app_live serve
```

Rules that apply to every mode:

- The user id becomes the graph name `vixy_<user>`.
- Never use the user `demo`: its graph `vixy_demo` is the team's preserved M1 graph.
- Run `vixy reset-replay` only on `m3_*` / `test_*` disposable users.
- No paid model is called. `/ask` and the agent run on rules, and nothing needs `MODEL_API_KEY`.

## 2. URL and configuration

| Setting | Default | Notes |
|---|---|---|
| Base URL | `http://127.0.0.1:8765/v1` | `serve --host H --port P` |
| iOS simulator / web on the same Mac | `http://127.0.0.1:8765/v1` | |
| Android emulator | `http://10.0.2.2:8765/v1` | host loopback alias |
| Physical phone (Expo Go etc.) | `http://<laptop LAN IP>:8765/v1` | needs `serve --host 0.0.0.0`; **no auth**, so only on a trusted network, and stop it afterwards |
| `FALKORDB_URL` | from `.env` (`redis://localhost:6390`) | empty means an embedded graph: one process only, so it can't run `serve` and `replay` together |
| `VIXY_USER_ID` / `--user` | `demo` | always pass `--user` (see above) |
| `VIXY_SESSION_IDS` | the user id | comma separated; set it to the scenario's session ids when serving a replay user, so `status.events` counts them |
| `VIXY_TZ` | `Asia/Singapore` | the day boundaries used by `timeline?date=` |

Keep the base URL in one app config value, e.g. `EXPO_PUBLIC_VIXY_API`, so no backend change is ever needed.

## 3. Conventions

- Every response is a JSON object with a `schema` key. Success is `"vixy.api.v1"`. Errors are `"vixy.error.v1"` with HTTP 400/404/413/500 and the body `{"error": {"code", "message"}}`, for example `{"schema":"vixy.error.v1","error":{"code":"not_found","message":"no route GET /v1/nope"}}`. Error messages never contain transcript text.
- Times sent to the API are ISO-8601 **with an offset**. A naive time returns 400.
- Times returned are ISO strings and/or `*_ms` epoch milliseconds.
- `ts_ms` on notes, visits and decisions is **simulated event time**, not wall-clock time. During a replay it is the scenario's time.
- `v1` responses may gain fields, so ignore unknown fields. A breaking change would get `/v2`.
- Requests are serialized: one at a time per process. POST bodies are limited to 1 MB.

## 4. Endpoints

| Method | Path | Query / body | Returns |
|---|---|---|---|
| GET | `/v1/health` | | `{ok, graph, graph_schema}` |
| GET | `/v1/status` | | `{status}`: event counts per session, ledger, graph label counts, agent metrics, `ask_model_usage` |
| POST | `/v1/ask` | `{question, conversation_id?, now?}` | `{result}`: `vixy.ask.v1` |
| GET | `/v1/memories` | `about=<name>`, `status=active[,unconfirmed,…]`, `limit` (1–500, default 50) | `{memories, patterns}` |
| GET | `/v1/memories/{memory_id}` | | `{memory}` with `people`, `derived_from`, `conflicts_with`, `supersedes`, `superseded_by`, `evidence[]` (each with `original`) |
| GET | `/v1/people/{person_id}` | | `{person}`: name, status, aliases, `possibly_same_as`, memory ids |
| GET | `/v1/timeline` | `date=YYYY-MM-DD`, or `from=ISO&to=ISO` (≤ 31 days); `limit` (≤ 2000, default 500) | `{from, to, items}`: Visits and Notes by time |
| GET | `/v1/decisions` | `action=NOTIFY\|ASK_USER\|NO_ACTION\|MEMORY_UPDATE`, `since=ISO`, `limit` (1–500, default 50), `delivered=1` | `{decisions}`, newest first |
| GET | `/v1/decisions/{decision_id}` | | `{decision}`, including `memories` (each fully explained with evidence) |
| POST | `/v1/events/audio` | `{"asr": {...}}` or a Context Engine `AudioSegment` | `{outcome}` |
| POST | `/v1/events/video` | `{"perception": {...}}` or a Context Engine `VideoObservation` | `{outcome}` |
| POST | `/v1/dream` | | `{dream}`: offline consolidation run summary |
| GET | `/v1/questions` | `status=open\|answered\|all`, `limit` | `{questions}`: the agent's clarification questions with `about`, `proposal` and `heard` |
| GET | `/v1/questions/{id}` | | `{question}` |
| POST | `/v1/questions/{id}/answer` | `{answer: "yes"\|"no", now?}` | `{decisions, question}` |
| GET | `/v1/commitments` | | `{commitments}`: the user's plans |
| GET | `/v1/reminders` | `status=open\|done\|all` | `{reminders}` |

Nothing else exists. There are no list routes for people or places, and no route to dismiss a question without answering it, acknowledge a notification, or control a replay.

## 5. Identifiers

All ids are opaque strings. Store them as returned and never parse them. They are deterministic, so replaying the same events yields the same ids and no duplicates.

| Prefix / shape | What | Where you get it |
|---|---|---|
| `asr:<source>:<segment>`, `cam0:<frame>`, `asr:day1:<hash>-N` | **event id**: the original Context Engine event | `source_event_id`, `trigger_event_id`, `first_event_id`, `outcome.event_id` |
| `note_…` | Note: one utterance (observed) | timeline, `sources[]`, evidence |
| `vis_…` | Visit: a stay at a place (observed) | timeline, `sources[]` |
| `plc_…` | Place | `visit.place_id` |
| `ltm-claim-…`, `ltm-fact_…` | **memory** (LongTermMemory, inferred by the Dreamer) | `/memories`, `items[]`, `supporting_memory_ids` |
| `per_…` | Person | `memory.people[].id`, `memory.subject_id` |
| `dec_…` | **decision** (AgentLog): every agent decision, including notifications | `/decisions`, `outcome.decisions[].decision_id` |
| `conv_…` | ask conversation | `result.conversation_id` |
| `dream_…` | Dreamer run | `dream.fallback.run_id` |

Every item has `label` (`Note`, `Visit`, `LongTermMemory`, `Person`, `AgentLog`, …) and `layer`:

- `observed`: what the wearable saw or heard
- `inferred`: what the Dreamer concluded
- `entity`
- `operational`: agent records

## 6. Timeline, people and evidence

### Timeline

`GET /v1/timeline?date=2026-11-02` (abridged):

```json
{"schema": "vixy.api.v1", "from": "2026-11-02T00:00:00+08:00", "to": "2026-11-03T00:00:00+08:00",
 "items": [
  {"id": "vis_2212daa8c9ae139e", "label": "Visit", "layer": "observed", "kind": "stay",
   "start_ms": 1793581200000, "end_ms": 1793583580000,
   "start_time": "2026-11-02T01:00:00Z", "end_time": "2026-11-02T01:39:40Z",
   "place_type": "office", "activity": "working", "tags": ["office", "laptop", "monitor", "person", "office chair"],
   "place_id": "plc_6e9a4d2ccc29", "first_event_id": "cam0:m2d0v1f0000", "last_event_id": "cam0:m2d0v1f0119",
   "local_date": "2026-11-02", "sensitive": false},
  {"id": "note_7e7ea96f6e51a2b5", "label": "Note", "layer": "observed", "kind": "note",
   "text": "Marta has a presentation on Thursday at 9 AM.", "ts_ms": 1793581800000,
   "timestamp": "2026-11-02T01:10:00Z", "speaker_id": "spk_1", "speaker_role": "other",
   "source_event_id": "asr:rec-m2-2026-11-02:m2seg001", "local_date": "2026-11-02"}]}
```

- Note `kind` is `note`, `reminder` (with `status: open|done`), and so on.
- `speaker_role` is `wearer`, `other` or `unknown`.
- `speaker_id` is a diarization label, not a name.

### People

`GET /v1/people/per_alex`:

```json
{"schema": "vixy.api.v1", "person": {"id": "per_alex", "name": "Alex", "status": "resolved", "aliases": ["alex"],
 "possibly_same_as": [], "memories": ["ltm-claim-227fa54c192fdd53"], "label": "Person", "layer": "entity"}}
```

There is no people list. To build one, collect `people[]` from memory details. Alternatively, use `memories?about=<name>`, which matches a name, not an id.

### Memories and source evidence

`GET /v1/memories?limit=5` returns items like:

```json
{"id": "ltm-claim-227fa54c192fdd53", "statement": "Alex is allergic to peanuts.", "kind": "fact",
 "status": "active", "confidence": 0.65, "basis": "reported_speech", "subject": "Alex",
 "predicate": "allergic_to", "object": "peanut", "epistemic_status": "reported", "contested": false,
 "reported_by": ["other"], "about": ["Alex"], "evidence_count": 1, "updated_ms": 1793784927948,
 "label": "LongTermMemory", "layer": "inferred"}
```

`GET /v1/memories/ltm-claim-227fa54c192fdd53` adds the provenance chain. Each `evidence[]` entry is the Note, and `original` is the Context Engine event, re-read and hash-checked on every request:

```json
{"memory": {"id": "ltm-claim-227fa54c192fdd53", "statement": "Alex is allergic to peanuts.",
  "people": [{"id": "per_alex", "name": "Alex", "status": "resolved"}], "subject_id": "per_alex",
  "derived_by": "vixy.offline_consolidation.v2", "derived_from": [], "conflicts_with": [],
  "supersedes": [], "superseded_by": [], "speakers": ["spk_2"],
  "evidence": [{"label": "Note", "id": "note_ddcbe3873a16fbef", "text": "Alex is allergic to peanuts.",
    "timestamp": "2026-11-02T04:30:00Z", "speaker_id": "spk_2", "speaker_role": "other",
    "source_event_id": "asr:rec-m2-2026-11-02:m2seg003", "payload_sha256": "f48d4a79…",
    "original": {"event_id": "asr:rec-m2-2026-11-02:m2seg003", "kind": "audio", "ingest_seq": 214,
                 "payload_sha256": "f48d4a79…", "matches": true, "text": "Alex is allergic to peanuts."}}]}}
```

How to display a memory:

- `epistemic_status` is one of `observed`, `self_reported`, `reported`, `hypothesis`, `inferred` or `operational`. Show `reported` and `hypothesis` as **unverified**.
- When `contested: true` or `conflicts_with` is non-empty, show both sides.
- `original.matches: true` means the evidence is intact. `false` or `original: null` means the evidence can't be verified, so say so.

## 7. Ask API

```bash
curl -s localhost:8765/v1/ask -H 'Content-Type: application/json' \
  -d '{"question": "What do you remember about Alex?", "now": "2026-11-03T21:00:00+08:00"}'
```

```json
{"schema": "vixy.api.v1", "result": {"schema": "vixy.ask.v1",
 "question": "What do you remember about Alex?", "conversation_id": "conv_a9460812b984",
 "now": "2026-11-03T21:00:00+08:00", "status": "answered",
 "answer": "About Alex: Alex is allergic to peanuts (reported by someone else; not verified, confidence 0.65, first said Monday 2 Nov).",
 "interpretation": {"type": "about_entity", "people": ["Alex"], "topic_keywords": [], "time_range": null, "follow_up": false},
 "items": [{"id": "ltm-claim-227fa54c192fdd53", "label": "LongTermMemory", "layer": "inferred",
            "epistemic_status": "reported", "statement": "Alex is allergic to peanuts.", "confidence": 0.65,
            "status": "active", "source_ids": ["note_ddcbe3873a16fbef"], "…": "…"}],
 "sources": [{"id": "note_ddcbe3873a16fbef", "label": "Note", "source_event_id": "asr:rec-m2-2026-11-02:m2seg003",
              "speaker_role": "other", "speaker_id": "spk_2", "time": "2026-11-02T12:30:00+08:00",
              "statement": "Alex is allergic to peanuts.", "verified": true}],
 "answered_by": "rules:vixy.ask.v1", "model_usage": null}}
```

- **`status`** is one of:
  - `answered`
  - `insufficient_evidence`: nothing supports an answer, and `answer` says so. Show it as is; the backend never invents an answer.
  - `ambiguous`
  - `unsupported`
- **Follow-ups:** send the returned `conversation_id` back. "Why did you remind me about this?" and "What evidence supports that memory?" then resolve against the previous answer. Conversations survive a backend restart.
- **`now`:** send it for simulated data, e.g. the end of the replayed day. It anchors words like "yesterday".
- **Question types (`interpretation.type`):** `day_activity`, `schedule`, `what_said`, `about_entity`, `why_decision`, `evidence`, `search`.
- **Render `answer` as is**, and list `sources[]` as the citations, each with `verified`.

## 8. Agent notifications (polling; there is no streaming)

There is no WebSocket, SSE or push. Poll `GET /v1/decisions`.

`delivered=1` means `status == "done"`, and that **includes `NO_ACTION`**. Always filter by action:

- `NOTIFY`: a notification
- `ASK_USER`: a yes/no question

```bash
curl -s 'localhost:8765/v1/decisions?action=NOTIFY&delivered=1&limit=50'
```

```json
{"schema": "vixy.api.v1", "decisions": [
 {"id": "dec_ac4a60be82cd", "ts_ms": 1793697603000, "action": "NOTIFY", "priority": "normal", "status": "done",
  "trigger": "audio", "trigger_event_id": "asr:rec-m2-2026-11-03:m2seg007", "confidence": 0.65,
  "message": "Someone mentioned yesterday that Alex is allergic to peanuts. I haven't verified it. You may want to check with Alex before buying.",
  "reason": "remembered allergic_to 'peanut' conflicts with what is planned (peanut); reported memory ltm-claim-227fa54c192fdd53",
  "reasoning": "rules:vixy.recall.v1", "dedup_key": "recall:ltm-claim-227fa54c192fdd53",
  "supporting_memory_ids": ["ltm-claim-227fa54c192fdd53"],
  "supporting_memories": [{"id": "ltm-claim-227fa54c192fdd53", "statement": "Alex is allergic to peanuts.",
     "epistemic_status": "reported", "confidence": 0.65,
     "evidence": [{"id": "note_ddcbe3873a16fbef", "label": "Note", "source_event_id": "asr:rec-m2-2026-11-02:m2seg003"}]}],
  "concerns": [{"label": "Note", "id": "note_ddcbe3873a16fbef"}, {"label": "LongTermMemory", "id": "ltm-claim-227fa54c192fdd53"}],
  "label": "AgentLog", "layer": "operational"},
 {"id": "dec_2dddef8edf54", "ts_ms": 1793697440000, "action": "NOTIFY", "priority": "normal", "status": "done",
  "trigger": "place_entered", "message": "You're at the supermarket. Reminder: buy milk at the supermarket",
  "reason": "open reminder matches current place (supermarket)", "supporting_memories": [], "…": "…"}]}
```

An `ASK_USER` decision looks like this:

```json
{"id": "dec_bade2fa182c0", "action": "ASK_USER", "status": "done", "priority": "low",
 "message": "Mark \"buy milk at the supermarket\" as done?", "reason": "user may have completed an open reminder"}
```

Answer it with `POST /v1/questions/{question_id}/answer` and `{"answer": "yes"}` or `"no"` (list open questions with `GET /v1/questions`). A "yes" runs the agent's own allow-listed write, for example confirming a plan's new time or completing a reminder; "no" changes nothing. The web app in [`frontend/web/`](../frontend/web/README.md) uses exactly this.

Polling recipe:

1. Every 2–5 s, call `GET /v1/decisions?action=NOTIFY&delivered=1&since=<cursor>` (and the same with `action=ASK_USER`).
2. `since` is inclusive (`ts_ms >= since`), so de-duplicate by `id`.
3. Advance the cursor to the newest `ts_ms` you've seen, as ISO with an offset, e.g. `new Date(ts).toISOString()`.
4. `ts_ms` is simulated time. If you replay from scratch with `--fresh`, reset the cursor.
5. For a "why?" screen, call `GET /v1/decisions/{id}`. Its `memories[]` carry full evidence, the same as `/v1/memories/{id}`.
6. Read state is the app's job. The backend has no acknowledge or dismiss.

## 9. Simulator controls (what really exists)

| Control | How |
|---|---|
| Replay MP4 days through the real backend | CLI `vixy replay --scenario <manifest> [--fresh] [--speed N] [--question "..."]` |
| Speed | `--speed 0` = instant virtual time; `N` = N× media speed; gaps between days are skipped |
| Pause / resume | `--pause-file demo/outputs/PAUSE`: create the file to pause, delete it to resume |
| Stop / resume | Ctrl-C, then rerun the same command; it resumes and already-processed events are `duplicate` |
| Reset a disposable replay user | `vixy reset-replay --scenario <manifest>` (only `m3_*`/`test_*` users with `disposable: true`) |
| Inject events from the app | `POST /v1/events/audio` and `POST /v1/events/video` |
| Consolidate (Dreamer) | `POST /v1/dream`, or `vixy dream` |

None of the replay controls are HTTP routes. The app can show replay progress only by polling `/v1/status` and `/v1/decisions`.

Injecting events (bodies as accepted today):

```bash
curl -s localhost:8765/v1/events/audio -H 'Content-Type: application/json' -d '{"asr": {"segment_id": "app-001",
  "start_time": "2026-11-04T09:00:00+08:00", "end_time": "2026-11-04T09:00:03+08:00",
  "text": "Remind me to call Marta tomorrow.", "speaker_id": "spk_wearer", "speaker_is_wearer": true}}'
# -> {"outcome": {"status": "processed", "event_id": "asr:mic0:app-001", "kind": "audio", "ingest_seq": 705,
#     "note_id": "note_fb0fed1d64101d85", "visit_ids": [], "decisions": [{"action": "NO_ACTION",
#     "reason": "reminder stored by ingestion; it triggers at the matching place", "decision_id": "dec_fdfdaf828b98", ...}]}}
# posting the same segment again -> "status": "duplicate", no decisions
curl -s localhost:8765/v1/events/video -H 'Content-Type: application/json' -d '{"perception": {"frame_id": "app-f001",
  "timestamp": "2026-11-04T09:01:00+08:00", "classes": ["cup", "laptop"]}}'
# -> {"outcome": {"status": "processed", "event_id": "cam0:app-f001", "kind": "video", "decisions": [], ...}}
```

- Optional perception fields are `latitude` and `longitude`.
- Visits are written when a stay closes, i.e. after enough frames or on flush/replay end.
- A notification comes back in `outcome.decisions` *and* appears in `/v1/decisions`.
- `POST /v1/dream` returns the run summary. Offline, it reports `"used_fallback": true, "errors": ["no model client configured"]`, which is normal.

## 10. CORS and authentication

- **Authentication: none.** Anyone who can reach the port can read the memories and post events. The default bind is `127.0.0.1`. Use `--host 0.0.0.0` only on a trusted network, and only for as long as needed.
- **No CSRF / origin protection (open issue).** POST routes accept a JSON body sent with any `Content-Type`, and the server checks neither `Origin` nor `Host`. As a result, even on `127.0.0.1`:
  - a web page open in the developer's browser could POST to `/v1/events/*` or `/v1/dream` cross-origin, without being able to read the response;
  - DNS rebinding could expose GET routes.

  Don't keep `serve` running while browsing untrusted sites. The planned fix will change the API's behaviour and go through its own PR: require `Content-Type: application/json`, an allow-list for `Origin` and `Host`, and a per-install bearer token. The app should already send `Content-Type: application/json` on every POST.
- **CORS: none.** No `Access-Control-Allow-*` headers are sent, and `OPTIONS` returns `501`.
  - Native React Native / Expo `fetch` on iOS and Android is not subject to CORS, so it works.
  - A **browser** build (Expo web, Next.js client components) on another origin will be blocked. Use a same-origin dev proxy on the frontend side instead, e.g. Next.js `rewrites()` from `/vixy/:path*` to `http://127.0.0.1:8765/v1/:path*`, or a Vite/Metro dev-server proxy. Send `Content-Type: application/json` on POSTs.

## 11. Known limitations

- **One user per backend process.** To switch users, restart `serve` with another `--user`.
- **Embedded storage** (`FALKORDB_URL` empty) is single-process. Use the Docker FalkorDB server whenever `serve` and `replay`/`ingest` run at the same time.
- **Polling only.** There is no push, no acknowledgement, and no people/places list routes. `ASK_USER` questions can be answered with `POST /v1/questions/{id}/answer`.
- **Speaker identity:** `speaker_id` values are diarization labels or manifest annotations, not recognized people. Answers say they can't confirm who spoke.
- **Real recordings vs. synthetic data:**
  - The team's real MP4 recordings were processed end to end: 158 events → 47 Notes → 2 Dreamer runs.
  - They produced **no long-term memories and no proactive notifications**. Real-recording data gives timeline Notes and keyword `/ask` answers only.
  - Memory-aware notifications are demonstrated on the synthetic M2 demo (mode A) and the generated MP4 fixture (mode B).
- **Timestamps:** ASR segment times are model estimates. Empty detections don't mean an empty scene.
- **No paid model calls**, so `answered_by` is `rules:vixy.ask.v1`. A model-backed mode exists only as an explicit, separately authorized opt-in.

## 12. Connection smoke test (from the app's side)

With the backend from section 1A running:

```bash
B=http://127.0.0.1:8765/v1
curl -sf $B/health                                    # {"schema":"vixy.api.v1","ok":true,"graph":"vixy_app_demo",...}
curl -sf "$B/decisions?action=NOTIFY&delivered=1" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["decisions"]), "notifications")'   # 3
curl -sf $B/ask -H 'Content-Type: application/json' -d '{"question":"What do you remember about Alex?"}' | python3 -m json.tool | head
```

Or the same from the app, framework-free:

```js
const API = process.env.EXPO_PUBLIC_VIXY_API ?? "http://127.0.0.1:8765/v1";
const get = (p) => fetch(API + p).then((r) => r.json());
const health = await get("/health");                         // health.ok === true
const { decisions } = await get("/decisions?action=NOTIFY&delivered=1");
const ask = await fetch(API + "/ask", { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ question: "What do you remember about Alex?" }) }).then((r) => r.json());
console.log(health.graph, decisions.length, ask.result.status, ask.result.sources.length);
```

The backend's own end-to-end check is `uv run python scripts/smoke_test.py`.

## 13. Working against the backend without changing it

- The app's place is [`frontend/`](../frontend/README.md) in this repository. It talks to the backend only over HTTP, through one base-URL setting, and never imports the Python packages.
- Get the backend with `make install`. Update it with `git pull --ff-only && make install` (see the README).
- Use your own user ids, e.g. `app_demo`, `app_live`, or `m3_app` for replays. Never use `demo`.
- If the app needs something the API doesn't have (a people list, an answer route, push, CORS), open an issue or PR against `vixy/api.py` in this repository. Don't patch a local copy.
