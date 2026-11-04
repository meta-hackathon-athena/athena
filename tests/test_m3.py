"""Manifest, provenance, local decoding, cache and clock failure boundaries."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from vixy.config import VixyConfig
from vixy.perception import _normalize, cache_identity, inspect_video, load_cache
from vixy.replay import reset_disposable
from vixy.scenario import Recording, Scenario, digest, load_scenario


def scenario(**kw):
    return Scenario.model_validate({"user_id": "m3_unit", "recordings": [{
        "id": "day1", "video": "day1.mp4", "session_id": "session1",
        "simulated_start": "2026-11-02T18:00:00+08:00"}], **kw})


def test_manifest_rejects_naive_time_overlap_invalid_speed_and_unknown_fields():
    valid = scenario().model_dump(mode="json")
    for mutation in [dict(playback_speed=-1), dict(playback_speed=float("nan")), dict(unrecognized=True)]:
        with pytest.raises(ValidationError):
            Scenario.model_validate({**valid, **mutation})
    rec = valid["recordings"][0]
    with pytest.raises(ValidationError):
        Recording.model_validate({**rec, "simulated_start": "2026-11-02T18:00:00"})
    with pytest.raises(ValidationError):
        Recording.model_validate({**rec, "speakers": [{"start": 0, "end": 5}, {"start": 4, "end": 8}]})


def test_normalized_timing_unknown_speakers_and_explicit_annotations():
    s = scenario()
    r = s.recordings[0]
    raw = {"segment_id": "seg1", "text": " Hello there. ", "language": "en"}
    row = _normalize(s, r, "cache", "audio", 2, 5, raw, {"source_sha256": "a" * 64})
    event = row["event"]
    assert event["start_time"] == "2026-11-02T10:00:02Z"
    assert row["available_at"] == "2026-11-02T10:00:05+00:00"
    assert event["speaker_id"] is None and "speaker_is_wearer" not in event["source"]
    assert event["source"]["capture_time"] is None and event["location"] is None
    r = Recording.model_validate({**r.model_dump(), "recorded_at": "2025-01-01T00:00:00Z",
        "speakers": [{"start": 1, "end": 6, "speaker_id": "annotated", "speaker_is_wearer": True}]})
    known = _normalize(s, r, "cache", "audio", 2, 5, raw, {"source_sha256": "a" * 64})["event"]
    assert known["source"]["speaker_is_wearer"] is True and known["speaker_id"] == "annotated"
    assert known["source"]["capture_time"] == "2025-01-01T00:00:02+00:00"
    crossing = _normalize(s, r, "cache", "audio", 2, 7, raw, {"source_sha256": "a" * 64})["event"]
    assert crossing["speaker_id"] is None


def test_gps_only_uses_real_recent_fixes_and_detector_counts_survive():
    s = scenario()
    r = Recording.model_validate({**s.recordings[0].model_dump(), "gps": [
        {"offset_s": 2, "latitude": 1.3, "longitude": 103.7}], "gps_max_age_s": 3})
    raw = {"frame_id": "frame1", "classes": [{"class": "person", "count": 2, "confidence": .9}]}
    event = _normalize(s, r, "cache", "video", 3, 3, raw, {"source_sha256": "a" * 64})["event"]
    assert event["source"]["counts"] == {"person": 2} and event["location"]["latitude"] == 1.3
    assert "#t=3.000000" in event["frame_ref"]
    for offset in [0, 7]:
        assert _normalize(s, r, "cache", "video", offset, offset, raw,
                          {"source_sha256": "a" * 64})["event"]["location"] is None


def test_cache_invalidates_changed_media_models_manifest_and_corruption(tmp_path):
    import importlib.util
    if not importlib.util.find_spec("ultralytics"):
        pytest.skip("install the perception extra for cache fingerprint tests")
    s = scenario()
    rec = s.recordings[0]
    media = tmp_path / rec.video
    media.write_bytes(b"not decoded in this cache identity unit test")
    identity = cache_identity(s, rec, media)
    key = digest(identity)
    cache = {"schema": "vixy.perception_cache.v1", "identity": identity, "key": key,
             "events": [], "events_sha256": digest([])}
    directory = tmp_path / "cache"
    directory.mkdir()
    file = directory / f"{key}.json"
    file.write_text(json.dumps(cache))
    manifest = tmp_path / "scenario.json"
    assert load_cache(s, rec, manifest, tmp_path)["key"] == key
    media.write_bytes(b"changed")
    with pytest.raises(ValueError, match="no matching"):
        load_cache(s, rec, manifest, tmp_path)
    media.write_bytes(b"not decoded in this cache identity unit test")
    cache["events_sha256"] = "corrupt"
    file.write_text(json.dumps(cache))
    with pytest.raises(ValueError, match="damaged"):
        load_cache(s, rec, manifest, tmp_path)
    assert cache_identity(s.model_copy(update={"perception": s.perception.model_copy(update={"fps": 2})}), rec, media) != identity
    models = tmp_path / "models"
    models.mkdir()
    weights = models / "yolo11n.pt"
    weights.write_bytes(b"first-checkpoint")
    with_weights = cache_identity(s, rec, media, models)
    weights.write_bytes(b"replacement-checkpoint")
    assert cache_identity(s, rec, media, models) != with_weights


def test_corrupt_media_and_reset_protection(tmp_path):
    media = tmp_path / "broken.mp4"
    media.write_bytes(b"invalid")
    with pytest.raises(ValueError, match="ffprobe failed"):
        inspect_video(media)
    s = scenario()
    # Validation must happen before any graph or filesystem deletion.
    with pytest.raises(ValueError, match="reset requires"):
        reset_disposable(VixyConfig(user_id="demo").under(tmp_path), s)
    s = s.model_copy(update={"disposable": True})
    cfg = VixyConfig(user_id=s.user_id).under(tmp_path)
    Path(cfg.events_db).write_text("preserve me")
    with pytest.raises(ValueError, match="without matching"):
        reset_disposable(cfg, s)
    assert Path(cfg.events_db).read_text() == "preserve me"


def test_shared_environment_never_overrides_explicit_values(monkeypatch, tmp_path):
    from prod_dreamer_model.config import DreamerConfig, _load_env_files
    from prod_dreamer_model.model_extract import ModelSettings
    env = tmp_path / "local.env"
    env.write_text("MODEL_API_KEY=synthetic-unit-test-value\n")
    monkeypatch.setenv("VIXY_ENV_FILE", str(env))
    monkeypatch.setenv("MODEL_API_KEY", "")
    _load_env_files()
    assert ModelSettings.from_env().api_key is None
    monkeypatch.delenv("MODEL_API_KEY")
    VixyConfig.from_env()
    assert DreamerConfig.from_env().model_api_key == ModelSettings.from_env().api_key == "synthetic-unit-test-value"


def test_private_runtime_refuses_contributor_models_before_any_io():
    from types import SimpleNamespace
    from vixy.runtime import VixyRuntime
    from prod_dreamer_model.config import DreamerConfig
    from vixy.dream_job import run_dream
    contributor = SimpleNamespace(model_id="muse-spark-1.3-contributor")
    with pytest.raises(ValueError, match="contributor"):
        VixyRuntime(reasoner=contributor)
    with pytest.raises(ValueError, match="contributor"):
        run_dream(VixyConfig(), None, extractor=SimpleNamespace(model=contributor))
    with pytest.raises(ValueError, match="contributor"):
        DreamerConfig(graph_layout="unified", muse_model=contributor.model_id,
                      allow_contributor_model=True).check_model_policy()


def test_standalone_active_agent_uses_same_meta_key_and_requires_model_opt_in(monkeypatch):
    from agent.llm import from_env
    monkeypatch.setenv("MODEL_API_KEY", "synthetic-shared-test-key")
    monkeypatch.setenv("VIXY_LLM_MODEL", "")
    monkeypatch.setenv("VEYRA_LLM_MODEL", "")
    assert from_env() is None
    monkeypatch.setenv("VIXY_LLM_MODEL", "muse-spark-1.3")
    provider = from_env()
    assert provider.api_key == "synthetic-shared-test-key" and provider.base_url == "https://api.meta.ai/v1"
    monkeypatch.setenv("VIXY_LLM_MODEL", "muse-spark-1.3-contributor")
    with pytest.raises(ValueError, match="contributor"):
        from_env()


def test_simulated_clock_reminders_idempotency_restart_and_session_isolation(tmp_path):
    import uuid
    from vixy.config import connect_graph
    from vixy.runtime import VixyRuntime
    user = "m3_clock_" + uuid.uuid4().hex[:8]
    cfg = VixyConfig(user_id=user, session_ids=("day1", "day2")).under(tmp_path)
    at = int(datetime(2026, 11, 2, 10, tzinfo=timezone.utc).timestamp() * 1000)
    try:
        with VixyRuntime(cfg) as rt:
            rt.agent_memory.record_commitment(title="Clock test", start_ms=at + 20 * 60_000,
                end_ms=at + 40 * 60_000, status="confirmed", source="app", now_ms=at - 60_000)
            decisions = rt.advance_clock(at, "tick1")
            assert any("Clock test" in (d["message"] or "") and d["status"] == "done" for d in decisions)
            before = rt.memory.stats()
            assert rt.advance_clock(at, "tick1") == [] and rt.memory.stats() == before
            with pytest.raises(ValueError, match="backwards"):
                rt.advance_clock(at - 1, "new-earlier-tick")
            # Unknown speech cannot become a wearer command.
            s = scenario(user_id=user)
            r = s.recordings[0].model_copy(update={"session_id": "day1"})
            audio = _normalize(s, r, "cache", "audio", 1, 4,
                {"segment_id": "unknown", "text": "Vixy, remind me to buy milk."}, {"source_sha256": "a"*64})["event"]
            outcome = rt.ingest_audio(audio)
            assert outcome.ok
            assert all(d["action"] != "MEMORY_UPDATE" for d in outcome.decisions)
            # Each recording keeps its own session on persisted Visit nodes.
            for session in reversed(cfg.session_ids):
                for offset in (0, 15, 35):
                    assert rt.ingest_video({"event_id": f"{session}-{offset}", "session_id": session,
                        "timestamp": datetime.fromtimestamp(at/1000 + offset, timezone.utc).isoformat(),
                        "description": "cup", "detections": [{"label": "cup", "confidence": .9}]}).ok
                rt.flush_episodes(session)
            assert set(x[0] for x in rt.store.q("MATCH (v:Visit) RETURN v.session_id")) == {"day1", "day2"}
            assert all(a == b for a, b in rt.store.q("MATCH (n:Note)-[:DURING]->(v:Visit) RETURN n.session_id, v.session_id"))
            rt.advance_clock(at + 2 * 86_400_000, "tick-day3")
            assert rt.engine.store.count("day1") == 4  # one audio + three frames; ticks are not sensor data
        with VixyRuntime(cfg) as rt:
            assert rt.clock_ms() == at + 2 * 86_400_000
            assert rt.advance_clock(at, "tick1") == []
    finally:
        connect_graph(cfg).delete()
