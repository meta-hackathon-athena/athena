"""Opt-in private-recording acceptance. No private media/transcripts in this file."""
import json
import os
import re
import threading
import urllib.request
from pathlib import Path

import pytest

from vixy.api import serve
from vixy.perception import load_cache
from vixy.replay import replay_config, replay_scenario
from vixy.runtime import VixyRuntime
from vixy.scenario import load_scenario

MANIFEST = os.getenv("VIXY_REAL_M3_SCENARIO")
SERVER = os.getenv("VIXY_TEST_FALKORDB_URL")


@pytest.mark.skipif(not MANIFEST, reason="set VIXY_REAL_M3_SCENARIO to an isolated disposable private-recording manifest")
@pytest.mark.parametrize("backend", ["embedded", pytest.param("server", marks=[pytest.mark.server,
    pytest.mark.skipif(not SERVER, reason="set VIXY_TEST_FALKORDB_URL to a disposable server")])])
def test_real_recordings_notes_dream_decisions_http_ask_and_duplicate_replay(backend, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "")
    monkeypatch.setenv("BASETEN_API_KEY", "")
    monkeypatch.setenv("FALKORDB_URL", SERVER if backend == "server" else "")
    manifest = Path(MANIFEST)
    scenario = load_scenario(manifest)
    assert scenario.disposable and scenario.user_id.startswith("m3_real_")
    output = Path(os.getenv("VIXY_REAL_M3_OUTPUT", f"demo/outputs/{scenario.user_id}"))
    caches = [load_cache(scenario, rec, manifest, output) for rec in scenario.recordings]
    events = [row for c in caches for row in c["events"]]
    audio = [row["event"] for row in events if row["kind"] == "audio"]
    assert audio and all(c["model_usage"]["vision"]["calls"] > 0 for c in caches)
    for c in caches:
        for row in c["events"]:
            if row["kind"] == "audio":
                source = row["event"]["source"]
                assert 0 <= source["capture_offset_s"] < source["capture_end_s"] <= c["media"]["duration_s"]
                assert source["capture_end_s"] <= c["media"]["audio_offset_s"] + c["media"]["audio_decoded_duration_s"]
    # This test consumes actual inference output; replay must never run inference.
    from vixy import perception
    monkeypatch.setattr(perception.LocalModels, "__init__", lambda *a, **kw: pytest.fail("replay ran inference"))
    first = replay_scenario(manifest, output, fresh=True, emit=lambda *a: None)
    assert first["outcomes"] == {"processed": len(events)}
    cfg = replay_config(scenario, output)
    with VixyRuntime(cfg) as rt:
        assert rt.memory.stats()["Note"] == len(audio)
        assert sum(rt.status()["events"].values()) == len(events)
        assert rt.memory.stats()["DreamRun"] >= sum(r.dream_after for r in scenario.recordings)
        for segment in audio:
            original = rt.engine.get_event_record(segment["event_id"])
            assert original.event["text"] == segment["text"]
            rows = rt.store.q("MATCH (n:Note {source_event_id: $id}) RETURN n.payload_sha256, n.speaker_role",
                              {"id": segment["event_id"]})
            assert rows[0][0] == original.payload_sha256
            if not segment["source"].get("speaker_is_wearer") and segment.get("speaker_id") is None:
                assert rows[0][1] == "unknown"
        # Select a term from actual observed speech; no scenario answer is scripted.
        query = max(re.findall(r"[a-z]{5,}", " ".join(a["text"] for a in audio).lower()), key=len)
        server = serve(rt, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/ask",
                data=json.dumps({"question": query}).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as response:
                answer = json.load(response)["result"]
            assert answer["status"] == "answered" and answer["sources"]
            assert all(s["verified"] and rt.engine.get_event_record(s["source_event_id"]) for s in answer["sources"])
            # Persist private verification evidence only in the ignored local output.
            (output / f"verified-ask-{backend}.json").write_text(json.dumps(answer, indent=2))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        snapshot = (rt.memory.stats(), rt.store.q("MATCH ()-[r]->() RETURN count(r)")[0][0])
    again = replay_scenario(manifest, output, emit=lambda *a: None)
    assert again["outcomes"] == {"duplicate": len(events)}
    with VixyRuntime(cfg) as rt:
        assert (rt.memory.stats(), rt.store.q("MATCH ()-[r]->() RETURN count(r)")[0][0]) == snapshot
