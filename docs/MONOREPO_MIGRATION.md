# Monorepo migration (2026-11-04)

`meta-hackathon-athena/vixy` consolidates the integrated Vixy backend, previously split across
three repositories, into one repository. This document records:

- where every file came from
- what changed and why
- which differences in behaviour are known

## 1. Sources

The sources were clean snapshots of the verified `main` commits, taken with `git archive`. **No Git history was imported**, so the Baseten credential committed in master-repo's history (commit `aa020ad`) is not in this repository.

| Original repository | `main` commit (source) | Tree | Files | Destination |
|---|---|---|---|---|
| https://github.com/meta-hackathon-athena/master-repo | `b88542b70ce372da9cab0dc9d37258443143c57a` | `5e19a98aa0cd01af44c931bb2b598c210e8c0d8b` | 93 | repository root |
| https://github.com/meta-hackathon-athena/context-engine | `4bb5cc2c4f1a4ff67e8a459a0d92b895ad2afd1f` | `fc44db6c26c20e250599f3c41e6eee5fac12a634` | 28 | `components/context-engine/` |
| https://github.com/meta-hackathon-athena/dreamer-model | `51da8333e24ba88baf615314988798c823fde6f1` | `668152b6c33f948154a18c3665fe8cd6f010e856` | 37 | `components/dreamer-model/` |

These are the merge commits of the M3 release, combining master-repo PRs #1–#3, context-engine PR #1 and dreamer-model PRs #1–#3. The three original repositories were not modified, archived or made read-only, and they keep their full history. To trace a file's history, look it up in the original repository at the path from the table above.

## 2. File mapping

| Source | Destination | Notes |
|---|---|---|
| master-repo `vixy/`, `agent/`, `memory/`, `tests/`, `demo/`, `docs/`, `docker-compose.yml`, `.python-version` | same paths at the root | unchanged, except the docs listed in section 4 |
| master-repo `pyproject.toml`, `uv.lock`, `.gitignore`, `.env.example`, `README.md`, `scripts/` | root | adapted (section 4) |
| context-engine `*` | `components/context-engine/*` | source and tests unchanged; README gained a monorepo note |
| dreamer-model `*` (including Ara's synthetic `dream_output/` sample) | `components/dreamer-model/*` | see section 4 for the 2 code edits |
| — | `Makefile`, `scripts/test.sh`, `scripts/diagnose.sh`, `.github/workflows/ci.yml`, `frontend/README.md`, this file, `docs/reports/VIXY_MONOREPO_VALIDATION.md` | new |

**Not imported:**

| Path | Why |
|---|---|
| master-repo `yolomodelvision/` (`dec.py`, `dec-y.py`, `main.py`, `classes.txt`, `koufu.PNG` 7.7 MB) | Legacy prototype that calls Baseten's paid API with `BASETEN_API_KEY`. It isn't part of the pipeline or the tests; M3 perception is `vixy/perception.py` with local YOLO11n. It's still available in master-repo at the source commit. |
| master-repo `.github/workflows/tests.yml`, `scripts/components.env` | Cross-repository CI needing a `VIXY_COMPONENTS_TOKEN`, and component commit pins. Both are obsolete now the components are in-tree. |
| context-engine and dreamer-model `.github/workflows/tests.yml` | Nested workflows don't run; replaced by the root `ci.yml`. |
| dreamer-model `uv.lock` | A uv workspace has one lock (the root `uv.lock`). |

**Never copied** (all local-only and gitignored): `.env`, API credentials, user videos and audio, transcripts and replay monitors, SQLite databases, FalkorDB data, downloaded model weights, perception caches, logs. The user's local `.env` and recordings stay in the old `master-repo` workspace and were not read into this repository.

## 3. Dependency and packaging changes

- **uv workspace.** The root `pyproject.toml` declares `[tool.uv.workspace] members = ["components/context-engine", "components/dreamer-model"]`, and resolves both component packages with `{ workspace = true }`. They were `{ path = "../context-engine" }` and `{ path = "../dreamer-model" }` before. Both are installed editable from `components/*/src`, so there is one copy of each module. The Python import names (`context_engine`, `prod_dreamer_model`, `agent`, `memory`, `vixy`) didn't change, so no import paths changed.
- **dreamer-model `[tool.uv.sources]`.** `vixy-context-engine = { path = "../context-engine", editable = true }` became `{ workspace = true }`. uv requires workspace members to reference each other that way. It resolves to the same directory.
- **Root dev group.** It now includes `vixy-context-engine[api,dev]`, so Context Engine's API tests (fastapi, httpx) run from the shared environment.
- **`uv.lock`.** It was regenerated from the old lock with `uv lock`. **Every package version that was locked before is unchanged.** The differences are:
  - the two component sources (`../x` → `components/x`)
  - six packages that Context Engine's api/dev extras pull in: `fastapi 0.162.0`, `starlette 1.10.0`, `uvicorn 0.54.0`, `click 8.5.0`, `annotated-doc 0.0.5`, `opentelemetry-api 1.45.2`

  Before, Context Engine's tests resolved these unpinned at test time (`uv run --isolated`); now they are locked.
- **Project name.** It stays `metahackathon`. It's only lock metadata, and renaming wasn't required.
- **Docker.** `docker-compose.yml` is unchanged, including the project name `vixy`. Starting FalkorDB from this repository therefore reuses the **same** container and data volume (`vixy_vixy_falkordb_data`) as the old workspace, so existing graphs (`vixy_demo` etc.) are kept, not migrated or reset.

## 4. Edited files and why

**Code (2 files, both in the Dreamer):**

1. `components/dreamer-model/src/prod_dreamer_model/config.py`. The env-file search list (used by the Dreamer and, through `vixy/config.py`, by the runtime) contained the sibling path `<dreamer-model>/../master-repo/.env`. In the monorepo that path doesn't exist, so the root `.env` would have been ignored. The entry is now the monorepo root `.env`. It's added only when the package sits at `<root>/components/dreamer-model`, so a standalone checkout never reads an unrelated parent `.env`. The order is unchanged: process environment > `VIXY_ENV_FILE` > `DREAMER_ENV_FILE` > repository-root `.env` > package/component `.env` files.
2. `components/dreamer-model/src/prod_dreamer_model/cli.py`. The help text example `--history-db ../master-repo/data/…` became `data/…`, run from the root. The docstring is the only change.

**Packaging and scripts:**

- `pyproject.toml` and `components/dreamer-model/pyproject.toml` (section 3), and `uv.lock`.
- `.gitignore` gained `logs/`, `*.log`, `*.sqlite`, `*.db`, `*.env`, `*.m4a`, `*.mp3`, `*.safetensors`, `node_modules/`, `.expo/`, `.next/`, `venv/`, and tool caches.
- `.env.example`: the header and the "repository-root `.env`" wording. No new keys, and no values beyond the old defaults.
- `scripts/bootstrap.sh` no longer clones or pins sibling repositories. It runs `uv sync --frozen`, creates `.env` from the example, starts FalkorDB and runs the smoke test.
- `scripts/smoke_test.py` now checks that the components import from `components/*` instead of `../*`.
- New: `scripts/test.sh` (test levels), `scripts/diagnose.sh`, `Makefile`, `.github/workflows/ci.yml`.

**Documentation:**

- `README.md` was rewritten.
- `docs/ARCHITECTURE.md` gained a monorepo layout section and a note on where things run.
- `docs/FRONTEND_INTEGRATION.md`: clone/setup and `make` targets, and the app's location in `frontend/`. The API content is unchanged because the API is unchanged.
- `docs/README.md`, `docs/API.md` and `demo/README.md`: paths and the index.
- `components/*/README.md`: a monorepo note and install paths.
- The historical reports, audit and target architecture are **unchanged**. `docs/README.md` explains how their old paths map.

`docstring`/comment mentions of "master-repo" inside the components (e.g. "written by the master-repo runtime") were left as is. They mean the runtime at this repository's root.

## 5. Behavioural differences

- **None** in the Context Engine, the Active Agent, the Dreamer, the runtime or the API. Their source files are byte-identical to the sources, except the two Dreamer edits above. Of those, `cli.py` changes only help text, and `config.py` changes only which `.env` path is found. The regression comparison is in [reports/VIXY_MONOREPO_VALIDATION.md](reports/VIXY_MONOREPO_VALIDATION.md). On the team's real recordings, YOLO and Whisper produced bit-identical caches.
- **Environment files.**
  - The runtime and the standalone Dreamer now read `<repo>/.env`. Before, they read `master-repo/.env` of the sibling layout.
  - A `components/dreamer-model/.env` is still read after it, exactly as before.
- **Context Engine dependency resolution.** Its tests now run against locked fastapi/httpx versions instead of the latest at run time. Standalone use (Python ≥ 3.10, `pip install -e components/context-engine`) is unchanged and tested in CI on Python 3.10.
- **Standalone Dreamer.** Inside the monorepo, `uv` commands run from `components/dreamer-model` use the root workspace environment. Outside the monorepo, the package installs with `uv pip install -e .`, as its README describes.
- **Known hazard (unchanged code, documented).** `python -m vixy demo --fresh` without `--user` uses the user `demo` and deletes its graph. With the default `.env` that is the shared server's preserved `vixy_demo`. All documented commands and `make` targets pass disposable users, and `make demo-data`/`make serve` refuse `demo`.

## 6. Decisions

- **Snapshots instead of history** (security requirement). Provenance is kept by section 1 and by the original repositories.
- **Keep the master-repo root layout.** It avoids moving the runtime and changing its imports and test paths. The components go in `components/` as uv workspace members.
- **Test suites run one per component** (`scripts/test.sh`). All three have a top-level `tests/__init__.py` package, so one pytest session would collide on the package name `tests`.
- **CI** needs no cross-repository token. The audiovisual level (torch, model downloads, real local inference) is an optional manual job; locally it is `make smoke-av`.
- **The original repositories stay as they are.** Whether to archive them or add a "moved to vixy" notice is a team decision.
