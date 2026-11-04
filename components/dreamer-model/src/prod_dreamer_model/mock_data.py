"""Synthetic node-memory for a week of wearable use.

Stands in for the real YOLO/vision stream so the dreamer can be exercised
end-to-end today. The class vocabulary is drawn from
``yolomodelvision/classes.txt``, and the rhythm is the one the product wants to
learn: the bus to campus at ~08:12 on weekdays, lunch at the food court, an
afternoon in the library, gym on Tue/Thu, the supermarket on Friday, and a
clinic visit that is *not* a habit.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, time as Time
from datetime import timedelta
from zoneinfo import ZoneInfo

from .frames import ClassObservation, VisionFrame

# place -> (lat, lon, classes seen there)
PLACES: dict[str, tuple[float | None, float | None, list[str]]] = {
    "home": (1.3483, 103.6831,
             ["bedroom", "bed", "pillow", "lamp", "wardrobe", "sofa", "television"]),
    "bus": (None, None,
            ["bus interior", "bus", "seat", "window"]),
    "classroom": (1.3442, 103.6810,
                  ["classroom", "projector", "whiteboard", "chair", "laptop", "notebook", "pen"]),
    "food_court": (1.3467, 103.6800,
                   ["food court", "canteen", "chair", "table", "plate", "bowl",
                    "noodle", "cup", "tray", "chopstick"]),
    "library": (1.3478, 103.6809,
                ["library", "bookcase", "book", "laptop", "table", "chair", "notebook"]),
    "gym": (1.3490, 103.6880,
            ["gym", "dumbbell", "treadmill", "mirror", "barbell"]),
    "supermarket": (1.3500, 103.6900,
                    ["supermarket", "shopping cart", "shelf", "fruit", "milk"]),
    "park": (1.3455, 103.6790,
             ["park", "tree", "grass", "bench", "dog"]),
    "cafe": (1.3470, 103.6815,
             ["cafe", "coffee", "cup", "table", "chair", "cake"]),
    "clinic": (1.3510, 103.6920,
               ["clinic", "chair", "poster", "nurse"]),
}
NOISE = ["ceiling", "handbag", "pillar", "garbage", "camera", "poster", "lamp"]

# (place, "HH:MM", minutes) — a normal weekday.
WEEKDAY: list[tuple[str, str, int]] = [
    ("home", "07:40", 30),
    ("bus", "08:12", 25),
    ("classroom", "08:40", 170),
    ("food_court", "11:35", 55),
    ("library", "12:35", 230),
    ("bus", "16:30", 25),
    ("home", "17:00", 60),
]
WEEKEND: list[tuple[str, str, int]] = [
    ("home", "09:00", 60),
    ("park", "10:10", 60),
    ("cafe", "11:20", 70),
    ("home", "13:00", 120),
]

# Spoken notes (finalized audio transcripts) that illustrate the audio path.
NOTES: list[tuple[int, str, str, int]] = [
    # (weekday 0=Mon, "HH:MM", text, seconds)
    (0, "08:15", "Taking the bus to uni again, the traffic was bad today.", 4),
    (0, "11:42", "I really like the noodles at the canteen here.", 4),
    (2, "12:40", "Need to finish the assignment in the library this afternoon.", 5),
    (2, "19:10", "Picking up my prescription at the clinic.", 4),
    (4, "18:15", "Remind me to buy milk at the supermarket.", 4),
    (5, "11:30", "Meeting Alex at the cafe for brunch.", 4),
]


@dataclass
class MockDay:
    date: Date
    frames: list[VisionFrame] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)

    @property
    def day_id(self) -> str:
        return self.date.isoformat()


def _classes_at(place: str, rng: random.Random) -> tuple[ClassObservation, ...]:
    names = PLACES[place][2]
    out: list[ClassObservation] = []
    for name in names:
        if rng.random() < 0.18:  # class not visible in this frame
            continue
        if name == "person":
            count = rng.randint(1, 12)
        else:
            count = rng.randint(1, 3)
        out.append(ClassObservation(name=name, count=count,
                                    confidence=round(rng.uniform(0.62, 0.97), 2)))
    if rng.random() < 0.18:  # false positive
        out.append(ClassObservation(name=rng.choice(NOISE), count=1,
                                    confidence=round(rng.uniform(0.55, 0.78), 2)))
    return tuple(out)


def _frames_for(
    place: str,
    start: datetime,
    minutes: int,
    rng: random.Random,
    tz: ZoneInfo,
    every_s: int = 20,
    path: tuple[tuple[float, float], tuple[float, float]] | None = None,
) -> list[VisionFrame]:
    lat, lon, _ = PLACES[place]
    n = max(int(minutes * 60 // every_s), 1)
    out: list[VisionFrame] = []
    for i in range(n):
        if rng.random() < 0.08:  # dropped frame
            continue
        ts = start + timedelta(seconds=i * every_s)
        if path:
            (a, b), (c, d) = path
            t = i / max(n - 1, 1)
            flat, flon = a + (c - a) * t, b + (d - b) * t
        elif lat is not None and lon is not None:
            flat = lat + rng.gauss(0, 0.00012)  # ~13 m GPS noise
            flon = lon + rng.gauss(0, 0.00012)
        else:
            flat = flon = None
        out.append(
            VisionFrame(
                frame_id=f"{place}-{ts.astimezone(tz).strftime('%Y%m%dT%H%M%S')}-{i:04d}",
                ts_ms=int(ts.timestamp() * 1000),
                classes=_classes_at(place, rng),
                lat=flat,
                lon=flon,
                accuracy_m=15.0 if flat is not None else None,
            )
        )
    return out


def _plan_for(day: Date, week_index: int) -> list[tuple[str, str, int]]:
    if day.weekday() >= 5:
        return list(WEEKEND)
    plan = list(WEEKDAY)
    if day.weekday() in (1, 3):  # Tue/Thu: gym after class, home later
        plan = plan[:5] + [("gym", "16:10", 70), ("home", "17:40", 60)]
    if day.weekday() == 4:  # Fri: supermarket run
        plan = plan + [("supermarket", "18:10", 25)]
    if day.weekday() == 2 and week_index == 0:  # one-off, not a habit
        plan = plan + [("clinic", "19:15", 30)]
    return plan


def synthetic_days(
    start_date: str | Date | None = None,
    days: int = 7,
    seed: int = 7,
    tz_name: str = "Asia/Singapore",
) -> list[MockDay]:
    """Deterministic mock week(s). ``start_date=None`` starts the previous Monday."""
    tz = ZoneInfo(tz_name)
    if start_date is None:
        today = datetime.now(tz).date()
        start = today - timedelta(days=days - 1)
    elif isinstance(start_date, str):
        start = Date.fromisoformat(start_date)
    else:
        start = start_date
    # snap to Monday so the weekly rhythm is complete
    start = start - timedelta(days=start.weekday())

    rng = random.Random(seed)
    out: list[MockDay] = []
    for d in range(days):
        day = start + timedelta(days=d)
        week_index = d // 7
        plan = _plan_for(day, week_index)
        mock = MockDay(date=day)
        prev_place = "home"
        prev_end = datetime.combine(day, Time(0, 0), tzinfo=tz)
        for i, (place, hhmm, mins) in enumerate(plan):
            h, m = map(int, hhmm.split(":"))
            start_dt = datetime.combine(day, Time(h, m), tzinfo=tz) + timedelta(
                minutes=rng.randint(-5, 5)
            )
            start_dt = max(start_dt, prev_end + timedelta(minutes=2))
            prev_end = start_dt + timedelta(minutes=mins)
            path = None
            if place == "bus":
                nxt = PLACES[plan[i + 1][0]] if i + 1 < len(plan) else PLACES["home"]
                prev = PLACES[prev_place]
                if prev[0] is not None and nxt[0] is not None:
                    path = ((prev[0], prev[1]), (nxt[0], nxt[1]))
            mock.frames.extend(_frames_for(place, start_dt, mins, rng, tz, path=path))
            prev_place = place

        for wd, hhmm, text, secs in NOTES:
            if day.weekday() != wd or week_index > 0:
                continue
            h, m = map(int, hhmm.split(":"))
            ts = datetime.combine(day, Time(h, m), tzinfo=tz)
            mock.notes.append(
                {
                    "event_id": f"note-{day.isoformat()}-{hhmm.replace(':', '')}",
                    "start_time": ts.isoformat(),
                    "end_time": (ts + timedelta(seconds=secs)).isoformat(),
                    "text": text,
                    "speaker_id": "spk_0",
                    "confidence": 0.94,
                }
            )
        mock.frames.sort(key=lambda f: f.ts_ms)
        mock.notes.sort(key=lambda n: n["start_time"])
        out.append(mock)
    return out


def total_frames(days: list[MockDay]) -> int:
    return sum(len(d.frames) for d in days)
