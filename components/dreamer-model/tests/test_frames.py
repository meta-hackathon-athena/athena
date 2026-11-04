"""Vision-frame parsing and the context-engine observation mapping."""

from __future__ import annotations

import pytest

from prod_dreamer_model.frames import (
    describe_frame,
    parse_frame,
    parse_ts,
    to_video_observation,
)


def test_parse_current_shape():
    frame = parse_frame(
        {
            "frame_id": "99b232d8abc9",
            "timestamp": "2026-10-26T09:38:46.744729+00:00",
            "classes": [
                {"class": "person", "count": 2, "confidence": 0.94},
                {"class": "chair", "count": 3, "confidence": 0.88},
                {"class": "cup", "count": 4, "confidence": 0.72},
            ],
        }
    )
    assert frame.frame_id == "99b232d8abc9"
    assert frame.ts_ms == parse_ts("2026-10-26T09:38:46.744729+00:00")
    assert [c.name for c in frame.classes] == ["chair", "cup", "person"]
    assert frame.class_map()["person"] == (2, 0.94)


def test_parse_legacy_shapes():
    frame = parse_frame(
        {"timestamp": "2026-10-26T09:38:46Z", "classes": ["person", "cup"],
         "confidences": {"person": 0.9}}
    )
    assert frame.class_map()["person"] == (1, 0.9)
    assert frame.class_map()["cup"] == (1, 1.0)
    assert frame.frame_id == "2026-10-26T09:38:46Z"


def test_parse_rejects_naive_timestamp():
    with pytest.raises(ValueError):
        parse_frame({"timestamp": "2026-10-26T09:38:46", "classes": []})


def test_describe_orders_by_confidence():
    frame = parse_frame(
        {"frame_id": "f", "timestamp": "2026-10-26T09:38:46Z",
         "classes": [{"class": "cup", "count": 1, "confidence": 0.6},
                     {"class": "person", "count": 12, "confidence": 0.95}]}
    )
    assert describe_frame(frame) == "person x12, cup"


def test_observation_keeps_counts_in_source_and_gps():
    frame = parse_frame(
        {"frame_id": "f1", "timestamp": "2026-10-26T09:38:46Z", "lat": 1.34, "lon": 103.68,
         "classes": [{"class": "person", "count": 5, "confidence": 0.9}]}
    )
    obs = to_video_observation(frame, "s1")
    assert obs["event_id"] == "cam0:f1"
    assert obs["source"]["counts"] == {"person": 5}
    assert obs["location"] == {"latitude": 1.34, "longitude": 103.68}
    assert obs["detections"] == [{"label": "person", "confidence": 0.9}]
