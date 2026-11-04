"""ASR must not turn Whisper's zero-padded tail into observations."""
import wave
from pathlib import Path

import pytest

from vixy.perception import _asr_interval, cache_identity, command, inspect_video, process_recording
from vixy.scenario import Scenario


def bound(start, end, *, audio=67.5773125, offset=0, media=67.64):
    return _asr_interval({"start": start, "end": end}, audio_duration_s=audio,
                         audio_offset_s=offset, media_duration_s=media)


def test_real_failure_padded_tail_is_rejected_not_moved_into_audio():
    # Same timing as the failing recording, without retaining private speech/media.
    assert bound(68, 69) == (None, "outside_decoded_audio")
    assert bound(67, 68) == (None, "boundary_exceeds_50ms_tolerance")
    assert bound(0, 300)[0] is None
    assert bound(67.5773125, 68)[0] is None
    assert bound(-3, -1)[0] is None


def test_short_audio_drift_and_stream_offsets():
    assert bound(-.02, .1, audio=.2, media=1) == ((0, .1), "clipped_small_drift")
    assert bound(.1, .22, audio=.2, media=1) == ((.1, .2), "clipped_small_drift")
    assert bound(.1, .26, audio=.2, media=1)[0] is None
    assert bound(.01, .19, audio=.2, offset=2, media=4)[0] == (2.01, 2.19)
    interval, status = bound(0, .2, audio=.3, offset=-.02, media=1)
    assert interval == pytest.approx((0, .18)) and status == "clipped_small_drift"
    assert bound(0, .7, audio=1, offset=-.5, media=1)[0] is None
    # Also enforce a shorter media timeline; decoded codec padding is not speech support.
    assert bound(.1, .32, audio=1, media=.3)[0] == (.1, .3)


@pytest.mark.parametrize("start,end", [(2, 1), (1, 1), (float("nan"), 2), (0, float("inf")), ("bad", 2)])
def test_malformed_intervals_fail_loudly(start, end):
    with pytest.raises(ValueError, match="ASR interval"):
        bound(start, end)


class TimestampModel:
    """Only ASR/detector results are stubbed in these decoder/normalizer tests."""
    def __init__(self):
        self.usage = {k: {"calls": 0, "seconds": 0} for k in ("vision", "asr")}

    def provenance(self, kind):
        return {"model_id": "test-only"}

    def detect(self, frame):
        self.usage["vision"]["calls"] += 1
        return []

    def transcribe(self, wav):
        with wave.open(str(wav)) as pcm:
            duration = pcm.getnframes() / pcm.getframerate()
        self.usage["asr"]["calls"] += 1
        return {"language": "en", "segments": [
            # Whisper can retain speech despite a high no-speech score because its
            # average token logprob is good. Do not apply a second score-only gate.
            {"id": 0, "start": 0, "end": duration / 2, "text": "observed speech",
             "no_speech_prob": .72, "avg_logprob": -.5},
            {"id": 1, "start": duration / 2, "end": duration + .02, "text": "small drift"},
            {"id": 2, "start": duration + .1, "end": duration + .2, "text": "padded tail"},
            {"id": 3, "start": 0, "end": duration + 10, "text": "unbounded overrun"},
        ]}


@pytest.mark.parametrize("stream_shift", [0, 5, -.1, -1])
def test_actual_short_mp4_audio_support_and_nonzero_origin(tmp_path, stream_shift):
    pytest.importorskip("cv2")
    video = tmp_path / "short.mp4"
    command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=5:d=2",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=0.5",
             "-c:v", "libx264", "-c:a", "aac", "-output_ts_offset", str(stream_shift), str(video)])
    s = Scenario.model_validate({"user_id": "m3_interval_test", "recordings": [{"id": "short",
        "session_id": "short", "video": video.name, "simulated_start": "2026-11-04T12:00:00Z"}]})
    r = s.recordings[0]
    info = inspect_video(video)
    assert 0 < info["duration_s"] <= 2.1
    cache = process_recording(s, r, video, TimestampModel(), cache_identity(s, r, video))
    speech = [e["event"] for e in cache["events"] if e["kind"] == "audio"]
    if stream_shift == -1:
        # The edit list cuts the entire short audio track before time zero.
        assert cache["media"]["audio_decoded_samples"] == 0 and speech == []
        assert cache["model_usage"]["asr"]["calls"] == 0
        assert "no final speech segments" in cache["warnings"][0]
        return
    assert [e["text"] for e in speech] == ["observed speech", "small drift"]
    assert cache["media"]["audio_decoded_duration_s"] < 1
    assert all(e["source"]["capture_end_s"] <= cache["media"]["audio_decoded_duration_s"] for e in speech)
    assert any("segment 2" in w and "outside_decoded_audio" in w for w in cache["warnings"])
    assert any("segment 3" in w and "boundary_exceeds" in w for w in cache["warnings"])
    assert speech[-1]["source"]["asr_timing"]["status"] == "clipped_small_drift"


def test_trailing_digital_silence_cannot_become_speech(tmp_path):
    pytest.importorskip("cv2")
    video = tmp_path / "trailing.mp4"
    command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=5:d=3",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=0.5", "-af", "apad=whole_dur=2",
             "-c:v", "libx264", "-c:a", "aac", str(video)])
    class SilenceModel(TimestampModel):
        def transcribe(self, wav):
            return {"segments": [
                {"id": 0, "start": 0, "end": .4, "text": "audible signal", "no_speech_prob": .72, "avg_logprob": -.5},
                {"id": 1, "start": 1, "end": 1.5, "text": "hallucinated silence", "avg_logprob": -.1},
            ]}
    s = Scenario.model_validate({"user_id": "m3_silence", "recordings": [{"id": "trailing",
        "session_id": "trailing", "video": video.name, "simulated_start": "2026-11-04T12:00:00Z"}]})
    r = s.recordings[0]
    cache = process_recording(s, r, video, SilenceModel(), cache_identity(s, r, video))
    assert [e["event"]["text"] for e in cache["events"] if e["kind"] == "audio"] == ["audible signal"]
    assert any("segment 1: near_digital_silence" in w for w in cache["warnings"])
