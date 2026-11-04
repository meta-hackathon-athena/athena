"""Milestone 2 multi-day scenario: learn on Monday, apply it on Tuesday.

Synthetic perception output (same shapes as ``demo_data``) for two days, Asia/Singapore:

  Mon 2 Nov   home -> office -> food court -> home. People around the wearer say things:
              "Alex is allergic to peanuts." (someone else), "Marta has a presentation on
              Thursday at 9 AM." (someone else), "Sam is vegetarian." (unknown speaker),
              "Lena lives in Lisbon." ...
  -- night --  the Dreamer consolidates Monday into structured, evidence-backed memories
  Tue 3 Nov   "I'm buying peanuts for Alex." / "I'm buying chocolate for Alex too." /
              "I'm buying peanuts for Jordan." / "I'll meet Marta on Thursday at 9am." /
              "Lena moved to Berlin." ...

Nothing here says what the agent should answer: the decisions, answers and checks come from
the real runtime (Context Engine -> Episode Writer -> Active Agent -> FalkorDB -> Dreamer ->
retrieval / ask). All of it is invented test data; nothing comes from a real person.
"""

from __future__ import annotations

import random
from datetime import timedelta

from .demo_data import DAY0, HOME, OFFICE, SUPERMARKET, at

FRAME_EVERY_S = 20
FOOD_COURT = (1.3460, 103.6800, {"food court": .87, "table": .8, "chair": .78, "person": .9, "bowl": .66})
# (day, "HH:MM", minutes, place)
SCHEDULE = [
    (0, "07:50", 20, HOME), (0, "09:00", 40, OFFICE), (0, "12:20", 30, FOOD_COURT), (0, "18:30", 20, HOME),
    (1, "07:50", 20, HOME), (1, "09:00", 40, OFFICE), (1, "17:15", 30, SUPERMARKET), (1, "18:50", 30, HOME),
]
# (day, "HH:MM:SS", text, speaker_id, speaker_is_wearer: True | False | None=unknown)
SPEECH = [
    (0, "07:55:00", "Remind me to buy milk at the supermarket.", "spk_wearer", True),
    (0, "09:10:00", "Marta has a presentation on Thursday at 9 AM.", "spk_1", False),
    (0, "09:12:00", "Lena lives in Lisbon.", "spk_1", False),
    (0, "12:30:00", "Alex is allergic to peanuts.", "spk_2", False),
    (0, "12:30:30", "Oh, I didn't know that.", "spk_wearer", True),
    (0, "12:35:00", "Sam is vegetarian.", None, None),
    (0, "12:40:00", "I really like the noodles at the food court", "spk_wearer", True),
    (1, "17:20:00", "I'm buying peanuts for Alex.", "spk_wearer", True),
    (1, "17:22:00", "I'm buying chocolate for Alex too.", "spk_wearer", True),
    (1, "17:24:00", "I'm buying peanuts for Jordan.", "spk_wearer", True),
    (1, "17:30:00", "I'm buying peanuts for Alex.", "spk_wearer", True),      # said again later: no repeat
    (1, "17:40:00", "I bought the milk", "spk_wearer", True),
    (1, "19:00:00", "I'll meet Marta on Thursday at 9am", "spk_wearer", True),
    (1, "19:10:00", "Lena moved to Berlin.", "spk_3", False),
]
CALENDAR: list[dict] = []


def _frames(day: int, seed: int) -> list[dict]:
    rnd = random.Random(seed * 100 + day)
    out = []
    for i, (d, start, minutes, (lat, lon, classes)) in enumerate(SCHEDULE):
        if d != day:
            continue
        t0 = at(d, start)
        for k in range(minutes * 60 // FRAME_EVERY_S):
            seen = [{"class": c, "count": 2 if c == "person" and rnd.random() < .3 else 1,
                     "confidence": round(min(.99, max(.5, p + rnd.uniform(-.06, .06))), 2)}
                    for c, p in classes.items() if rnd.random() > .08]
            out.append({"frame_id": f"m2d{d}v{i}f{k:04d}",
                        "timestamp": (t0 + timedelta(seconds=k * FRAME_EVERY_S)).isoformat(),
                        "classes": seen, "latitude": round(lat + rnd.uniform(-1e-4, 1e-4), 6),
                        "longitude": round(lon + rnd.uniform(-1e-4, 1e-4), 6)})
    return out


def _speech(day: int) -> list[dict]:
    out = []
    for n, (d, hhmm, text, speaker, wearer) in enumerate(SPEECH):
        if d != day:
            continue
        start = at(d, hhmm)
        seg = {"segment_id": f"m2seg{n:03d}", "recording_id": f"rec-m2-{start.date().isoformat()}",
               "start_time": start.isoformat(), "end_time": (start + timedelta(seconds=3)).isoformat(),
               "text": text, "confidence": 0.93}
        if speaker:
            seg["speaker_id"] = speaker
        if wearer is not None:
            seg["speaker_is_wearer"] = wearer
        out.append(seg)
    return out


def day_timeline(day: int, seed: int = 11) -> list[tuple[str, dict]]:
    """All perception records of one day in time order: [("video", frame) | ("audio", segment)]."""
    items = [("video", f) for f in _frames(day, seed)] + [("audio", s) for s in _speech(day)]
    return sorted(items, key=lambda kv: (kv[1].get("timestamp") or kv[1]["start_time"], kv[0]))


def day_end_ms(day: int) -> int:
    return int((DAY0 + timedelta(days=day, hours=23, minutes=59)).timestamp() * 1000)


def evening_ms(day: int) -> int:
    return int((DAY0 + timedelta(days=day, hours=21)).timestamp() * 1000)
