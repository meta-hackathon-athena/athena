"""The human-readable diary: place naming, smoothing and stay merging."""

from __future__ import annotations

from prod_dreamer_model.readable import _hero, _smooth, _stay_segments


def test_hero_prefers_the_scene_class():
    assert _hero(["lamp", "wardrobe", "bedroom"]) == "bedroom"
    assert _hero(["bus", "seat", "bus interior"]) == "bus interior"
    assert _hero(["plate", "food court"]) == "food court"
    assert _hero(["ceiling", "poster"]) == "other"
    assert _hero([]) == "other"


def test_smooth_absorbs_a_single_dropout():
    heroes = ["home", "home", "other", "home", "home"]
    assert _smooth(heroes) == ["home"] * 5


def _frames(start_min: int, count: int, classes: list[str], every_s: int = 20) -> list[dict]:
    return [
        {
            "frame_id": f"{start_min}-{i}",
            "ts_ms": i,
            "local_date": "2026-10-19",
            "local_min": start_min + (i * every_s) // 60,
            "local_hour": 0,
            "dow": 0,
            "classes": list(classes),
        }
        for i in range(count)
    ]


def test_a_missed_class_does_not_split_the_stay():
    frames = _frames(460, 90, ["bedroom", "lamp", "pillow"])  # 07:40, 30 min
    frames[40]["classes"] = ["ceiling"]  # one frame loses the scene class
    segments = _stay_segments(frames)
    assert len(segments) == 1
    assert segments[0]["hero"] == "bedroom"
    assert segments[0]["start"] == 460
    assert segments[0]["end"] >= 489


def test_short_blips_are_dropped():
    frames = _frames(460, 60, ["library", "book"])  # 20 min in the library
    frames += _frames(481, 3, ["ceiling"])  # a 1-minute blip elsewhere
    segments = _stay_segments(frames)
    assert [s["hero"] for s in segments] == ["library"]


def test_two_real_places_stay_separate():
    frames = _frames(460, 30, ["bedroom"]) + _frames(490, 30, ["bus interior"])
    segments = _stay_segments(frames)
    assert [s["hero"] for s in segments] == ["bedroom", "bus interior"]
