"""Mock wearable week + context-engine bridge round trip."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from prod_dreamer_model.bridge import EventBridge, classify_note
from prod_dreamer_model.mock_data import synthetic_days

TZ = ZoneInfo("Asia/Singapore")


def test_mock_week_is_deterministic_and_has_the_commute():
    days = synthetic_days("2026-10-26", days=7, seed=7)
    assert [d.date.isoformat() for d in days][0] == "2026-10-26"
    assert all(d.frames for d in days)
    assert sum(len(d.notes) for d in days) == 6

    monday = days[0]
    bus = [f for f in monday.frames if "bus interior" in f.class_map()]
    assert bus, "expected a morning bus commute"
    first_bus = min(f.timestamp for f in bus).astimezone(TZ)
    assert 7 <= first_bus.hour <= 9, first_bus

    again = synthetic_days("2026-10-26", days=7, seed=7)
    assert [f.frame_id for f in again[0].frames] == [f.frame_id for f in monday.frames]


def test_classify_note():
    assert classify_note("Remind me to buy milk at the supermarket.") == "reminder"
    assert classify_note("I really like the noodles at the canteen here.") == "preference"
    assert classify_note("Taking the bus to uni again.") == "note"


def test_bridge_round_trip_is_idempotent_and_preserves_counts(tmp_path):
    days = synthetic_days("2026-10-26", days=2, seed=7)
    db = str(tmp_path / "events.sqlite3")
    with EventBridge(db, "tester", "Asia/Singapore") as bridge:
        first = bridge.ingest_mock_day(days[0])
        assert first.accepted > 0 and first.duplicates == 0
        assert first.by_kind["audio"] == len(days[0].notes)

        again = bridge.ingest_mock_day(days[0])
        assert again.accepted == 0
        assert again.duplicates == first.accepted

        second = bridge.ingest_mock_day(days[1])
        assert second.accepted > 0

        end = days[-1].date + timedelta(days=1)
        bundles = bridge.bundles_for_range(days[0].date, end)

    assert len(bundles) == 2
    monday = bundles[0]
    assert monday.day_id == "2026-10-26"
    assert len(monday.frames) == len(days[0].frames)
    assert len(monday.notes) == len(days[0].notes)
    assert monday.first_ms <= monday.last_ms

    frame = monday.frames[0]
    assert frame.classes and frame.description
    assert frame.lat is not None and frame.lon is not None
    assert any(c.count >= 1 for c in frame.classes)

    kinds = {n.kind for n in monday.notes}
    assert "preference" in kinds
