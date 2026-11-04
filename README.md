# Vixy

Vixy is a wearable AI assistant that observes, remembers and helps. This repository is the **whole
backend** in one place: perception, Context Engine, Episode Writer, Active Agent, unified FalkorDB
memory, Dreamer, retrieval, the Ask API and the MP4 simulator. A fresh clone is all you need.

```text
MP4 / camera observations
  -> YOLO11n + Whisper (local)          vixy/perception.py
  -> Context Engine + SQLite            components/context-engine        (Aryan)
  -> Episode Writer                     vixy/episode_writer.py
  -> Active Agent                       agent/                           (Samu)
  -> FalkorDB, one graph per user       memory/, docker-compose.yml      graph vixy_<user>, schema vixy.graph.v2
  <-> Dreamer                           components/dreamer-model         (Ara)
  -> retrieval / /v1/ask / notifications  vixy/retrieval.py, vixy/ask.py, vixy/api.py
```

This repository was created on 2026-11-04 from three original repositories at verified commits; see
[docs/MONOREPO_MIGRATION.md](docs/MONOREPO_MIGRATION.md). Validation results are in
[docs/reports/VIXY_MONOREPO_VALIDATION.md](docs/reports/VIXY_MONOREPO_VALIDATION.md).

## Layout

```text
vixy/
├── vixy/                     integrated runtime, CLI (python -m vixy), /v1 API, ask, M3 perception + replay
├── agent/                    Active Agent
├── memory/                   operational + episodic graph memory, schema.cypher
├── components/
│   ├── context-engine/       Context Engine (package vixy-context-engine, import context_engine)
│   └── dreamer-model/        Dreamer (package prod-dreamer-model, import prod_dreamer_model)
├── frontend/                 reserved for the app (Ara); see frontend/README.md
├── tests/                    runtime, agent, memory, integration and M3 tests
├── demo/                     MP4 simulator workflow; demo/inputs and demo/outputs are local-only
├── scripts/                  bootstrap.sh, test.sh, smoke_test.py, diagnose.sh
├── docs/                     architecture, API, contracts, schema, frontend handoff, reports
├── docker-compose.yml        FalkorDB server (localhost:6390)
├── Makefile                  the entry points below
├── pyproject.toml, uv.lock   one uv workspace: the root + components/*
└── .env.example              configuration template (no secrets)
```

## Install

Prerequisites:

- [`uv`](https://docs.astral.sh/uv/getting-started/installation/), which installs Python 3.12 itself
- Docker, for the FalkorDB server
- `make`
- for MP4 perception only: `ffmpeg`/`ffprobe` (`brew install ffmpeg`)

```bash
git clone https://github.com/meta-hackathon-athena/vixy.git
cd vixy
make install              # uv sync --frozen, .env from .env.example, FalkorDB, smoke test
make install-perception   # same + YOLO/Whisper extra and an MP4 replay smoke test
```

`make install` runs [scripts/bootstrap.sh](scripts/bootstrap.sh); `--no-docker` there uses the
embedded single-process graph instead. All components resolve from `components/` through the uv
workspace, and nothing outside the repository is cloned or referenced.

No paid model is called by default, and no key is needed. Real keys go only into your local `.env`,
which is gitignored. See the "Meta Muse" notes below.

## Run the backend

```bash
make demo-data VIXY_USER=app_demo    # two synthetic days -> Dreamer -> decisions (M2 demo, 18 checks)
make serve VIXY_USER=app_demo        # http://127.0.0.1:8765/v1  (HOST=, PORT= to change)
curl -s localhost:8765/v1/health
```

The app contract (endpoints, ids, polling notifications, simulator, CORS/auth) is in
[docs/FRONTEND_INTEGRATION.md](docs/FRONTEND_INTEGRATION.md). Field reference:
[docs/API.md](docs/API.md).

**Always pass a user id.** The default user is `demo`, whose graph `vixy_demo` is the team's
preserved M1 graph. `vixy demo --fresh` and `demo-m2 --fresh` delete that user's graph, and
`.env.example` points at the shared server, so use your own ids (`app_demo`, `m1_try`, …). The
`make` targets refuse `VIXY_USER=demo`.

Other commands (run from the repository root; `FALKORDB_URL` comes from `.env`):

```bash
uv run python -m vixy --user m1_try --state-dir data/m1_try demo --fresh    # M1 pipeline + self-checks
uv run python -m vixy --user app_demo --state-dir data/app_demo ask "What do you remember about Alex?"
uv run python -m vixy --user app_demo --state-dir data/app_demo decisions --limit 10
uv run python -m vixy --user app_demo --state-dir data/app_demo timeline 2026-11-02
uv run python -m vixy --user app_demo --state-dir data/app_demo dream        # offline consolidation
uv run python -m vixy --user app_demo --state-dir data/app_demo memories --about Alex
uv run python -m vixy --user u --state-dir data/u ingest events.jsonl | flush | recover | status | migrate
# the Dreamer as its own process on the same graph (Ara's CLI). Needs the shared server, i.e.
# FALKORDB_URL from .env or exported; without it, the Dreamer opens its own empty embedded file:
uv run python -m prod_dreamer_model.cli --layout unified --user app_demo dream --offline \
    --history-db data/app_demo/vixy_events.sqlite3
```

## MP4 simulator (Milestone 3)

```bash
make replay SCENARIO=demo/inputs/scenario_01/scenario.json SPEED=4    # process (YOLO+Whisper) then replay
make replay SCENARIO=demo/inputs/scenario_01/scenario.json REPLAY_ARGS=--fresh
```

The manifest format, your own recordings, pause/resume, reset rules and inspection commands are in
[demo/README.md](demo/README.md). `demo/inputs/**` and `demo/outputs/**` are never committed.

## Tests

| Command | What runs |
|---|---|
| `make test` | **unit**: Context Engine, Dreamer and runtime/agent/memory suites, offline, embedded graph |
| `make test-integration` | **integration** (FalkorDB server): runtime shared-server variants, 14 Active Agent scenarios, M1 + M2 demos, `/v1` HTTP smoke test |
| `make smoke-av` | **M3 audiovisual**: generated MP4 → local YOLO11n + Whisper → cache → replay acceptance (embedded + server), real-model media edge cases, MP4 replay smoke test |
| `make test-all` | all three |

All levels blank the model keys and use new disposable users (`it_*`, `smoke_*`, `m3_av_*`) whose
graphs are deleted afterwards. A single suite runs directly:
`uv run pytest -q components/context-engine`, `uv run pytest -q components/dreamer-model`, or
`uv run pytest -q`. The opt-in test for private real recordings is described in
[demo/README.md](demo/README.md#timestamp-boundary-diagnostics-and-real-recording-verification).

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs `scripts/test.sh unit` and
`integration` on every push to `main` and every PR, against a FalkorDB service container. It also
runs Context Engine standalone on Python 3.10. The audiovisual level is an optional manual job
(`workflow_dispatch`, input `audiovisual`), because it installs torch and downloads model weights.
It has been validated **locally** (`make smoke-av`) but has not yet run on GitHub Actions; see
[the validation report](docs/reports/VIXY_MONOREPO_VALIDATION.md#8-github-actions).

## Logs and diagnostics

- `logs/`: every `scripts/test.sh` and `make replay` run writes a timestamped log here; CI uploads it.
- `make diagnose`: versions, FalkorDB status and graph names, and which `.env` keys are set (names only) → `logs/diagnose-*.txt`.
- `demo/outputs/<user>/`: per-replay monitor `replay.jsonl` (contains transcripts: private), `summary.json`, caches and `state/`.
- `data/<user>/`: Context Engine and runtime SQLite for CLI/serve users.

## Meta Muse and what is verified

- **Runs locally:** everything in the default configuration: YOLO11n, Whisper, Context Engine,
  Episode Writer, the Active Agent rules and recall, FalkorDB, the Dreamer's offline rules, and `/v1/ask` rules.
- **Can use Meta Muse** (opt-in, needs `MODEL_API_KEY` and separate billing authorization):
  - Dreamer `dream --online` and `--model-extraction`
  - the agent recall reasoner and `/ask` rephrasing, when a model is passed to `VixyRuntime`
  - contributor models are refused in wearable paths
- **Not verified live:** no Muse inference call has been made. Only a metadata `GET /v1/models` was made in M3.
- **Real recordings:** the team's two real recordings process end to end (158 events → 47 Notes), but
  produce **no long-term memories and no proactive notifications**. Memory-aware behaviour is shown with
  synthetic data only (the M2 demo and the generated MP4 fixture).

## Documentation

[docs/README.md](docs/README.md) is the index:

- [ARCHITECTURE](docs/ARCHITECTURE.md)
- [GRAPH_SCHEMA](docs/GRAPH_SCHEMA.md)
- [CONTRACTS](docs/CONTRACTS.md)
- [API](docs/API.md)
- [FRONTEND_INTEGRATION](docs/FRONTEND_INTEGRATION.md)
- [MONOREPO_MIGRATION](docs/MONOREPO_MIGRATION.md)
- the M1–M3 and monorepo reports in [docs/reports/](docs/reports/)

Component docs: [agent/README.md](agent/README.md), [memory/README.md](memory/README.md),
[components/context-engine/README.md](components/context-engine/README.md),
[components/dreamer-model/README.md](components/dreamer-model/README.md).

## Security

- Never commit `.env`, keys, recordings, transcripts, SQLite files, graph data, model weights or caches. `.gitignore` covers all of them.
- This repository was built from clean source snapshots and contains no history from the original repositories. A Baseten key committed in the original master-repo history (commit `aa020ad`) is therefore **not** in this repository; the account owner confirmed its revocation on 2026-11-04.
- The `/v1` API has no authentication and binds to `127.0.0.1` by default. It doesn't check `Content-Type`, `Origin` or `Host` either, so a malicious web page could send it cross-site POSTs (CSRF) or reach it through DNS rebinding. This is an open issue; see [FRONTEND_INTEGRATION §10](docs/FRONTEND_INTEGRATION.md#10-cors-and-authentication) for the planned fix. Don't leave `serve` running while browsing untrusted sites.
