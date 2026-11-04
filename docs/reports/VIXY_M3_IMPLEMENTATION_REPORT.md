# Vixy — Milestone 3 implementation report

Date: 2026-11-04. Status: implementation and synthetic audiovisual acceptance verified; M1/M2/M3 branches published and PRs open. The original publication used generated fixtures. The subsequent repair and actual-recording acceptance are documented in §11.

## 1. Outcome and architecture

Normal MP4s with audio now drive the existing integrated backend. The simulator replaces hardware input; it does not substitute scripted intelligence, memory, answers or notifications.

```text
MP4 -> ffprobe inspection
    -> OpenCV decode / PTS sampling -> local YOLO detector
    -> FFmpeg audio extraction -> local Whisper ASR
    -> existing adapters -> validated Context Engine v1 events -> perception cache
    -> deterministic multi-day replay / shared simulated clock
    -> Context Engine SQLite -> per-session Episode Writer -> Active Agent
    -> one FalkorDB graph per user -> actual Dreamer consolidation
    -> retrieval, grounded HTTP /v1/ask, memory-aware decisions
```

Implementation is concentrated in master-repo: `vixy/scenario.py`, `perception.py`, `replay.py`, `fixture.py`, CLI additions, runtime/agent clock entry points, per-session writers and tests. The standalone Active Agent factory now uses the shared Meta key and endpoint, with explicit model opt-in; legacy Baseten fallback is removed. Dreamer changes share environment loading, require explicit online CLI opt-in and prohibit contributor models in unified wearable paths. Context Engine requires no M3 changes; its existing strict schemas and SimulatedClock are reused.

## 2. Documentation preservation

`master-repo/docs/` is canonical. Its index links architecture, graph schema, contracts, API, target architecture, audit and reports. The original M1/M2 reports, target architecture and audit were copied without changing their content. Existing newer M2 architecture/contracts/API/schema files were retained and extended. At initial publication the workspace-root copies were retained. During the subsequent repair they were confirmed byte-identical to the tracked canonical files, then removed to leave one canonical location. All future reports go directly into `docs/reports/`.

The root README links canonical docs and the MP4 workflow. [demo/README.md](../../demo/README.md) provides complete commands, schema examples and limitations. [Synthetic evidence](VIXY_M3_SYNTHETIC_EVIDENCE.json) contains safe generated-fixture results, not private recordings or credentials.

## 3. Models actually executed

| Component | Actual model / package | Main acceptance device |
|---|---|---|
| Object detector | YOLO11n, `yolo11n.pt`, Ultralytics 8.4.202 | CPU |
| ASR | Whisper `base.en`, openai-whisper 20250625 | CPU |
| Inference runtime | PyTorch 2.15.0; OpenCV 5.0.0.93 | Apple Silicon host |
| Additional device smoke check | Actual YOLO prediction on Apple MPS with person detection asserted | MPS passed |

Detector checkpoint SHA-256: `0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1`.

ASR checkpoint SHA-256: `25a8566e1d0c1e2231d1c762132cd20e0f96a85d16145c3a00adf5d1ac670ead`.

`uv.lock` pins the resolved environment; local inference dependencies are an optional `perception` extra. Ultralytics analytics synchronization is disabled. Model weights are downloaded into ignored outputs, and no per-frame paid VLM is used. Ultralytics is distributed under its [AGPL license](https://github.com/ultralytics/ultralytics/blob/main/LICENSE); Whisper uses its [MIT license](https://github.com/openai/whisper/blob/main/LICENSE). M4 distribution should account for the selected dependencies' licenses.

## 4. Video and audio processing

- FFprobe inspects streams, codecs, dimensions, finite duration, stream offsets and recording-time metadata. Corrupt/missing/unsupported files fail with nonzero CLI status before graph mutation.
- OpenCV sequentially decodes actual frames and samples using presentation timestamps at configurable FPS (default 1). YOLO returns classes, actual counts and per-instance confidences. Captions only list detections; no object detection means “No supported objects detected,” not an assertion that the scene is physically empty.
- Stable frame IDs include the cache identity and sample index; frame references include source video SHA-256 and media offset. Full frames are not stored as graph nodes.
- FFmpeg extracts the first audio stream to temporary mono 16 kHz PCM. Audio stream offset relative to the video/container timeline is added to ASR timestamps. Temporary extracted audio is removed after processing.
- Whisper runs locally with explicit/detected language and timestamped final segments. The original transcript, log probability and no-speech score are preserved. Uncalibrated scores are not presented as a confidence probability. Digital silence skips ASR; no-audio and no-speech inputs produce explicit warnings.
- Unknown speakers remain unknown. Only a segment wholly inside an explicit annotation receives speaker/wearer identity. No diarization, speaker-name guessing, automatic wearer inference or invented GPS is implemented.

## 5. Scenario, cache and clock

The committed safe example is `demo/scenario.example.json`. Private manifests and MP4s belong under ignored `demo/inputs/<scenario>/`. Recordings specify user/session IDs, video path, explicit-offset simulated start, optional real `recorded_at`, optional GPS fixes and speaker intervals, and optional `dream_after`. Scenario fields include playback speed, timer interval, optional explicit dream boundaries and final simulated time. GPS uses only supplied recent fixes; no interpolation or geocoding.

Real capture time is unknown unless supplied; it is never inferred from simulated time. File creation metadata is reported separately. Source provenance preserves capture offsets/times, simulated start, user/session, video hash, model IDs/versions/checkpoint hashes, per-instance detections and source event IDs.

Process mode saves normalized observations, not answers, memories or notifications. Cache identity includes source bytes, user/recording/session/time/annotations, preprocessing version, model configuration, checkpoint contents and package versions. An events digest detects accidental corruption. Replacing weights, changing media or changing preprocessing/configuration requires a new matching cache. Model weights and the same locked environment remain available for fingerprint checks, but replay loads no inference models.

Replay globally orders video at capture time and final audio at segment end. It advances Context Engine's SimulatedClock, Active Agent time and Dreamer boundary time. CE ingestion timestamps are asserted equal to simulated availability time in acceptance tests. Timer ticks are operational inputs; no fake camera frames are injected to drive reminders. Default one-minute virtual ticks preserve checks across recording gaps; they do not sleep through a real day. `--speed` paces media within recordings; `0` means fast virtual time.

Per-session Episode Writers preserve correct attribution and restart checkpoints. Notes can link DURING only to Visits in their own session, including overlapping sessions. Stable event IDs, CE hashes, the runtime ledger and boundary/timer checkpoints make repeat replay idempotent. Pause-file control and restarting the same replay command support pause/resume. Graph audit creation/update times remain wall-clock metadata, distinct from event time.

`--fresh`/`reset-replay` requires a named `m3_*` or `test_*` user, `disposable:true`, the `vixy` graph prefix and matching ownership for existing state. It deletes only that user's graph and exact replay SQLite files. It never unlinks an embedded RDB that might hold other graphs, deletes a Docker volume or resets `vixy_demo`.

## 6. CLI and input workflow

```bash
cd master-repo
uv sync --frozen --extra perception
# Install FFmpeg/ffprobe on PATH; on macOS: brew install ffmpeg.
mkdir -p demo/inputs/scenario_01
cp demo/scenario.example.json demo/inputs/scenario_01/scenario.json
# Put your own day1.mp4 and day2.mp4 here; edit times and annotations in scenario.json.
uv run --extra perception python -m vixy process --scenario demo/inputs/scenario_01/scenario.json
uv run --extra perception python -m vixy replay --scenario demo/inputs/scenario_01/scenario.json --fresh
uv run --extra perception python -m vixy replay --scenario demo/inputs/scenario_01/scenario.json
uv run --extra perception python -m vixy --user m3_scenario_01 --state-dir demo/outputs/m3_scenario_01/state dream
uv run --extra perception python -m vixy --user m3_scenario_01 --state-dir demo/outputs/m3_scenario_01/state decisions --limit 20
uv run --extra perception python -m vixy --user m3_scenario_01 --state-dir demo/outputs/m3_scenario_01/state memories --about Alex
uv run --extra perception python -m vixy --user m3_scenario_01 --state-dir demo/outputs/m3_scenario_01/state ask "What do you remember about Alex?"
uv run --extra perception python -m vixy reset-replay --scenario demo/inputs/scenario_01/scenario.json
```

Both one-day and multi-day replay use the same command; recording boundaries control consolidation. Use the same `FALKORDB_URL`, user and state directory for inspection. Default output: `demo/outputs/<user>/`. The existing `serve` command exposes `/v1/ask`, memories, decisions, provenance and status. Structured monitor output is appended to `replay.jsonl` and the latest results to `summary.json`. See the demo README for pause, language/device configuration and generated fixtures.

## 7. Actual tests and evidence

At original M3 implementation time, no user MP4 existed. The generated acceptance fixture contains two 34-second MP4s: a repeated public Ultralytics bus photo with locally synthesized speech. It is a synthetic composite, not actual wearable footage or a natural conversation. No transcript is passed to ASR and no backend answer is scripted.

| Stage / suite | Actual result |
|---|---|
| M1/M2 baseline before M3 | master 94, Dreamer 62, Context Engine 53 passed |
| Full master M1/M2/M3 suite, embedded + Docker + genuine model edge tests | 106 passed |
| Post-review changed-path regression (checkpoint replacement and overlapping-session evidence) | 49 passed |
| Post-review real-media + embedded/Docker replay acceptance | 3 passed |
| Final Dreamer suite including key-alone offline regression | 63 passed |
| Context Engine suite | 53 passed |
| Standalone shared-key factory plus agent checks | 36 passed |
| Original Active Agent eval | 14/14 passed |
| Original full-day agent simulator | exit 0; 30,420 frames, 7 delivered; 2 FakeLLM calls, zero paid inference |
| Memory demo, Context Engine demo | exit 0 on scratch storage |
| Legacy Dreamer 14-day offline demo | exit 0; 10 patterns, 11 memories |
| M1/M2 integrated demos | pass in the full regression suite, embedded and Docker |

Initial sandbox-only embedded runs could not create sockets and were interrupted; authorized runs with local socket access passed. Tests use isolated scratch databases/users. No cloud deployment occurred.

Measured main acceptance:

1. Real YOLO inference sampled 34 frames per video (68 total), detecting bus/person classes with counts and scores.
2. Real Whisper inference produced day 1 “Alex is allergic to peanuts.” and day 2 “I am buying peanuts for Alex.” The day-1 speaker is unknown; the synthetic day-2 wearer annotation is explicit.
3. All 70 normalized events passed CE, Episode Writer and Agent. The graph contains 2 Notes, 2 Visits, 2 Days, 1 Person, 1 LongTermMemory and 2 DreamRuns; 4 AgentLog nodes.
4. Day 1's Dreamer wrote `ltm-claim-227fa54c192fdd53`, with SUPPORTED_BY to `note_48254ae2271e1507`, itself backed by CE event `asr:day1:b3f86815690d8927b1ac1862-0`. Text/hash checks pass.
5. Day 2 delivered `dec_5bf560e61806`, triggered by `asr:day2:277e1fae9d56fa9b4a96ca72-0`, using the prior day's memory. Its message preserves attribution: “Someone mentioned yesterday that Alex is allergic to peanuts. I haven't verified it. You may want to check with Alex before buying.”
6. Actual HTTP `/v1/ask` answered from that memory and cited the original Note with `verified:true`. The answer says the claim is reported and unverified.
7. Repeat replay returned 70 duplicates, with unchanged graph node and relationship counts. Tests prohibit perception model initialization during replay. Restart, alternate playback speed, pause-file controls and restored simulated clock are tested.
8. Additional actual-model tests passed for silent audio, no audio, black frames/empty detections and delayed audio stream alignment. Actual MPS prediction passed when available.

Safe details and hashes are in [VIXY_M3_SYNTHETIC_EVIDENCE.json](VIXY_M3_SYNTHETIC_EVIDENCE.json). Full local media, caches and transcript logs remain in ignored `demo/outputs/` and `demo/inputs/`.

Measured final CPU inference time (excludes downloads and model loading): YOLO 1.238 s / 34 frames on day 1 and 0.654 s / 34 frames on day 2; Whisper 0.375 s and 0.261 s respectively. The earlier Docker two-day replay completed in 0.826 s (virtual day gap, no perception). These are small-fixture measurements, not throughput guarantees for real recordings.

## 8. Meta readiness and security

The local key is present and shared safely across runtime/Active Agent model settings, the standalone Active Agent interpreter factory, and Dreamer. Explicit process environment values, including empty offline overrides, win. One read-only `GET https://api.meta.ai/v1/models` returned HTTP 200 and listed `muse-spark-1.3` among 9 model IDs. No inference, tool call, private media upload or paid API call occurred. Generation/tool-call behavior remains unverified live. The metadata endpoint is documented by [Meta](https://dev.meta.ai/docs/api-reference/models).

Unified wearable runtime, extractor and online Dreamer paths refuse contributor models even if a legacy synthetic-only override is present. Frequent perception is local. Installing a key does not enable inference: the runtime stays deterministic and standalone Dreamer requires explicit `--online` or `--model-extraction` flags.

The user confirmed historical Baseten-key revocation on 2026-11-04 before publication. This is user attestation, not a provider audit; the old key was not used/tested. Git history was preserved. `.env` is local/untracked; media, databases, virtual environments, model weights and generated outputs are ignored. Publication scans check exact known credential values without displaying them and check docs for private absolute paths.

Read-only preservation check after testing: `vixy_demo` still has 91 nodes and schema `vixy.graph.v1`; Docker still mounts `vixy_vixy_falkordb_data`. No reset, migration or deletion was applied to either.

## 9. Git publication and dependencies

GitHub confirmed the repositories moved from `meta-hackathon-vixy` to `meta-hackathon-athena`; existing origin URLs redirect successfully. M1/M2 pushes and the following PRs are confirmed. They remain open and unmerged.

| Repo | M1 SHA / PR | M2 SHA / PR |
|---|---|---|
| master-repo | `a3991569450e7c5194a3af305c42fbb809b42c4f` — [PR 1](https://github.com/meta-hackathon-athena/master-repo/pull/1) | `021c0bb36f73e70ff3ff41df72fd0cb02aef7acc` — [PR 2](https://github.com/meta-hackathon-athena/master-repo/pull/2) |
| context-engine | `685413299a69e3ad694c847dfb56c6bb0148cb68` — [PR 1](https://github.com/meta-hackathon-athena/context-engine/pull/1) | no M2 change/branch required |
| dreamer-model | `68ce46e4135427ec444b732bee5aae322b1ff193` — [PR 1](https://github.com/meta-hackathon-athena/dreamer-model/pull/1) | `d596f6ccf21dea74d91113d0d340d47c507e44ba` — [PR 2](https://github.com/meta-hackathon-athena/dreamer-model/pull/2) |

M1 PRs target `main`. Master M1 intentionally includes Active Agent commits `f0304ff` and `32e0676`, ancestors required by the integrated runtime; the PR explains this. M2 PRs target their M1 branches, avoiding repeated diffs. M3 branches were created from the committed M2 state in master-repo and Dreamer only; Context Engine remains unchanged on M1. Both M3 PRs target their corresponding M2 branches. No merge, rebase, history rewrite or force-push is authorized or performed.

GitHub's M1/M2 PR check rollups are empty, and these repos have no tracked CI workflows. Local passing tests are reported separately; no remote CI pass is claimed. The GitHub connector returned 404 for both organization names; authenticated Git/gh successfully handled publication.

M3 publication confirmed by GitHub:

| Repo | M3 implementation commit | PR / base |
|---|---|---|
| master-repo | `efd959c288857b6cee2b7c752d1bd2dbf344f3a8` | [PR 3](https://github.com/meta-hackathon-athena/master-repo/pull/3) → `integration/m2-intelligence` |
| dreamer-model | `95bb213352d881143e60ddaa3c7a7f4d76b2f4e5` | [PR 3](https://github.com/meta-hackathon-athena/dreamer-model/pull/3) → `integration/m2-intelligence` |

The final publication addendum only updates this report/evidence. Its containing commit is discoverable via `git log -1 -- docs/reports/VIXY_M3_IMPLEMENTATION_REPORT.md`; the branch tip and exact remote SHA are also exposed by the PR. Both M3 PRs are OPEN, unmerged, and report no CI checks (`statusCheckRollup: []`). All substantive implementation commits above were confirmed as matching remote heads before the addendum. No remote CI success is claimed.

Final validation: master-repo **106 passed in 53.03 s**, Dreamer **63 passed in 4.32 s**, Context Engine **53 passed**. The dependency lock check passed; `uv sync --frozen --extra perception --offline --dry-run` reported no changes. Missing-MP4 CLI handling returned a clear error without graph mutation. Final credential/media/private-path scans passed.

## 10. Limitations and M4 work

- The two supplied real recordings are now accepted (§11). Broader evaluation of noise, accents, languages and natural multi-speaker speech remains necessary.
- No automatic diarization or identity recognition. Only explicit grounded annotations attribute speech. ASR timing is estimated at segment level and can include adjacent silence.
- Detection is COCO-class object perception, not semantic scene/activity/relationship understanding. Existing backend heuristics remain inferences; there is no selective VLM implementation in M3.
- ASR loads each extracted recording's audio in memory; caches/scenario events are also materialized. Long recordings need streaming/chunking and bounded output. No concurrent replay for the same state/user is supported.
- The cache is trusted local data with corruption detection, not a signed portable artifact. The original source and checkpoint files are needed to validate its fingerprint. Model/library changes invalidate it.
- Replay pacing is best-effort; the CLI does not render audiovisual playback. Timer granularity is configurable (default 60 seconds). Operational audit timestamps use wall time. Some agent visual hysteresis remains in-memory, as documented in M1.
- Dreamer still uses the M2 deterministic English extraction rules by default. Meta generation and forced-tool-call compatibility have not been exercised live; paid use requires separate authorization.
- The HTTP API remains a local development API without authentication. No mobile app, full frontend, real notification delivery or cloud deployment was built.

M4 can consume existing `/v1` memory/ask/decisions/timeline/provenance routes and the structured monitor contract. Remaining work: mobile UI and audiovisual playback, live activity transport, alerts and explanations, user-confirmed identity/annotation controls, authenticated sessions, secure data handling, user-recording evaluation and packaging/license decisions.


## 11. Post-publication repair: actual MP4 bounds and sole canonical docs

**Verified 2026-11-04.** This section supersedes the earlier pending real-recording status. It contains aggregate results and timing diagnostics only. Private recordings, complete transcripts, caches, diagnostics and replay results remain in ignored local directories. No paid model or media upload was used.

### Reproduction and exact cause

The unchanged local `scenario_01/scenario.json` reproduced the process error after day 1's existing 49-event cache. The replay failure was downstream: day 2 never completed processing, so its cache did not exist.

Both MP4s have zero stream start offset. This failure was **not** an offset mistake or merely floating-point rounding. Raw local Whisper output and actual decoded PCM established:

| Timing measurement | Day 1 | Day 2 |
|---|---|---|
| Video/container duration | 42.623333 s | 67.640000 s |
| Declared AAC stream duration | 42.605646 s | 67.577324 s |
| Decoded 16 kHz PCM duration | 42.6056875 s | 67.5773125 s |
| Whisper raw segments | 20 | 40 |

Day 2's final Whisper decode begins at `seek=5200` (52 seconds) and pads the remaining audio to its model window. Its timestamp-token output includes repeated tail segments through 80 seconds. The first fatal segment is **ID 28, start 68.0 s, end 69.0 s**. The old normalizer computes `start=68.0` and `end=min(69.0, 67.64)=67.64`; the resulting inverted interval triggers `ASR returned an invalid media interval`. Entirely padded intervals have no real audio support and must not be relocated into the recording. Segment 27 (67–68 s) also crosses the actual PCM boundary by far more than small decoder drift.

A second defect hid valid speech: Vixy rejected every segment with `no_speech_prob > 0.6`, even when Whisper's combined rule retained it because token log probability was good. This discarded 14 day-1 segments and 20 day-2 segments. Keeping Whisper's own confidence decision restores that speech. A per-interval waveform-energy check rejects near-digital silence, including short hallucinations in the generated fixture, without reintroducing the incorrect no-speech-only gate. It does not claim that ASR is infallible in background noise.

### Correction and regression coverage

`vixy/perception.py` now:

- Measures extracted PCM sample count/rate and validates ASR against actual decoded audio support before adding stream offsets.
- Rejects wholly out-of-support segments with explicit recording/segment warnings. Boundary crossings are clipped only within **50 ms**; larger crossings are rejected rather than stretched or shifted into valid media. Reversed, non-finite and non-numeric intervals fail with useful IDs.
- Preserves raw timestamps, normalization status and interval RMS in event provenance. Original ASR text is not rewritten.
- Checks proposed intervals for near-digital silence with the same `1e-5` RMS floor already used for whole-file silence. Empty decoded tracks skip ASR.
- Uses actual stream-relative ends for MP4 edit-list timing, and checks video truncation against the video stream rather than a longer audio/container duration.
- Advances preprocessing to **vixy.perception.v2**, so old caches cannot be silently reused. Existing recordings and earlier cache files are preserved.

`tests/test_perception_intervals.py` adds 12 checks covering the exact padded-window interval class, conservative boundary rejection, small drift, malformed/out-of-range timestamps, short AAC audio, trailing digital silence, positive/negative MP4 edit offsets and an audio track edited entirely before time zero. Model results are stubbed only in these decoder/normalizer boundary tests. Genuine YOLO/Whisper perception and the supplied recordings are separately exercised end to end.

`tests/test_m3_real_recordings.py` adds opt-in private-recording acceptance on embedded and disposable Docker storage. It verifies cache bounds, actual CE ingestion, Note text/hash provenance, Dreamer runs, live HTTP Ask evidence and unchanged graph counts on duplicate replay. It selects a query term from observed text instead of prescribing a final answer, memory or notification.

### Actual perception and replay results

The original process command now completes for both days and creates matching v2 caches:

| Result | Day 1 | Day 2 |
|---|---|---|
| Video observations / genuine YOLO calls | 43 | 68 |
| Accepted final ASR segments | 20 | 27 |
| Total normalized events | **63** | **95** |
| Rejected ASR segments | 0 | **13** |

Day 2 rejects IDs 28–39 as wholly outside decoded audio (12 segments) and ID 27 as an excessive boundary crossing (1 segment). Warnings identify each rejected ID and raw interval. No speech is invented to fill discarded intervals. Detected object classes are model predictions, not confirmed scene or activity labels. The model choices remain YOLO11n / Ultralytics 8.4.202 and Whisper base.en / openai-whisper 20250625 on CPU.

The untouched media and original manifest were reused through an ignored derivative manifest with disposable user **m3_real_repair_20261104** and separate sessions/state. Full replay succeeded on both embedded FalkorDB and a temporary Docker server on port 6391. That container had no mounts and never accessed `vixy_vixy_falkordb_data`; it was used instead of the preserved server for all server regression variants.

Actual backend results:

- **158 events** ingested and fully processed; **47 observed Notes**, **47 AgentLog decisions**, **2 Days** and **2 DreamRuns** persisted. Note-to-original-event text and SHA-256 checks pass.
- **0 Visits**: visual segmentation candidates on day 1 lasted at most 28.025 s, and on day 2 at most 29.007 s, below the existing 30-second minimum. Raw video observations remain preserved in Context Engine. No episode was fabricated or threshold changed to force a result.
- **0 LongTermMemory/Pattern nodes**: deterministic Dreamer ran on 20 then 47 Notes and found no eligible durable structured claim. Observed Notes remain queryable; durable memory creation is not forced.
- **47 NO_ACTION decisions**: 46 utterances were non-actionable; one possible plan was explicitly withheld because its speaker was unverified. **0 notifications and 0 commitments** were created. All speakers remain unknown because no wearer annotations were supplied.
- Actual HTTP `/v1/ask` returned grounded keyword results with verified source events. A request asserting “I said” correctly returned insufficient evidence when speaker identity could not be established. The local keyword inspection returns 3 verified Note sources across the two simulated days. Full private answers stay in ignored output files.
- Repeating replay produced **158 duplicates**, with identical graph node and relationship counts. Perception inference is prohibited during these replay tests.

Final verification: **master-repo 120 passed in 58.88 s**, **Dreamer 63 passed**, **Context Engine 53 passed**. This includes original M1/M2/M3 tests, generated audiovisual acceptance, actual-recording acceptance on both backends, original agent scenario checks, local-model edge tests and the new interval/silence regressions. An intermediate run exposed the synthetic silence hallucination after removing the score-only gate; the waveform guard corrected it and the complete final suite passed.

### Documentation cleanup and publication

The canonical files were already present and tracked in the M3 branch. Each of the four workspace-root originals was compared byte-for-byte with both its canonical working copy and its committed `HEAD` blob before removal. They contained no unique information. The redundant root files were then removed from the workspace; historical M1/M2 report contents remain unchanged. The architecture reference and documentation index now point only to canonical locations:

```text
master-repo/docs/README.md
master-repo/docs/ARCHITECTURE.md
master-repo/docs/GRAPH_SCHEMA.md
master-repo/docs/CONTRACTS.md
master-repo/docs/API.md
master-repo/docs/VIXY_TARGET_ARCHITECTURE_EN.md
master-repo/docs/VIXY_SYSTEM_AUDIT.md
master-repo/docs/reports/VIXY_M1_IMPLEMENTATION_REPORT.md
master-repo/docs/reports/VIXY_M2_IMPLEMENTATION_REPORT.md
master-repo/docs/reports/VIXY_M3_IMPLEMENTATION_REPORT.md
```

The workspace root is not a Git repository, so removing its redundant copies is a local workspace cleanup; their canonical versions were already committed in master-repo. All reference/index/report fixes are committed on the existing `integration/m3-perception` branch and published to the same [master-repo PR #3](https://github.com/meta-hackathon-athena/master-repo/pull/3). The repair commit is the containing report commit, available via `git log -1 -- docs/reports/VIXY_M3_IMPLEMENTATION_REPORT.md`. No merge or history rewrite is performed. No M4 work is included.

To inspect the verified local embedded results:

```bash
cd master-repo
FALKORDB_URL='' uv run --extra perception python -m vixy \
  --user m3_real_repair_20261104 \
  --state-dir demo/outputs/m3_real_repair_20261104/state \
  timeline 2026-11-02
```

Use the same prefix with `decisions --limit 100`, `memories`, or a keyword `ask` query. The private `verified-keyword-ask.json` and `verified-ask-embedded.json` outputs contain the already-checked answers and source traces.
