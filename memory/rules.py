"""Deterministic rules: visit tags -> place type and activity.

Cue words are class names that exist in yolomodelvision/classes.txt.
Scene cues (e.g. "food court") weigh more than object cues (e.g. "plate").
"""

from __future__ import annotations

# place_type -> (scene cues, object cues)
PLACE_CUES: dict[str, tuple[set[str], set[str]]] = {
    "food_court": ({"food court", "canteen"}, {"plate", "bowl", "noodle", "meal", "food", "tray"}),
    "restaurant": ({"restaurant"}, {"menu", "wine glass"}),
    "cafe": ({"cafe", "coffee shop"}, {"coffee", "latte", "espresso"}),
    "classroom": ({"classroom", "lecture room", "auditorium"}, {"whiteboard", "blackboard", "projector"}),
    "library": ({"library"}, {"bookcase", "bookshelf"}),
    "lab": ({"laboratory", "chemistry lab", "computer room"}, {"microscope", "beaker"}),
    "office": ({"office"}, {"office chair", "cubicle"}),
    "gym": ({"gym"}, {"dumbbell", "treadmill", "barbell"}),
    "supermarket": ({"supermarket"}, {"shopping cart", "shopping basket"}),
    "shop": ({"mall", "store"}, {"shopping bag"}),
    "home": ({"bedroom", "living room", "kitchen", "home"}, {"bed", "pillow"}),
    "transit": ({"bus interior", "bus stop", "bus station", "subway station", "subway"},
                {"bus", "train", "steering wheel"}),
    "outdoors": ({"park", "beach", "playground"}, {"tree", "grass"}),
    "healthcare": ({"hospital", "clinic", "pharmacy"}, set()),
    "worship": ({"church", "mosque", "temple"}, set()),
}

# Visits to these are stored but never turned into inferred habits.
SENSITIVE_PLACE_TYPES = {"healthcare", "worship"}

ACTIVITY_CUES: dict[str, set[str]] = {
    "eating": {"plate", "bowl", "noodle", "meal", "food", "fast food", "chopstick", "fork", "spoon"},
    "working": {"laptop", "computer", "keyboard", "monitor", "book", "notebook", "pen", "paper"},
    "exercising": {"dumbbell", "treadmill", "barbell"},
}
PLACE_ACTIVITY = {
    "food_court": "eating", "restaurant": "eating", "cafe": "eating",
    "classroom": "studying", "library": "studying", "lab": "studying", "office": "working",
    "gym": "exercising", "supermarket": "shopping", "shop": "shopping",
    "transit": "commuting", "home": "at_home",
}

SCENE_WEIGHT = 1.0
OBJECT_WEIGHT = 0.4
MIN_PLACE_SCORE = 0.3


def classify_place(tags: dict[str, float]) -> str:
    """tags: class -> fraction of the visit's frames it appeared in."""
    best, best_score = "unknown", 0.0
    for ptype, (scenes, objects) in PLACE_CUES.items():
        score = SCENE_WEIGHT * max((tags.get(c, 0.0) for c in scenes), default=0.0)
        score += OBJECT_WEIGHT * sum(tags.get(c, 0.0) for c in objects)
        if score > best_score:
            best, best_score = ptype, score
    return best if best_score >= MIN_PLACE_SCORE else "unknown"


def classify_activity(tags: dict[str, float], place_type: str) -> str:
    scores = {a: sum(tags.get(c, 0.0) for c in cues) for a, cues in ACTIVITY_CUES.items()}
    act, score = max(scores.items(), key=lambda kv: kv[1])
    if score >= 0.5:
        # a laptop in a classroom or library means studying, not "working"
        if act == "working" and PLACE_ACTIVITY.get(place_type) == "studying":
            return "studying"
        return act
    return PLACE_ACTIVITY.get(place_type, "unknown")
