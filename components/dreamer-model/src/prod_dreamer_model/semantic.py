"""Semantic extraction: structured claims from verified utterances (``vixy.semantic.v2``).

A *claim* is one thing an utterance asserts, in a fixed shape::

    subject    who it is about (a named person, or the wearer: "user")
    predicate  allergic_to | diet | avoids | likes | dislikes | relationship |
               lives_in | works_at | studies_at | has_event | states
    object     the value ("peanut", "vegetarian", "sister", "presentation", ...)
    polarity   False for "Alex is not allergic to peanuts"
    hedged     "I think", "maybe", "apparently" ... -> an uncertain hypothesis
    change     explicit change-over-time markers ("moved to", "now", "anymore")
    event time for plans, resolved against the moment the utterance was said

Claims say nothing about truth. Who said it (wearer / someone else / unknown) is
attached by the consolidation step from the Note, and decides the epistemic
status: another person's statement is *reported*, never verified fact.

The rules are deliberately generic (no names or phrases from the demo data);
anything they do not recognise but that still states something about a named
person falls back to the Milestone 1 rule (predicate ``states``). An optional
model extractor (``model_extract.py``) can add claims the rules miss; its
output goes through the same validation and is labelled with the model id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

EXTRACTOR_VERSION = "vixy.semantic.v2"
RULES_EXTRACTOR = f"rules:{EXTRACTOR_VERSION}"

# predicate -> how it is stored and compared
#   kind        LongTermMemory.kind
#   functional  one current value per subject (a different value is a change or a conflict)
#   caution     relevant when someone is about to act against it (agent recall)
PREDICATES: dict[str, dict[str, Any]] = {
    "allergic_to": {"kind": "fact", "category": "health", "functional": False, "caution": True},
    "diet": {"kind": "fact", "category": "diet", "functional": False, "caution": True},
    "avoids": {"kind": "preference", "category": "diet", "functional": False, "caution": True},
    "likes": {"kind": "preference", "category": "preference", "functional": False, "caution": False},
    "dislikes": {"kind": "preference", "category": "preference", "functional": False, "caution": True},
    "relationship": {"kind": "relationship", "category": "relationship", "functional": True, "caution": False},
    "lives_in": {"kind": "fact", "category": "location", "functional": True, "caution": False},
    "works_at": {"kind": "fact", "category": "work", "functional": True, "caution": False},
    "studies_at": {"kind": "fact", "category": "education", "functional": True, "caution": False},
    "has_event": {"kind": "schedule", "category": "plan", "functional": True, "caution": False},
    "states": {"kind": "fact", "category": "statement", "functional": False, "caution": False},
}

NOT_A_NAME = {
    "I", "The", "This", "That", "These", "Those", "It", "He", "She", "They", "We", "You",
    "There", "Here", "What", "Who", "Where", "When", "Why", "How", "Which", "Everyone",
    "Someone", "Nobody", "Everybody", "Somebody", "Today", "Tomorrow", "Yesterday",
    "Tonight", "Vixy", "Veyra", "Hey", "Ok", "Okay", "Yes", "No", "Let", "Remind", "Maybe",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December", "And", "But", "So", "Also", "Well",
    "Oh", "Apparently", "Actually", "Just", "By", "If", "Did", "Does", "Is", "Do", "My",
    "Our", "His", "Her", "Their", "Please", "Thanks", "Sorry", "Good", "Great", "AM", "PM",
    "Anyway", "Remember", "Look", "Listen", "Wait", "Guess", "Btw", "Fyi", "Note", "Now", "Then", "Still",
}


def _is_name_word(word: str) -> bool:
    # sentence-initial adverbs ("Honestly", "Apparently") are capitalized but are not names
    return word not in NOT_A_NAME and not (word.endswith("ly") and len(word) > 4)
_REL = (r"sister|brother|mother|father|mom|mum|dad|son|daughter|wife|husband|partner|girlfriend|"
        r"boyfriend|friend|best friend|colleague|coworker|co-worker|boss|manager|roommate|flatmate|"
        r"cousin|aunt|uncle|grandmother|grandfather|grandma|grandpa|neighbour|neighbor|teacher|"
        r"doctor|classmate|niece|nephew")
_EVENT = (r"presentation|meeting|interview|exam|test|appointment|birthday|party|flight|class|lecture|"
          r"wedding|dinner|lunch|game|match|recital|concert|surgery|deadline|review|demo|talk|"
          r"performance|trip|session|call|deposition|hearing|shift")
_DIETS = (r"vegetarian|vegan|pescatarian|gluten[- ]free|lactose[- ]intolerant|halal|kosher|diabetic|"
          r"celiac|coeliac|teetotal|sober")
_NAME_WORD = r"[A-Z][a-z]{1,20}"
_S = rf"(?P<subj>\bI\b|{_NAME_WORD}(?:\s{_NAME_WORD})?)"
_BE = r"(?:\s+(?:is|am|was|has\s+been)|\s*'s|\s*'m)"
_NOTBE = r"(?:\s+(?:is|am|was)\s+not|\s+isn't|\s+wasn't|\s*'s\s+not|\s*'m\s+not|\s+is\s+no\s+longer|\s*'s\s+no\s+longer)"
_CLAUSE = r"(?P<obj>[^,.;!?]+)"
_HEDGE = re.compile(r"\b(i think|i believe|maybe|probably|might|perhaps|apparently|i heard|rumou?r|not sure|"
                    r"supposedly|i guess|seems|could be)\b", re.I)
_CHANGE = re.compile(r"\b(now|anymore|any more|no longer|moved|moving|rescheduled|changed|switched|instead|"
                     r"actually|recently|these days|pushed|postponed|started|just)\b", re.I)
_SKIP_START = re.compile(r"^(?:if|whether|does|do|did|is|are|was|were|can|could|should|would|will|remind me|"
                         r"don't forget|dont forget|vixy|veyra|hey vixy|ok vixy)\b", re.I)
_LEAD = re.compile(r"^(?:(?:okay|ok|so|oh|well|and|also|btw|by the way|fyi|just so you know|remember|"
                   r"apparently|actually|i heard|i think|i believe|um|uh)[,\s]+)+", re.I)
_OBJ_STOP = {"a", "an", "the", "some", "any", "very", "really", "all", "of", "to"}
_OBJ_END = re.compile(r"\s+(?:anymore|any more|either|too|as well|anyway|because|which|who|when|since|if|but|so|though|although|from|"
                      r"this|next|tomorrow|today|tonight|yesterday|on|at|in|for)\b.*$", re.I)


@dataclass(frozen=True)
class Mention:
    """A person as named in one utterance. ``first == 'user'`` means the wearer."""

    first: str
    last: str | None = None

    @property
    def is_user(self) -> bool:
        return self.first == "user"

    @property
    def surface(self) -> str:
        return "user" if self.is_user else f"{self.first} {self.last}" if self.last else self.first


USER = Mention("user")


@dataclass
class Claim:
    note_id: str
    subject: Mention
    predicate: str
    object: str                      # normalized value (lowercase, singular for food-like objects)
    object_text: str                 # as said
    statement: str                   # the sentence it came from
    polarity: bool = True
    hedged: bool = False
    change: bool = False
    event_start_ms: int | None = None
    event_has_time: bool = False
    time_text: str | None = None
    object_person: Mention | None = None
    extracted_by: str = RULES_EXTRACTOR

    @property
    def spec(self) -> dict[str, Any]:
        return PREDICATES[self.predicate]

    def as_dict(self) -> dict[str, Any]:
        return {"note_id": self.note_id, "subject": self.subject.surface, "predicate": self.predicate,
                "object": self.object, "object_text": self.object_text, "statement": self.statement,
                "polarity": self.polarity, "hedged": self.hedged, "change": self.change,
                "event_start_ms": self.event_start_ms, "event_has_time": self.event_has_time,
                "time_text": self.time_text, "extracted_by": self.extracted_by,
                "object_person": self.object_person.surface if self.object_person else None}


# --------------------------------------------------------------------------- text helpers
def sentences(text: str) -> list[str]:
    text = text.replace("’", "'").strip()
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def norm_text(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split())


def stem(word: str) -> str:
    """Tiny, predictable singularizer: peanuts -> peanut, berries -> berry, dishes -> dish."""
    w = word.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ches", "shes", "sses", "xes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def norm_object(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9'-]*", text.lower()) if w not in _OBJ_STOP]
    return " ".join(stem(w) for w in words[:4])


def _mention(subj: str) -> Mention | None:
    if subj == "I":
        return USER
    parts = subj.split()
    if not _is_name_word(parts[0]):         # "Today Alex ..." / "Honestly Kai ..." -> the second word
        parts = parts[1:]
        if not parts or not _is_name_word(parts[0]):
            return None
    last = parts[1] if len(parts) > 1 and _is_name_word(parts[1]) else None
    return Mention(parts[0], last)


def _statement(sentence: str) -> str:
    s = sentence[0].upper() + sentence[1:]
    return s if s[-1] in ".!" else s + "."


def _split_objects(clause: str) -> list[str]:
    clause = _OBJ_END.sub("", clause.strip())
    parts = re.split(r"\s*,\s*|\s+(?:and|or|&)\s+", clause)
    out = []
    for p in parts:
        p = p.strip(" '\"")
        if p and len(p.split()) <= 4 and norm_object(p):
            out.append(p)
    return out


def _place(clause: str) -> str | None:
    clause = re.sub(r"\s+(?:because|which|who|when|since|but|so|now|these days|nowadays)\b.*$", "",
                    clause.strip(), flags=re.I).strip(" '\"")
    words = clause.split()
    return " ".join(words[:5]) if words else None


# --------------------------------------------------------------------------- time
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"]
_TIME = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])|\b(\d{1,2}):(\d{2})\b|"
                   r"\bat\s+(\d{1,2})\b(?!\s*(?:%|percent|people|minutes|hours))|\b(noon|midday|midnight)\b", re.I)


def resolve_time(text: str, ref: datetime) -> tuple[datetime | None, bool, str | None]:
    """(start, has_time, matched text) for the first date/time phrase in ``text``, read
    relative to ``ref`` (the moment the utterance was said, local time). Day without a
    time -> local midnight with has_time False. Nothing found -> (None, False, None)."""
    low = text.lower()
    day = None
    spans: list[str] = []
    if m := re.search(r"\b(day after tomorrow|tomorrow|tonight|today|this (?:morning|afternoon|evening))\b", low):
        day = ref.date() + timedelta(days=2 if m.group(1) == "day after tomorrow" else
                                     1 if m.group(1) == "tomorrow" else 0)
        spans.append(m.group(0))
    elif m := re.search(r"\b(?:(next|this|on|coming)\s+)?(" + "|".join(_WEEKDAYS) + r")\b", low):
        ahead = (_WEEKDAYS.index(m.group(2)) - ref.weekday()) % 7
        if ahead == 0 and m.group(1) != "this":
            ahead = 7
        day = ref.date() + timedelta(days=ahead)
        spans.append(m.group(0))
    elif m := re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS) + r")\b|\b(" + "|".join(_MONTHS)
                        + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", low):
        dnum, mon = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        try:
            cand = ref.date().replace(month=_MONTHS.index(mon) + 1, day=int(dnum))
        except ValueError:
            cand = None
        if cand is not None:
            if cand < ref.date() - timedelta(days=60):
                cand = cand.replace(year=cand.year + 1)
            day = cand
            spans.append(m.group(0))
    hour = minute = None
    if t := _TIME.search(text):
        if t.group(1):
            hour, minute = int(t.group(1)) % 12, int(t.group(2) or 0)
            if t.group(3).lower().startswith("p"):
                hour += 12
        elif t.group(4):
            hour, minute = int(t.group(4)), int(t.group(5))
        elif t.group(6):
            hour, minute = int(t.group(6)), 0
            if 1 <= hour <= 7:
                hour += 12                            # "at 6" -> evening
        else:
            hour, minute = (0, 0) if t.group(7).lower() == "midnight" else (12, 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            hour = minute = None
        else:
            spans.append(t.group(0).strip())
    if day is None and hour is None:
        return None, False, None
    if day is None:
        day = ref.date() if (hour, minute) > (ref.hour, ref.minute) else ref.date() + timedelta(days=1)
    start = datetime(day.year, day.month, day.day, hour or 0, minute or 0, tzinfo=ref.tzinfo)
    return start, hour is not None, " ".join(spans)


# --------------------------------------------------------------------------- rules
def _p(pattern: str) -> re.Pattern:
    return re.compile(pattern.replace("{S}", _S))


# (predicate, pattern, options). Case-sensitive on purpose: a lowercase word is never a name.
_RULES: list[tuple[str, re.Pattern, dict[str, Any]]] = [
    ("allergic_to", _p(rf"{{S}}{_NOTBE}\s+(?:(?:actually|really)\s+)?allergic\s+to\s+{_CLAUSE}"), {"polarity": False}),
    ("allergic_to", _p(rf"{{S}}{_BE}\s+(?:(?:very|severely|really|super|deathly|quite|seriously)\s+)?allergic\s+to\s+{_CLAUSE}"), {}),
    ("allergic_to", _p(rf"{{S}}\s+(?:has|have|'s got)\s+(?:an?\s+)?(?:(?:severe|serious|bad|mild)\s+)?(?P<obj>[a-z]+(?:\s[a-z]+)?)\s+allergy"), {}),
    ("diet", _p(rf"{{S}}{_NOTBE}\s+(?:a\s+|an\s+)?(?:strict\s+)?(?P<obj>{_DIETS})\b"), {"polarity": False}),
    ("diet", _p(rf"{{S}}{_BE}\s+(?:(?:now|still)\s+)?(?:a\s+|an\s+)?(?:strict\s+)?(?P<obj>{_DIETS})\b"), {}),
    ("avoids", _p(rf"{{S}}\s+(?:doesn't|does not|don't|do not|can't|cannot|won't|never)\s+(?:eat|drink|touch)s?\s+{_CLAUSE}"), {}),
    ("dislikes", _p(rf"{{S}}\s+(?:really\s+)?(?:hates|dislikes|can't stand|cannot stand|doesn't like|does not like|"
                    rf"hate|dislike|don't like|do not like)\s+{_CLAUSE}"), {"third_person_only": True}),
    ("likes", _p(rf"{{S}}\s+(?:really\s+|absolutely\s+)?(?:likes|loves|enjoys|adores|prefers|is into|is a fan of|"
                 rf"is a big fan of)\s+{_CLAUSE}"), {"third_person_only": True}),
    # "my sister" is the *speaker's* sister: a relationship to the user only when the wearer said it
    ("relationship", _p(rf"{{S}}\s+is\s+my\s+(?P<obj>{_REL})\b"), {"third_person_only": True, "wearer_only": True}),
    ("relationship", _p(rf"\b[Mm]y\s+(?P<obj>{_REL})\s+(?P<subj>{_NAME_WORD}(?:\s{_NAME_WORD})?)"),
     {"third_person_only": True, "wearer_only": True}),
    ("relationship", _p(rf"{{S}}\s+is\s+(?P<other>{_NAME_WORD})'s\s+(?P<obj>{_REL})\b"), {"third_person_only": True}),
    ("lives_in", _p(rf"{{S}}\s+(?:(?:has\s+)?(?:just\s+|recently\s+)?moved|is moving|relocated|has relocated)\s+to\s+(?P<obj>[^,.;!?]+)"),
     {"change": True, "place": True}),
    ("lives_in", _p(rf"{{S}}\s+(?:(?:now|still|currently)\s+)?(?:lives|is living|is staying|live)\s+in\s+(?P<obj>[^,.;!?]+)"), {"place": True}),
    ("works_at", _p(rf"{{S}}\s+(?:(?:has\s+)?(?:just\s+|recently\s+)?started|got a job|is starting)\s+(?:working\s+|a job\s+)?(?:at|for|with)\s+(?P<obj>[^,.;!?]+)"),
     {"change": True, "place": True}),
    ("works_at", _p(rf"{{S}}\s+(?:(?:now|still|currently)\s+)?(?:works|work|is working)\s+(?:at|for)\s+(?P<obj>[^,.;!?]+)"), {"place": True}),
    ("studies_at", _p(rf"{{S}}\s+(?:(?:now|still|currently)\s+)?(?:studies|study|is studying|goes to school)\s+at\s+(?P<obj>[^,.;!?]+)"), {"place": True}),
    ("has_event", _p(rf"{{S}}'s\s+(?P<obj>{_EVENT})\s+(?:is|was|has been|got|is now|was just)\s+(?P<moved>(?:moved|rescheduled|pushed|postponed|changed|brought forward)\s+(?:back\s+)?(?:to\s+)?)?(?P<rest>[^.;!?]*)"),
     {"third_person_only": True, "event": True}),
    ("has_event", _p(rf"{{S}}(?:\s+has|\s+has got|'s got|\s+will have|'ll have|\s+is giving|\s+is having|\s+is doing)\s+(?:a|an|her|his|their|the|its)\s+"
                     rf"(?:(?:big|important|final|first|job)\s+)?(?P<obj>{_EVENT})\b(?P<rest>[^.;!?]*)"),
     {"third_person_only": True, "event": True}),
    ("has_event", _p(rf"{{S}}\s+is\s+(?:coming|arriving|visiting|flying in|landing|coming over)\b(?P<rest>[^.;!?]*)"),
     {"third_person_only": True, "event": True, "fixed_object": "visit"}),
]


def _lead_stripped(sentence: str) -> str:
    return _LEAD.sub("", sentence).strip()


def extract_claims(note_id: str, text: str, said_at: datetime, *, speaker_is_wearer: bool = False) -> list[Claim]:
    """Structured claims in one utterance, rules only. ``said_at`` is local time.

    A first-person claim ("I'm allergic to ...") is about the wearer only when the
    wearer said it; from anyone else it is skipped (we don't know whose "I" it is)."""
    out: list[Claim] = []
    for raw in sentences(text):
        if raw.endswith("?"):
            continue
        body = _lead_stripped(raw)
        if not body or _SKIP_START.match(body):
            continue
        hedged = bool(_HEDGE.search(raw))
        found: list[Claim] = []
        taken: set[tuple[str, str]] = set()
        for predicate, rx, opts in _RULES:
            for m in rx.finditer(body):
                subj = _mention(m.group("subj"))
                if subj is None or (subj.is_user and (opts.get("third_person_only") or not speaker_is_wearer)):
                    continue
                if opts.get("wearer_only") and not speaker_is_wearer:
                    continue
                if (subj.surface, predicate) in taken and predicate != "has_event":
                    continue
                change = bool(opts.get("change")) or bool(_CHANGE.search(body))
                polarity = opts.get("polarity", True)
                base = {"note_id": note_id, "subject": subj, "predicate": predicate, "statement": _statement(raw),
                        "polarity": polarity, "hedged": hedged, "change": change}
                if opts.get("event"):
                    obj = opts.get("fixed_object") or m.group("obj").lower()
                    rest = m.groupdict().get("rest") or ""
                    start, has_time, ttext = resolve_time(rest, said_at)
                    moved = bool(m.groupdict().get("moved"))
                    found.append(Claim(**{**base, "change": moved or bool(re.search(r"\b(now|instead|moved|rescheduled)\b", rest, re.I))},
                                       object=obj, object_text=obj, time_text=ttext,
                                       event_start_ms=int(start.timestamp() * 1000) if start else None,
                                       event_has_time=has_time))
                elif opts.get("place"):
                    place = _place(m.group("obj"))
                    if place:
                        found.append(Claim(**base, object=place.lower(), object_text=place))
                elif predicate == "relationship":
                    other = m.groupdict().get("other")
                    other_m = _mention(other) if other else None
                    rel = m.group("obj").lower()
                    found.append(Claim(**base, object=f"{rel} of {other_m.surface}" if other_m else rel,
                                       object_text=rel, object_person=other_m))
                else:
                    for obj in _split_objects(m.group("obj")):
                        found.append(Claim(**base, object=norm_object(obj), object_text=obj))
                taken.add((subj.surface, predicate))
        if not found:
            # Milestone 1 rule: any other declarative statement about a named person
            for name, statement in person_statements(body):
                subj = _mention(name)
                if subj and not subj.is_user:
                    found.append(Claim(note_id=note_id, subject=subj, predicate="states",
                                       object=norm_text(statement), object_text=statement,
                                       statement=_statement(raw), hedged=hedged))
        out += found
    return out


# --------------------------------------------------------------------------- Milestone 1 rule
_PREDICATE_WORDS = (
    r"is|was|has|had|likes|loves|hates|prefers|enjoys|works|lives|studies|needs|wants|"
    r"will|won't|can't|cannot|doesn't|does not|isn't|is not|owns|speaks|plays|drives"
)
_PERSON_STATEMENT = re.compile(
    rf"(?<![A-Za-z'])([A-Z][a-z]{{1,20}})(?:'s\s+[a-z]+(?:\s+[a-z]+)?)?\s+(?:{_PREDICATE_WORDS})\b\s+\S+"
)


def person_statements(text: str) -> list[tuple[str, str]]:
    """(name, sentence) for each declarative sentence about a named person (M1 rule)."""
    out = []
    for sentence in sentences(text):
        if sentence.endswith("?"):
            continue
        for match in _PERSON_STATEMENT.finditer(sentence):
            name = match.group(1)
            if name in NOT_A_NAME:
                continue
            out.append((name, _statement(sentence)))
            break  # one subject per sentence
    return out


def local_time(ts_ms: int, tz: str) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, ZoneInfo(tz))


@dataclass
class ExtractionStats:
    notes: int = 0
    rule_claims: int = 0
    model_claims: int = 0
    model_rejected: int = 0
    model_calls: int = 0
    model_errors: list[str] = field(default_factory=list)
    model_cached_notes: int = 0
