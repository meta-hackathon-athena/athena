"""Synthetic week of vision frames (same shape as the vision model output + GPS),
so the memory pipeline can be exercised without the wearable."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from .frames import Frame
from .store import TZ

# (name, lat, lon, classes the vision model would plausibly return there)
PLACES = {
    "home": (1.3483, 103.6831, ["bedroom", "bed", "pillow", "lamp", "wardrobe"]),
    "bus": (None, None, ["bus interior", "bus", "person", "seat", "window"]),
    "classroom": (1.3442, 103.6810, ["classroom", "projector", "whiteboard", "chair", "person", "laptop"]),
    "koufu": (1.3467, 103.6800, ["food court", "person", "chair", "table", "plate", "noodle", "cup"]),
    "library": (1.3478, 103.6809, ["library", "bookcase", "book", "laptop", "person", "table"]),
    "gym": (1.3490, 103.6880, ["gym", "dumbbell", "treadmill", "person", "mirror"]),
    "supermarket": (1.3500, 103.6900, ["supermarket", "shopping cart", "shelf", "person", "fruit"]),
    "clinic": (1.3510, 103.6920, ["clinic", "chair", "person", "poster"]),
}
NOISE = ["ceiling", "handbag", "pillar", "garbage", "camera", "poster", "lamp"]

# (place, start "HH:MM", minutes) per weekday; Tue/Thu add gym; Wed has a clinic visit on day 3
WEEKDAY = [("home", "07:40", 30), ("bus", "08:12", 25), ("classroom", "08:40", 170),
           ("koufu", "11:35", 55), ("library", "12:35", 230), ("bus", "16:30", 25),
           ("home", "17:00", 60)]


def _frames_for(place: str, start: datetime, minutes: int, rng: random.Random,
                every_s: int = 15, path: tuple | None = None) -> list[Frame]:
    lat, lon, classes = PLACES[place]
    out = []
    n = minutes * 60 // every_s
    for i in range(n):
        if rng.random() < 0.1:                     # dropped frames
            continue
        ts = start + timedelta(seconds=i * every_s)
        seen = {c: round(rng.uniform(0.6, 0.97), 2) for c in classes if rng.random() > 0.15}
        counts = {c: (rng.randint(1, 12) if c == "person" else rng.randint(1, 3)) for c in seen}
        if rng.random() < 0.2:                     # false positives
            seen[rng.choice(NOISE)] = round(rng.uniform(0.6, 0.8), 2)
        if path:                                   # moving: interpolate along the path
            (a, b), (c, d) = path
            t = i / max(n - 1, 1)
            flat, flon = a + (c - a) * t, b + (d - b) * t
        else:
            flat = lat + rng.gauss(0, 0.00012) if lat else None  # ~13 m GPS noise
            flon = lon + rng.gauss(0, 0.00012) if lon else None
        out.append(Frame(frame_id=f"{place}-{ts.isoformat()}", ts_ms=int(ts.timestamp() * 1000),
                         classes=seen, counts=counts, lat=flat, lon=flon, accuracy_m=15.0))
    return out


def synthetic_week(start_date: str = "2026-10-19", days: int = 5, seed: int = 7) -> list[Frame]:
    rng = random.Random(seed)
    base = datetime.fromisoformat(start_date).replace(tzinfo=TZ)
    frames: list[Frame] = []
    for d in range(days):
        day = base + timedelta(days=d)
        plan = list(WEEKDAY)
        if day.weekday() in (1, 3):
            plan[-1] = ("gym", "17:00", 60)
            plan.append(("home", "18:20", 40))
        if d == 2:
            plan.append(("clinic", "19:15", 30))      # sensitive: stored, never a habit
        if d == 4:
            plan.append(("supermarket", "18:10", 20))
        prev = PLACES["home"]
        prev_end = day
        for i, (place, hhmm, mins) in enumerate(plan):
            h, m = map(int, hhmm.split(":"))
            start = day.replace(hour=h, minute=m) + timedelta(minutes=rng.randint(-6, 6))
            start = max(start, prev_end + timedelta(minutes=2))   # slots never overlap
            prev_end = start + timedelta(minutes=mins)
            path = None
            if place == "bus":
                nxt = PLACES[plan[i + 1][0]]
                path = ((prev[0], prev[1]), (nxt[0], nxt[1]))
            frames += _frames_for(place, start, mins, rng, path=path)
            if PLACES[place][0] is not None:
                prev = PLACES[place]
    return frames
