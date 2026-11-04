# Vixy monorepo validation (2026-11-04)

This report covers the regression validation of `meta-hackathon-athena/vixy` against the M3 baseline (the three original repositories at the `main` commits in [MONOREPO_MIGRATION.md](../MONOREPO_MIGRATION.md)).

## Conditions

- **Paid calls:** none. Model keys were blanked in every run (`MODEL_API_KEY=` `BASETEN_API_KEY=`).
- **Meta Muse:** no inference call was made. All Muse paths remain **unverified live**, and the tests use `FakeStructuredModel`.
- **Test users:** disposable only (`it_*`, `smoke_*`, `cmp_*`, `val_*`, `m3_av_*`, plus the disposable real-recording user `m3_real_repair_20261104` on a scratch copy). Their graphs were deleted afterwards.
- **Preserved graph:** `vixy_demo` had 91 nodes / 179 relationships before and after.
- **Machine:** macOS 26.5.2 arm64, uv 0.12.18, Python 3.12.14. FalkorDB 6.0.3 on Redis 8.10.2: the pinned `docker-compose.yml` image on `localhost:6390`.
- **Code under test:** the validated tree is the initial commit's content, cloned into an empty directory with **no sibling repositories**. The published commit adds only this report and two documentation clarifications (the CSRF note and a `FALKORDB_URL` hint).

## 1. Fresh clone install

`git clone … && make install-perception` (= `scripts/bootstrap.sh --perception`) in an empty directory:

- `uv sync --frozen` resolved every component from `components/`.
- `.env` was created from the example, and FalkorDB was healthy.
- The **smoke test passed 18/18**:
  - imports resolve to `components/context-engine` and `components/dreamer-model`;
  - the M2 pipeline passed 18/18;
  - the `/v1` HTTP checks passed: health, notifications, decision/memory evidence, ask, timeline;
  - the MP4 path ran: fixture → YOLO + Whisper → replay of 70 observations → 1 memory → ask answered, with 0 paid calls.
- Wall time: 57 s, with a warm uv cache.

## 2. Regression results against the M3 baseline

| Suite | M3 baseline (original repos, final `main`) | Monorepo | Result |
|---|---|---|---|
| Context Engine tests | 53 passed | 53 passed (workspace env); 53 passed standalone on Python 3.10 | equal |
| Dreamer tests | 63 passed | 63 passed | equal |
| Runtime/agent/memory, embedded | n/a (run together with the server variants) | 100 passed, 20 skipped (the server and MP4 opt-ins) | — |
| Runtime incl. FalkorDB server variants | 118 passed, 2 skipped (with all MP4 opt-ins) | 115 passed, 5 skipped (integration level, no MP4 fixture) + 24 passed (MP4 level: `test_m3`, acceptance embedded+server, media, intervals) | all pass, nothing new skipped¹ |
| Active Agent evaluation | 14/14 | 14/14; output identical to the baseline | equal |
| M1 demo (`vixy demo --fresh`) | 9/9 | 9/9; output identical² | equal |
| M2 demo (`vixy demo-m2 --fresh`) | 18/18 | 18/18; output identical² | equal |
| M3 audiovisual acceptance (generated MP4, local YOLO11n + Whisper) | passed (embedded + server) | passed (embedded + server) | equal |
| Backend HTTP API (`/v1` smoke + acceptance HTTP ask) | 11/11 + 18/18 with MP4 | 11/11 + 18/18 with MP4 | equal |
| Duplicate-replay idempotency (M1/M2 demo checks, M3 acceptance, real-recording test) | pass | pass | equal |
| Real recordings: perception output | 2 caches, 63 + 95 events | same cache keys; **bit-identical** event digests and warnings | identical |
| Real recordings: acceptance (`test_m3_real_recordings`) | 1 passed, 1 skipped (embedded only) | 2 passed (embedded **and** server) | equal or better |
| Real recordings: outcome | 158 events, 47 Notes, 2 Dreamer runs, 0 memories, 47 `NO_ACTION` | identical | identical |

¹ The 5 integration-level skips are the opt-in MP4/real-recording tests, which the MP4 level and the real-recording run execute.

² For outputs marked identical, the M1/M2 demos and the agent evaluation were run from the original master-repo checkout (`b88542b`) and from the monorepo, with the same disposable user ids. The outputs were compared after normalizing temp paths and timings. M2 and the evaluation match line for line. M1 differs only in the order of keys in label-count dictionaries returned by FalkorDB; the counts are equal.

## 3. Evidence-backed retrieval

These checks passed in the monorepo:

- `GET /v1/memories/{id}`: the evidence's `original.matches` is true, i.e. the Note's text and SHA-256 equal the Context Engine event.
- `GET /v1/decisions/{id}`: it returns the supporting memories with their evidence.
- `POST /v1/ask`: the answer cites `verified: true` sources.
- Real recordings: the actual HTTP `/v1/ask` cites original Notes, each verified against Context Engine.

## 4. Commands (all from the repository root)

| Purpose | Command | Result |
|---|---|---|
| install | `make install` / `make install-perception` | 18/18 smoke checks |
| unit | `make test` | 3 suites pass |
| integration | `make test-integration` | 5 steps pass |
| M3 audiovisual | `make smoke-av` | 4 steps pass |
| everything | `make test-all` | see section 5 |
| serve + app contract | `make demo-data VIXY_USER=val_app && make serve VIXY_USER=val_app` | health OK; the JS snippet from FRONTEND_INTEGRATION §12 prints `vixy_val_app 3 answered 1`; `POST /v1/events/audio` returns `processed` |
| standalone Dreamer CLI | `uv run python -m prod_dreamer_model.cli --layout unified --user val_app dream --offline --history-db data/val_app/vixy_events.sqlite3` (with `FALKORDB_URL`) | 6 long-term memories |
| guard | `make serve VIXY_USER=demo` | refused |

## 5. `make test-all` on the fresh clone

`make test-all`, run in the clean clone of the initial commit with no sibling repositories, exited 0. All 13 steps passed:

| Level | Step | Result |
|---|---|---|
| unit | context-engine tests | 53 passed |
| unit | dreamer-model tests | 63 passed |
| unit | runtime, agent and memory tests (embedded) | 100 passed, 20 skipped (server/MP4 opt-ins) |
| integration | runtime tests incl. shared-server variants | 115 passed, 5 skipped (MP4/real-recording opt-ins) |
| integration | Active Agent scenarios | 14/14 |
| integration | M1 demo, disposable user `it_m1_*` | 9/9 checks |
| integration | M2 demo, disposable user `it_m2_*` | 18/18 checks |
| integration | HTTP `/v1` smoke test | 11/11 |
| av | generate synthetic MP4 fixture (public image + macOS `say`) | pass |
| av | process MP4 with local YOLO11n + Whisper `base.en` | pass |
| av | M3 acceptance (embedded + server), real-model media edge cases, perception intervals, `test_m3` | 24 passed |
| av | MP4 replay smoke test | 18/18 |

Afterwards only `vixy_demo` and `vixy_m3_validation_20261104` remained on the server, i.e. the graphs that existed before.

## 6. Security checks before publishing

These ran on the exact staged tree (154 files):

- **gitleaks v8.24.0** (`dir` mode) reported 2 findings. Both are false positives: the `cache_key` fields in `docs/reports/VIXY_M3_SYNTHETIC_EVIDENCE.json` are 64-hex SHA-256 cache-identity digests (`key = digest(identity)` in `vixy/perception.py`).
- **Known real key values** (the leaked Baseten key and the local `MODEL_API_KEY`) were compared without printing them: 0 occurrences.
- **Real-recording transcripts:** 0 of 28 transcript sentences (≥ 20 characters) appear in any file.
- **Private file types:** no media, audio, image, SQLite/RDB/DB, model weight or `.env` files, and no file larger than 500 KB.
- **Token patterns:** no OpenAI-, GitHub- or AWS-style tokens or private keys.
- **History:** none was imported, so the history-only Baseten credential can't be present.

## 7. Known limitations (unchanged from M3)

- The `/v1` API has no authentication and no CSRF/origin/host checks. See FRONTEND_INTEGRATION §10; a fix needs its own behaviour-changing PR.
- Real recordings produce no long-term memories and no proactive notifications. Memory-aware behaviour is shown on synthetic data only.
- Meta Muse generation, tool calling and paid usage remain untested live.
- **Slow pytest exit with the embedded graph.** Processes that open embedded falkordblite instances take about 30 s to several minutes to exit after the tests finish, while the redislite servers shut down. Measured on `tests/test_store.py`, 8 tests reported in 1.1 s: wall time was 34 s in the original master-repo and 31 s in the monorepo. So the delay is pre-existing, not caused by the migration, and it is why `make test` takes several minutes for about 1 minute of tests.
- The M3 audiovisual level was validated locally only, not by GitHub Actions; see section 8.

## 8. GitHub Actions

The repository was created during GitHub's 2026-11-04 incident, and GitHub never processed its first pushes: no events, no workflow registered. Opening PR #1 (`fix/register-github-actions`) registered the `ci` workflow.

- **First run (41833706809): failure.** It failed on `tests/test_m3.py::test_corrupt_media_and_reset_protection`. The runner had no `ffprobe`, so the error was `required executable missing` instead of `ffprobe failed`. This was a CI environment gap, not a code bug; the `tests` job now installs `ffmpeg`.
- **Second run (41834751464): success.** In this run the unit level also ran the server variants, because the job sets `VIXY_TEST_FALKORDB_URL`. `scripts/test.sh unit` now clears that variable, so the unit level is embedded-only everywhere, matching the local runs, and the server variants run only in the integration level.
- **Latest successful run on the PR (41836742363, commit `0b2bc70`): success.** The following commit only removed a temporary comment from `ci.yml` and updated this section. The CI run on `main` after the merge is recorded in the PR #1 merge notes.

| Job (run 41836742363) | Result |
|---|---|
| unit, embedded only | context-engine 53 passed; dreamer-model 63 passed; runtime/agent/memory 94 passed, 26 skipped |
| integration, FalkorDB service container | runtime incl. shared-server variants 109 passed, 11 skipped; Active Agent 14/14; M1 demo 9/9; M2 demo 18/18; HTTP `/v1` smoke 11/11 |
| context-engine standalone, Python 3.10 | 53 passed |
| M3 audiovisual | not run on GitHub; optional manual job, see below |

**CI skips.** The CI environment has no perception extra (torch, Ultralytics, Whisper, OpenCV), no processed MP4 fixture and no private recordings. The skipped tests are the perception, MP4 acceptance, media and real-recording tests. All of them ran and passed **locally** (sections 2 and 5).

**Audiovisual: validated locally only.** The M3 audiovisual level (MP4 → local YOLO11n + Whisper → replay acceptance, media edge cases, MP4 replay smoke) was validated **locally on macOS arm64** with `make smoke-av` / `make test-all`, not by GitHub Actions. The CI job `M3 audiovisual (optional, real local models)` exists but runs only on demand (`gh workflow run ci.yml -f audiovisual=true`), and it hasn't been run yet. On Linux it would synthesize speech with `espeak` instead of macOS `say`, so its results may differ from the local fixture.
