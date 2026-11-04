"""Opt-in acceptance of genuinely processed audiovisual fixtures (no perception mocks).

Generate/process a disposable fixture first, then set VIXY_M3_SCENARIO and VIXY_M3_OUTPUT.
Only the manifest's named disposable test user is reset. Original media/cache stays local.
"""
import json
import os
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

from vixy.api import VixyAPI, serve
from vixy.perception import load_cache
from vixy.replay import replay_config, replay_scenario
from vixy.runtime import VixyRuntime
from vixy.scenario import load_scenario, ms

MANIFEST = os.getenv("VIXY_M3_SCENARIO")
SERVER = os.getenv("VIXY_TEST_FALKORDB_URL")


@pytest.mark.skipif(not MANIFEST, reason="set VIXY_M3_SCENARIO to a generated, genuinely processed fixture")
@pytest.mark.parametrize("backend", ["embedded", pytest.param("server", marks=[pytest.mark.server,
    pytest.mark.skipif(not SERVER, reason="set VIXY_TEST_FALKORDB_URL for Docker acceptance")])])
def test_actual_av_multiday_pipeline_grounded_http_ask_and_duplicate_replay(backend, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "")
    monkeypatch.setenv("BASETEN_API_KEY", "")
    monkeypatch.setenv("FALKORDB_URL", SERVER if backend == "server" else "")
    manifest = Path(MANIFEST)
    scenario = load_scenario(manifest)
    assert scenario.disposable and scenario.user_id.startswith("m3_")
    output = Path(os.getenv("VIXY_M3_OUTPUT", f"demo/outputs/{scenario.user_id}"))
    caches = [load_cache(scenario, r, manifest, output) for r in scenario.recordings]
    events = [e for cache in caches for e in cache["events"]]
    video = [e["event"] for e in events if e["kind"] == "video"]
    audio = [e["event"] for e in events if e["kind"] == "audio"]
    assert len(caches) == 2 and len(video) >= 60 and len(audio) >= 2
    assert any(e["source"].get("counts", {}).get("person", 0) >= 1 for e in video)
    assert all(c["model_usage"]["vision"]["calls"] > 0 and c["model_usage"]["asr"]["calls"] > 0 for c in caches)
    assert "Alex is allergic to peanuts" in audio[0]["text"]
    assert "buying peanuts for Alex" in audio[1]["text"]
    assert audio[0]["speaker_id"] is None and audio[1]["source"]["speaker_is_wearer"] is True
    assert all(e["source"]["capture_time"] is None for e in video + audio)
    assert all(e["source"]["model"]["checkpoint_sha256"] for e in video + audio)
    # Replay is inference-free: prohibit loading models, without replacing any backend component.
    from vixy import perception
    monkeypatch.setattr(perception.LocalModels, "__init__", lambda *a, **kw: pytest.fail("replay ran inference"))
    result = replay_scenario(manifest, output, fresh=True, speed=2000,
                             question="What do you remember about Alex?", emit=lambda *a: None)
    assert result["outcomes"] == {"processed": len(events)}
    assert result["status"]["graph_labels"]["Note"] >= 2
    assert result["status"]["graph_labels"]["Visit"] >= 2
    assert result["status"]["graph_labels"]["DreamRun"] >= 2
    assert result["agent"]["recall"]["rule_hits"] >= 1
    assert result["answer"]["status"] == "answered" and all(s["verified"] for s in result["answer"]["sources"])
    cfg = replay_config(scenario, output)
    with VixyRuntime(cfg) as rt:
        before = rt.store.q("MATCH (n) RETURN count(n)")[0][0], rt.store.q("MATCH ()-[r]->() RETURN count(r)")[0][0]
        delivered = [d for d in rt.memory.decisions(limit=100) if d.get("supporting_memory_ids") and d["status"] == "done"]
        assert any("allergic" in d["message"] and "yesterday" in d["message"] for d in delivered)
        for source in result["answer"]["sources"]:
            original = rt.engine.get_event_record(source["source_event_id"])
            assert original and original.event["text"] in [a["text"] for a in audio]
            assert original.ingested_at == datetime.fromisoformat(original.event["end_time"].replace("Z", "+00:00"))
        assert rt.clock_ms() == result["simulated_end_ms"]
        # Actual HTTP request against the integrated runtime, not a canned API response.
        server = serve(rt, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/ask",
                data=json.dumps({"question": "What do you remember about Alex?"}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                answer = json.load(response)["result"]
            assert answer["status"] == "answered" and any(s["verified"] for s in answer["sources"])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
    pause = output / "acceptance.pause"
    pause.touch()
    release = threading.Timer(.1, lambda: pause.unlink(missing_ok=True))
    t0 = time.perf_counter()
    release.start()
    try:
        again = replay_scenario(manifest, output, speed=0, pause_file=pause, emit=lambda *a: None)
    finally:
        release.join(timeout=2)
        pause.unlink(missing_ok=True)
    assert time.perf_counter() - t0 >= .09
    assert again["outcomes"] == {"duplicate": len(events)}
    assert again["status"]["graph_labels"] == result["status"]["graph_labels"]
    with VixyRuntime(cfg) as rt:
        after = rt.store.q("MATCH (n) RETURN count(n)")[0][0], rt.store.q("MATCH ()-[r]->() RETURN count(r)")[0][0]
        assert after == before
