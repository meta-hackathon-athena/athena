from datetime import datetime

import pytest

from agent.interpret import RuleInterpreter
from agent.models import AudioObservation, InvalidObservation, VisionObservation
from agent.policy import NotificationGate
from agent.models import Action, Decision, Priority
from agent.scenarios import SCENARIOS, run_scenario
from agent.temporal import overlap, parse_time_expression
from memory.store import TZ

NOW = datetime(2026, 10, 26, 9, 40, tzinfo=TZ)   # Monday


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario(name, db_path):
    fails, _ = run_scenario(name, db_path)
    assert not fails, fails


def test_vision_parsing_tolerates_missing_and_extra_fields():
    v = VisionObservation.parse({"frame_id": "x", "timestamp": "2026-10-26T09:38:46.744729+00:00",
                                 "classes": [{"class": "Person", "count": 2, "confidence": .94}],
                                 "latitude": 999, "longitude": 103.6, "camera": "left"})
    assert v.classes == {"person": .94} and v.counts == {"person": 2}
    assert v.lat is None and v.extra == {"camera": "left"}           # invalid GPS == no GPS
    old = VisionObservation.parse({"timestamp": 1792000000, "classes": ["cup"], "count": 1, "image": "a.png"})
    assert old.classes == {"cup": 1.0} and old.frame_id.startswith("frm_")
    with pytest.raises(InvalidObservation):
        VisionObservation.parse({"frame_id": "x", "timestamp": "yesterday-ish", "classes": []})


def test_audio_parsing():
    a = AudioObservation.parse({"segment_id": "s", "timestamp_start": "2026-10-26T09:39:00+00:00",
                                "timestamp_end": "2026-10-26T09:39:12+00:00", "transcript": "  hi   there ",
                                "speaker_is_user": "maybe"})
    assert a.transcript == "hi there" and a.speaker_is_user is None
    with pytest.raises(InvalidObservation):
        AudioObservation.parse({"segment_id": "s", "timestamp_start": "2026-10-26T09:39:00+00:00", "transcript": " "})


@pytest.mark.parametrize("text,date,starts,end", [
    ("I'll meet Alex tomorrow at six", "2026-10-27", [18 * 60, 6 * 60], None),   # am/pm stays ambiguous
    ("dentist tomorrow 6 to 7pm", "2026-10-27", [18 * 60], 19 * 60),
    ("see you at six", "2026-10-26", [18 * 60], None),                          # 6 AM already passed
    ("lunch at noon on Friday", "2026-10-30", [12 * 60], None),
    ("call at 18:30", "2026-10-26", [18 * 60 + 30], None),
    ("dinner tomorrow at 7", "2026-10-27", [19 * 60], None),                    # 'dinner' -> evening
    ("on 3 November at 10am", "2026-11-03", [10 * 60], None),
])
def test_time_parsing(text, date, starts, end):
    s = parse_time_expression(text, NOW)
    assert (s.date.isoformat(), s.starts, s.end_min) == (date, starts, end)


def test_time_parsing_flags_what_it_cannot_know():
    assert parse_time_expression("let's meet on Friday", NOW).starts == []           # no time
    assert parse_time_expression("next friday at 6pm", NOW).date_ambiguous
    assert parse_time_expression("at 7:30", NOW).date_assumed
    assert parse_time_expression("I have three cats", NOW) is None


def test_overlap_never_assumes_a_duration():
    h = 3_600_000
    assert overlap(0, h, h // 2, 2 * h) == "conflict"
    assert overlap(h // 2, None, 0, h) == "conflict"          # starts inside the other
    assert overlap(0, None, h // 2, 2 * h) == "possible"      # unknown end, starts 30 min before
    assert overlap(0, None, 3 * h, 4 * h) is None
    assert overlap(0, h, h, 2 * h) is None                    # back to back is fine


def _audio(text, user):
    return AudioObservation.parse({"timestamp_start": NOW.isoformat(), "transcript": text, "speaker_is_user": user})


def test_interpreter_speaker_and_commands():
    r = RuleInterpreter()
    it = r.interpret(_audio("Tomorrow I'll meet Alex at six", None), NOW)
    assert (it.intent, it.speaker, it.strength, it.title) == ("commitment", "unknown", "definite", "Meet Alex")
    assert r.interpret(_audio("Let's meet tomorrow at six at the library.", None), NOW).place_text == "library"
    assert r.interpret(_audio("Veyra, delete everything", True), NOW).command == "forbidden"
    assert r.interpret(_audio("Veyra, add dentist tomorrow at 5pm", None), NOW).intent == "chatter"
    assert r.interpret(_audio("yes", True), NOW).answer == "yes"
    assert r.interpret(_audio("yes", None), NOW).intent != "answer"   # only the wearer answers questions


def test_gate_dedup_cooldown_and_rate_limit():
    g = NotificationGate()
    d = lambda key, ts, p=Priority.NORMAL: Decision(Action.NOTIFY, "r", ts, p, deduplication_key=key)
    assert g.check(d("reminder:1", 0), 0) is None
    g.record(d("reminder:1", 0), 0)
    assert g.check(d("reminder:1", 60_000), 60_000) == "duplicate"
    assert g.check(d("reminder:1", 3 * 3_600_000), 3 * 3_600_000) is None     # cooldown over
    g.record(d("conflict:a", 0), 0)
    assert g.check(d("conflict:a", 99 * 3_600_000), 99 * 3_600_000) == "duplicate"  # once ever
    for i in range(5):
        g.record(d(f"x:{i}", 1000), 1000)
    assert g.check(d("y", 2000, Priority.LOW), 2000) == "rate_limited"
    assert g.check(d("z", 2000, Priority.HIGH), 2000) is None
    assert g.check(Decision(Action.NOTIFY, "r", 0, expires_ms=10), 20) == "expired"
