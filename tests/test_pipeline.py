from datetime import datetime, timedelta

from memory.frames import Frame, attach_gps, parse_frame
from memory.rules import classify_activity, classify_place
from memory.segmenter import segment
from memory.store import TZ

VISION_OUTPUT = {
    "frame_id": "2eccf0cd92f9",
    "timestamp": "2026-10-26T09:30:56.820596+00:00",
    "image": "yolomodelvision/koufu.PNG",
    "classes": ["person", "chair", "table", "laptop", "cup"],
    "count": 5,
}
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=TZ)


def frames(classes, n, start=T0, every_s=10, lat=1.3467, lon=103.68, prefix="f"):
    return [Frame(frame_id=f"{prefix}{i}", ts_ms=int((start + timedelta(seconds=i * every_s)).timestamp() * 1000),
                  classes={c: 0.9 for c in classes}, lat=lat, lon=lon, accuracy_m=10)
            for i in range(n)]


NEW_VISION_OUTPUT = {
    "frame_id": "99b232d8abc9",
    "timestamp": "2026-10-26T09:38:46.744729+00:00",
    "classes": [
        {"class": "person", "count": 2, "confidence": 0.94},
        {"class": "chair", "count": 3, "confidence": 0.88},
        {"class": "table", "count": 1, "confidence": 0.91},
        {"class": "laptop", "count": 1, "confidence": 0.79},
        {"class": "cup", "count": 4, "confidence": 0.72},
    ],
}


def test_parse_new_vision_output_with_counts():
    f = parse_frame(NEW_VISION_OUTPUT)
    assert f.classes == {"person": 0.94, "chair": 0.88, "table": 0.91, "laptop": 0.79, "cup": 0.72}
    assert f.counts == {"person": 2, "chair": 3, "table": 1, "laptop": 1, "cup": 4}


def test_visit_keeps_max_counts():
    fs = [parse_frame({**NEW_VISION_OUTPUT, "frame_id": f"x{i}",
                       "timestamp": 1792900000 + i * 10}) for i in range(6)]
    fs[2].counts["person"] = 7
    vs = segment(fs, "u")
    assert vs[0].tag_max_count["person"] == 7 and vs[0].tag_max_count["cup"] == 4


def test_parse_current_vision_output():
    f = parse_frame(VISION_OUTPUT)
    assert f.frame_id == "2eccf0cd92f9"
    assert set(f.classes) == {"person", "chair", "table", "laptop", "cup"}
    assert all(c == 1.0 for c in f.classes.values())      # no scores in output -> 1.0
    assert "image" not in f.extra and not f.has_gps        # image path is not kept


def test_parse_with_confidences_and_gps():
    f = parse_frame({**VISION_OUTPUT, "confidences": {"laptop": 0.7}, "gps": {"lat": 1.3, "lon": 103.6}})
    assert f.classes["laptop"] == 0.7 and f.classes["cup"] == 1.0 and f.has_gps
    f2 = parse_frame({"timestamp": 1792900000, "classes": [{"label": "Cup", "confidence": 0.4}]})
    assert f2.classes == {"cup": 0.4}


def test_attach_gps_nearest_in_time():
    fs = [parse_frame(VISION_OUTPUT)]
    attach_gps(fs, [{"timestamp": "2026-10-26T09:30:50+00:00", "lat": 1.34, "lon": 103.68},
                    {"timestamp": "2026-10-26T11:00:00+00:00", "lat": 9.0, "lon": 9.0}])
    assert (fs[0].lat, fs[0].lon) == (1.34, 103.68)


def test_repeated_frames_become_one_visit():
    vs = segment(frames(["food court", "plate", "noodle", "person"], 30), "u")
    assert len(vs) == 1 and vs[0].frames == 30
    assert vs[0].place_type == "food_court" and vs[0].activity == "eating"


def test_single_odd_frame_does_not_split():
    fs = frames(["library", "book", "laptop"], 20)
    fs[10].classes = {"ceiling": 0.9, "handbag": 0.9}
    assert len(segment(fs, "u")) == 1


def test_scene_change_splits():
    a = frames(["classroom", "projector", "whiteboard"], 20)
    b = frames(["food court", "plate", "noodle"], 20, start=T0 + timedelta(seconds=200), prefix="g")
    vs = segment(a + b, "u")
    assert [v.place_type for v in vs] == ["classroom", "food_court"]


def test_time_gap_and_gps_move_split():
    a = frames(["library", "book"], 10)
    gap = frames(["library", "book"], 10, start=T0 + timedelta(minutes=30), prefix="g")
    assert len(segment(a + gap, "u")) == 2
    moved = frames(["library", "book"], 10, start=T0 + timedelta(seconds=100), lat=1.3567, prefix="m")
    assert len(segment(a + moved, "u")) == 2


def test_moving_bus_is_one_transit_visit():
    fs = frames(["bus interior", "bus", "person"], 60)
    for i, f in enumerate(fs):
        f.lat = 1.34 + i * 0.0005                          # ~55 m per frame
    vs = segment(fs, "u")
    assert len(vs) == 1 and vs[0].kind == "transit"


def test_blips_are_dropped():
    assert segment(frames(["gym"], 2), "u") == []


def test_koufu_example_rules():
    tags = {"person": 1.0, "chair": 1.0, "table": 1.0, "laptop": 1.0, "cup": 1.0}
    assert classify_place(tags) == "unknown"               # no scene class -> don't guess
    assert classify_activity(tags, "unknown") == "working"
    assert classify_place({**tags, "food court": 0.8}) == "food_court"
