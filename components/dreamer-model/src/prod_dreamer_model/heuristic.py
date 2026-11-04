"""Deterministic dreamer.

The Muse loop is the product, but a dreamer that can return nothing when the
network hiccups is not production-safe. This module reads the same FalkorDB
graph and derives the same kinds of beliefs with plain rules, so the pipeline
always ends with evidence-linked patterns and long-term memories.

It also doubles as the reference answer: every statement it writes is one the
model may reproduce, refine or reject.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import mean
from typing import Any

from .tools import GraphToolBox

DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAYS = {0, 1, 2, 3, 4}
SENSITIVE_CLASSES = {
    "clinic": "clinic",
    "hospital": "hospital",
    "pharmacy": "pharmacy",
    "temple": "place of worship",
    "church": "place of worship",
    "mosque": "place of worship",
    "synagogue": "place of worship",
}


@dataclass(frozen=True)
class Signature:
    pattern_id: str
    class_name: str
    kind: str
    hour_lo: int
    hour_hi: int
    template: str
    about: tuple[str, ...]
    weekend: bool = False
    memory_kind: str = "routine"
    destination_class: str | None = None


SIGNATURES: list[Signature] = [
    Signature("morning-bus-commute", "bus interior", "commute", 5, 10,
              "Takes the bus in the morning around {time}", ("bus interior", "bus"),
              memory_kind="routine", destination_class="classroom"),
    Signature("morning-classes", "classroom", "schedule", 8, 12,
              "Attends classes on campus from about {time}", ("classroom", "projector"),
              memory_kind="schedule"),
    Signature("lunch-food-court", "food court", "routine", 11, 14,
              "Eats lunch at the food court around {time}", ("food court", "noodle"),
              memory_kind="routine"),
    Signature("afternoon-library", "library", "routine", 12, 18,
              "Studies in the library in the afternoon from about {time}", ("library", "book"),
              memory_kind="routine"),
    Signature("gym-sessions", "gym", "routine", 15, 20,
              "Goes to the gym in the late afternoon around {time}", ("gym", "dumbbell"),
              memory_kind="routine"),
    Signature("supermarket-run", "supermarket", "routine", 16, 21,
              "Does a supermarket run around {time}", ("supermarket", "shopping cart"),
              memory_kind="routine"),
    Signature("weekend-outdoors", "park", "activity", 8, 15,
              "Spends time outdoors in the morning from about {time}", ("park", "tree"),
              weekend=True, memory_kind="routine"),
    Signature("weekend-cafe", "cafe", "social", 9, 16,
              "Goes to a cafe around {time}", ("cafe", "coffee"),
              weekend=True, memory_kind="relationship"),
]

# pattern kind -> long-term memory kind
MEMORY_KIND_FOR = {
    "routine": "routine",
    "schedule": "schedule",
    "commute": "routine",
    "activity": "routine",
    "location": "place",
    "preference": "preference",
    "social": "relationship",
    "anomaly": "fact",
    "other": "fact",
}


def third_person(text: str) -> str:
    """'I really like the noodles.' -> 'Really likes the noodles.'"""
    words = text.strip().rstrip(".").split()
    if not words:
        return text
    if words[0].lower() != "i":
        return text.strip()
    rest = words[1:]
    if not rest:
        return text.strip()
    verb, tail = rest[0].lower(), rest[1:]
    mapping = {
        "like": "Likes", "love": "Loves", "hate": "Dislikes",
        "really": None,  # handled below
    }
    if verb == "really" and tail:
        inner = tail[0].lower()
        if inner in {"like", "love"}:
            return " ".join([f"Really {inner}s"] + tail[1:]) + "."
        if inner == "hate":
            return " ".join(["Really dislikes"] + tail[1:]) + "."
    if verb in mapping and mapping[verb]:
        return " ".join([mapping[verb]] + tail) + "."
    return " ".join(["User"] + rest) + "."


def hhmm(minutes: int) -> str:
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def describe_days(dows: list[int]) -> str:
    days = sorted({d for d in dows if 0 <= d <= 6})
    if not days:
        return ""
    if set(days) == WEEKDAYS:
        return "on weekdays"
    if set(days) == {5, 6}:
        return "at weekends"
    return "on " + ", ".join(DOW_NAMES[d] for d in days)


def _collect(
    frames: list[dict[str, Any]],
    class_name: str,
    hour_lo: int,
    hour_hi: int,
    weekend: bool,
) -> dict[str, dict[str, Any]]:
    """Per local date: first sighting of a class inside the hour window."""
    per_day: dict[str, dict[str, Any]] = {}
    for frame in frames:
        classes = frame.get("classes") or []
        if class_name not in classes:
            continue
        hour = frame.get("local_hour")
        minute = frame.get("local_min")
        dow = frame.get("dow")
        if hour is None or minute is None or dow is None:
            continue
        if not (hour_lo <= int(hour) <= hour_hi):
            continue
        is_weekend = int(dow) >= 5
        if is_weekend != weekend:
            continue
        day = frame.get("local_date")
        if not day:
            continue
        current = per_day.get(day)
        if current is None:
            per_day[day] = {
                "first_min": int(minute),
                "last_min": int(minute),
                "frame_id": frame.get("frame_id"),
                "dow": int(dow),
                "frames": 1,
            }
        else:
            current["last_min"] = max(current["last_min"], int(minute))
            current["frames"] += 1
            if int(minute) < current["first_min"]:
                current["first_min"] = int(minute)
                current["frame_id"] = frame.get("frame_id")
    return per_day


def _confidence(days: int) -> float:
    return round(min(0.95, days / (days + 2)), 2)


_PERSON = re.compile(r"(?i:\b(?:meeting|met|with)\s+)([A-Z][a-zA-Z]{1,20})")


def _named_person(text: str) -> str | None:
    match = _PERSON.search(text)
    return match.group(1) if match else None


def heuristic_dream(toolbox: GraphToolBox) -> dict[str, int]:
    graph = toolbox.graph
    frames = graph.fetch_frame_projection()
    notes = graph.fetch_notes()
    all_days = sorted({f["local_date"] for f in frames if f.get("local_date")})
    min_days = 2 if len(all_days) >= 5 else 1

    # Where the morning bus ends up, so the commute statement is useful.
    campus_days = _collect(frames, "classroom", 8, 12, weekend=False)
    campus_time = (
        hhmm(int(round(mean(v["first_min"] for v in campus_days.values()))))
        if campus_days else None
    )

    patterns: list[str] = []
    memories: list[str] = []
    strong: list[tuple[str, str, str, float]] = []  # id, memory_kind, statement, conf

    for sig in SIGNATURES:
        per_day = _collect(frames, sig.class_name, sig.hour_lo, sig.hour_hi, sig.weekend)
        if len(per_day) < min_days:
            continue
        dows = [v["dow"] for v in per_day.values()]
        times = [v["first_min"] for v in per_day.values()]
        avg = int(round(mean(times)))
        lo, hi = min(times) - 10, max(times) + 10
        template = sig.template
        if sig.destination_class and campus_time and sig.pattern_id == "morning-bus-commute":
            template = "Takes the bus to campus in the morning around {time}"
        statement = f"{template.format(time=hhmm(avg))} {describe_days(dows)}."
        confidence = _confidence(len(per_day))
        evidence = list(per_day.keys()) + [v["frame_id"] for v in per_day.values() if v["frame_id"]]
        result = graph.write_pattern(
            pattern_id=sig.pattern_id,
            statement=statement,
            kind=sig.kind,
            confidence=confidence,
            evidence=evidence[:12],
            about=list(sig.about),
            time_window=f"{hhmm(max(lo, 0))}-{hhmm(min(hi, 23 * 60 + 59))}",
            days=dows,
        )
        if result.get("ok"):
            patterns.append(result["pattern_id"])
            memory_kind = MEMORY_KIND_FOR.get(sig.kind, "fact")
            long_statement = statement
            if memory_kind == "routine" and campus_time and sig.pattern_id == "morning-bus-commute":
                long_statement = (
                    f"{statement[:-1]}, arriving on campus around {campus_time}."
                )
            strong.append((result["pattern_id"], memory_kind, long_statement, confidence))

    # One-off / sensitive events: recorded as anomalies, never as habits.
    for class_name, label in SENSITIVE_CLASSES.items():
        per_day = _collect(frames, class_name, 0, 23, weekend=False) | _collect(
            frames, class_name, 0, 23, weekend=True
        )
        if not per_day:
            continue
        pid = f"one-off-{class_name}"
        statement = (
            f"Was seen at a {label} on {len(per_day)} day(s) — a one-off, "
            "not part of a routine."
        )
        result = graph.write_pattern(
            pattern_id=pid,
            statement=statement,
            kind="anomaly",
            confidence=0.3,
            evidence=list(per_day.keys()) + [v["frame_id"] for v in per_day.values() if v["frame_id"]],
            about=[class_name],
        )
        if result.get("ok"):
            patterns.append(pid)

    # Promote supported patterns into long-term memory.
    for pid, memory_kind, statement, confidence in strong:
        mid = f"ltm-{pid}"
        result = graph.write_memory(
            memory_id=mid,
            statement=statement,
            kind=memory_kind,
            confidence=confidence,
            evidence=[],
            from_patterns=[pid],
        )
        if result.get("ok"):
            memories.append(mid)

    # What the user actually said.
    for note in notes:
        kind = note.get("kind")
        text = (note.get("text") or "").strip()
        if not text:
            continue
        note_id = note.get("note_id")
        if kind == "preference":
            statement = third_person(text)
            pid = f"pref-{note_id}"
            graph.write_pattern(
                pattern_id=pid, statement=statement, kind="preference", confidence=0.9,
                evidence=[note_id], about=[],
            )
            patterns.append(pid)
            mid = f"ltm-pref-{note_id}"
            graph.write_memory(
                memory_id=mid, statement=statement, kind="preference", confidence=0.95,
                evidence=[note_id], from_patterns=[pid], about=[],
            )
            memories.append(mid)
        elif kind == "reminder":
            mid = f"ltm-reminder-{note_id}"
            graph.write_memory(
                memory_id=mid, statement=text, kind="reminder", confidence=0.9,
                evidence=[note_id], about=[],
            )
            memories.append(mid)
        else:
            person = _named_person(text)
            if person:
                mid = f"ltm-social-{note_id}"
                graph.write_memory(
                    memory_id=mid,
                    statement=third_person(text),
                    kind="relationship",
                    confidence=0.8,
                    evidence=[note_id],
                    about=[],
                )
                memories.append(mid)

    return {
        "patterns": len(patterns),
        "memories": len(memories),
        "days": len(all_days),
        "frames": len(frames),
        "notes": len(notes),
    }
