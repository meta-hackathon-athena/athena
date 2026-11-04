"""Memory-aware recall: is something remembered from an earlier day relevant right now?

Runs on utterances only (never per frame), after the existing rules have decided:

  1. cheap gate      the utterance must name a known person (or touch one of the
                     wearer's own caution memories) -- an in-memory index, no query
  2. retrieval       bounded graph retrieval of that person's memories (vixy.retrieval)
  3. rules           deterministic relevance:
                       caution   someone is about to buy/cook/bring/give X for Alex, and a
                                 memory says Alex is allergic to / avoids / dislikes /
                                 can't have X (diet and allergen families included)
                       plan      a plan with Alex at a time that overlaps something
                                 Alex was said to have then
  4. reasoner        only for a *possibly* relevant memory the rules can't settle,
                     and only if a model is configured (budgeted per hour); it may
                     pick NOTIFY or NO_ACTION for one of the candidate ids, nothing else
  5. message         always rendered here from the memory's stored fields, keeping its
                     attribution ("Someone mentioned yesterday that ..."), its doubts
                     (contested, hypothesis, ambiguous person) and "not verified"
                     for reported speech. A model never writes user-facing text.

Decisions then go through the normal NotificationGate (dedup key ``recall:<memory id>``,
cooldown, rate limits), so a recall can never bypass the anti-spam policy.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from memory.store import TZ

from .memory_tools import MemoryUnavailable
from .models import Action, AudioObservation, Decision, Priority, no_action

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR
RECALL_VERSION = "rules:vixy.recall.v1"

_ACTIONS = {
    "buy": "buying", "buying": "buying", "bought": "buying", "get": "getting", "getting": "getting",
    "grab": "getting", "grabbing": "getting", "order": "ordering", "ordering": "ordering", "cook": "cooking",
    "cooking": "cooking", "bake": "baking", "baking": "baking", "make": "making", "making": "making",
    "bring": "bringing", "bringing": "bringing", "serve": "serving", "serving": "serving", "gift": "giving it",
    "give": "giving it", "giving": "giving it", "feed": "giving it", "pick": "getting", "picking": "getting",
    "take": "taking it", "taking": "taking it", "prepare": "preparing", "preparing": "preparing",
    "treat": "getting", "surprise": "getting",
}
_ACTION = re.compile(r"\b(" + "|".join(sorted(_ACTIONS, key=len, reverse=True)) + r")\b", re.I)
MEAT = {"meat", "chicken", "beef", "pork", "bacon", "ham", "steak", "burger", "sausage", "lamb", "turkey",
        "salami", "pepperoni", "duck", "veal", "mutton", "jerky", "brisket", "rib", "wing", "nugget", "meatball"}
FISH = {"fish", "salmon", "tuna", "cod", "sushi", "shrimp", "prawn", "crab", "lobster", "anchovy", "sardine"}
DAIRY = {"milk", "cheese", "butter", "cream", "yogurt", "yoghurt", "ice", "dairy", "latte", "cheesecake"}
GLUTEN = {"bread", "pasta", "wheat", "flour", "cake", "cookie", "pizza", "noodle", "beer", "bagel", "croissant", "biscuit"}
ALCOHOL = {"beer", "wine", "vodka", "whisky", "whiskey", "rum", "gin", "champagne", "cocktail", "alcohol", "sake", "soju"}
# what a stated diet/allergy rules out (beyond the literal word)
FAMILIES: dict[str, set[str]] = {
    "vegetarian": MEAT | FISH, "vegan": MEAT | FISH | DAIRY | {"egg", "honey", "gelatin"},
    "pescatarian": MEAT, "lactose intolerant": DAIRY, "lactose-intolerant": DAIRY, "gluten free": GLUTEN,
    "gluten-free": GLUTEN, "celiac": GLUTEN, "coeliac": GLUTEN, "halal": {"pork", "bacon", "ham"} | ALCOHOL,
    "kosher": {"pork", "bacon", "ham", "shrimp", "prawn", "crab", "lobster"}, "teetotal": ALCOHOL, "sober": ALCOHOL,
    "nut": {"peanut", "almond", "cashew", "walnut", "hazelnut", "pecan", "pistachio", "macadamia", "nutella"},
    "tree nut": {"almond", "cashew", "walnut", "hazelnut", "pecan", "pistachio", "macadamia"},
    "shellfish": {"shrimp", "prawn", "crab", "lobster", "oyster", "mussel", "clam", "scallop"},
    "seafood": FISH | {"oyster", "mussel", "clam", "scallop", "squid", "octopus"},
    "dairy": DAIRY, "gluten": GLUTEN, "egg": {"egg", "omelette", "mayonnaise", "meringue"},
    "peanut": {"peanut", "satay", "nutella"},
}


def _stems(text: str) -> set[str]:
    from prod_dreamer_model.semantic import stem

    return {stem(w) for w in re.findall(r"[a-z][a-z'-]+", text.lower())}


def object_matches(memory: dict, utterance_stems: set[str]) -> list[str]:
    """Words of the utterance that the memory's object rules out (literal or by family)."""
    obj = str(memory.get("object") or "").lower()
    words = set(obj.split())
    hits = sorted(words & utterance_stems)
    family = FAMILIES.get(obj) or FAMILIES.get(obj.rstrip("s")) or set()
    if memory.get("predicate") in ("allergic_to", "avoids", "dislikes", "diet"):
        hits += sorted((family & utterance_stems) - set(hits))
    return hits


@dataclass
class RecallConfig:
    index_ttl_ms: int = 10 * MIN           # how often the cheap name index is refreshed (observation time)
    max_memories: int = 8
    plan_overlap_ms: int = 90 * MIN
    reasoner_max_per_hour: int = 6
    expires_ms: int = 30 * MIN


def _day_phrase(then_ms: int, now_ms: int) -> str:
    then, now = datetime.fromtimestamp(then_ms / 1000, TZ), datetime.fromtimestamp(now_ms / 1000, TZ)
    days = (now.date() - then.date()).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"on {then.strftime('%A')}"
    return f"on {then.strftime('%-d %b')}"


def _when(ms: int, has_time: bool) -> str:
    t = datetime.fromtimestamp(ms / 1000, TZ)
    day = t.strftime("%A %-d %b")
    return f"on {day} at {t.strftime('%H:%M')}" if has_time else f"on {day}"


def claim_text(m: dict) -> str:
    """The memory as a clause, from its structured fields (falls back to its statement)."""
    s = m.get("subject") or ""
    you = s == "user"
    who = "you" if you else s
    be = "are" if you else "is"
    obj = m.get("object_text") or m.get("object") or ""
    p, pos = m.get("predicate"), m.get("polarity") is not False
    if p == "allergic_to":
        return f"{who} {be}{'' if pos else ' not'} allergic to {obj}"
    if p == "diet":
        return f"{who} {be}{'' if pos else ' not'} {obj}"
    if p == "avoids":
        return f"{who} {'do' if you else 'does'}n't eat {obj}"
    if p == "dislikes":
        return f"{who} {'do' if you else 'does'}n't like {obj}"
    if p == "likes":
        return f"{who} like{'' if you else 's'} {obj}"
    if p == "has_event" and m.get("event_start_ms"):
        return f"{who} {'have' if you else 'has'} {'an' if obj[:1] in 'aeiou' else 'a'} {obj} " + \
               _when(m["event_start_ms"], bool(m.get("event_has_time")))
    return str(m.get("statement") or "").strip().rstrip(".!")


def attribution(m: dict) -> str:
    roles = m.get("reported_by") or []
    if m.get("epistemic_status") == "hypothesis":
        return "Someone thought" if roles != ["wearer"] else "You thought"
    return "You mentioned" if roles == ["wearer"] else "Someone mentioned"


def render(m: dict, now_ms: int, *, kind: str, action: str | None = None, ambiguous: bool = False) -> str:
    first = min([e.get("ts_ms") for e in m.get("evidence") or [] if e.get("ts_ms")] or [m.get("first_stated_ms") or now_ms])
    subject = m.get("subject") or "them"
    msg = f"{attribution(m)} {_day_phrase(first, now_ms)} that {claim_text(m)}"
    if ambiguous:
        msg += f" (if it's the same {subject})"
    msg += "."
    if m.get("contested"):
        msg += " Someone else said otherwise."
    elif m.get("epistemic_status") in ("reported", "hypothesis") and m.get("reported_by") != ["wearer"]:
        msg += " I haven't verified it."
    if kind == "caution":
        target = "yourself" if subject == "user" else subject
        msg += (f" You may want to double-check before {action or 'going ahead'}." if subject == "user"
                else f" You may want to check with {target} before {action or 'going ahead'}.")
    elif kind == "plan":
        msg += f" {subject} may not be free then."
    elif kind == "possible":
        msg += " It might matter here."
    return msg


def summary(m: dict) -> dict:
    """What an AgentLog / API keeps about a supporting memory."""
    return {"id": m["id"], "statement": m.get("statement"), "subject": m.get("subject"),
            "predicate": m.get("predicate"), "epistemic_status": m.get("epistemic_status"),
            "reported_by": m.get("reported_by"), "confidence": m.get("confidence"),
            "contested": bool(m.get("contested")),
            "evidence": [{k: e.get(k) for k in ("id", "label", "source_event_id", "ts_ms", "speaker_role")}
                         for e in (m.get("evidence") or [])[:3]]}


REASON_TOOL = {
    "type": "function", "name": "choose_intervention",
    "description": "Decide whether one remembered fact should be brought up now.",
    "parameters": {"type": "object", "required": ["decision"], "properties": {
        "decision": {"type": "string", "enum": ["notify", "no_action"]},
        "memory_id": {"type": "string", "maxLength": 80},
        "rationale": {"type": "string", "maxLength": 200}}},
}
REASON_INSTRUCTIONS = """You decide whether a wearable assistant should interrupt the user.
You get one utterance and a few remembered facts (with who said them and when). Choose "notify" with
the memory_id of exactly one fact only if it clearly matters for what the user is doing right now;
otherwise "no_action". Remembered facts reported by other people are unverified. Prefer silence."""


class MemoryRecall:
    def __init__(self, memory, reasoner=None, config: RecallConfig | None = None):
        self.mem = memory                     # agent.memory_tools.AgentMemory
        self.reasoner = reasoner              # prod_dreamer_model.model_extract.BudgetedModel or None
        self.cfg = config or RecallConfig()
        self._index: tuple[int, set[str], set[str]] | None = None   # (built_at, person names, self-caution words)
        self._reasoner_calls: deque[int] = deque()
        self.stats = {"gated_out": 0, "retrievals": 0, "rule_hits": 0, "reasoner_calls": 0, "reasoner_failures": 0,
                      "silent_candidates": 0, "unavailable": 0}

    def invalidate(self) -> None:
        """New memories were consolidated (after a dream): rebuild the name index."""
        self._index = None

    def _names(self, ts: int) -> tuple[set[str], set[str]]:
        if self._index is None or ts - self._index[0] >= self.cfg.index_ttl_ms or ts < self._index[0]:
            names, own = self.mem.recall_index()
            self._index = (ts, names, own)
        return self._index[1], self._index[2]

    def assess(self, a: AudioObservation, it, ts: int) -> list[Decision]:
        if it.suspicious or it.intent in ("command", "answer"):
            return []
        from vixy.retrieval import Cues

        text = a.transcript
        stems = _stems(text)
        action = _ACTION.search(text)
        try:
            people, own = self._names(ts)
            cues = Cues.from_text(text, now_ms=ts)
            mentioned = [p for p in cues.people if p.lower() in people]
            self_caution = bool(action) and bool(own & stems) and a.speaker_is_user is not False
            if not mentioned and not self_caution:
                self.stats["gated_out"] += 1
                return []
            cues.people = mentioned
            self.stats["retrievals"] += 1
            found = self.mem.retrieve(cues, limit=self.cfg.max_memories, include=("memory",),
                                      exclude_event_ids=(a.segment_id,))
        except MemoryUnavailable:
            self.stats["unavailable"] += 1    # recall is an aid: no memory, no recall, no guess
            return []
        items = [m for m in found["items"] if m.get("label") == "LongTermMemory"]
        if self_caution:
            items += [m for m in self.mem.retrieve(type(cues)(keywords=sorted(own & stems), now_ms=ts),
                                                   limit=4, include=("memory",),
                                                   exclude_event_ids=(a.segment_id,))["items"]
                      if m.get("subject_id") == "user" and m["id"] not in {i["id"] for i in items}]
        ambiguous = set(n.lower() for n in found.get("ambiguous_people") or [])
        verb = _ACTIONS[action.group(1).lower()] if action else None
        decisions, possible = [], []
        for m in items:
            subj = str(m.get("subject") or "")
            is_self = m.get("subject_id") == "user"
            if not is_self and subj.split()[0].lower() not in {p.lower() for p in mentioned}:
                continue
            amb = subj.split()[0].lower() in ambiguous
            if m.get("caution") and m.get("polarity") is not False and action:
                hits = object_matches(m, stems)
                if hits:
                    decisions.append(self._decision(m, ts, "caution", verb, amb, hits))
                    continue
                possible.append(m)
            elif m.get("predicate") == "has_event" and m.get("event_start_ms") and it.intent == "commitment" and it.time:
                starts = [s for s, _ in it.time.intervals(TZ)] if it.time.has_time else []
                if any(abs(s - m["event_start_ms"]) <= self.cfg.plan_overlap_ms for s in starts):
                    decisions.append(self._decision(m, ts, "plan", None, amb, []))
                elif m["event_start_ms"] >= ts:
                    possible.append(m)
        if decisions:
            self.stats["rule_hits"] += len(decisions)
            return decisions[:2]
        if possible:
            return self._reason(a, possible[:4], ts, ambiguous)
        if items:
            return [no_action(f"recalled {len(items)} memories about {', '.join(mentioned) or 'you'}; none relevant now",
                              ts, "audio")]
        return []

    def _decision(self, m: dict, ts: int, kind: str, verb: str | None, amb: bool, hits: list[str],
                  reasoning: str = RECALL_VERSION, confidence_cap: float = 0.9) -> Decision:
        conf = min(float(m.get("confidence") or 0.5), confidence_cap)
        if m.get("contested"):
            conf *= 0.7
        if amb:
            conf *= 0.8
        why = {"caution": f"remembered {m.get('predicate')} '{m.get('object')}' conflicts with what is planned"
                          + (f" ({', '.join(hits)})" if hits else ""),
               "plan": "remembered plan of the same person overlaps the new plan",
               "possible": "a remembered fact may matter (model judgement)"}[kind]
        d = Decision(Action.NOTIFY, f"{why}; {m.get('epistemic_status')} memory {m['id']}", ts,
                     Priority.NORMAL if kind != "possible" else Priority.LOW,
                     confidence=round(conf, 2), message=render(m, ts, kind=kind, action=verb, ambiguous=amb),
                     related_memory_ids=[m["id"]] + [e["id"] for e in (m.get("evidence") or [])[:3] if e.get("id")],
                     deduplication_key=f"recall:{m['id']}", expires_ms=ts + self.cfg.expires_ms, trigger="audio")
        d.supporting_memories = [summary(m)]
        d.reasoning = reasoning
        return d

    def _reason(self, a: AudioObservation, cands: list[dict], ts: int, ambiguous: set[str]) -> list[Decision]:
        ids = [m["id"] for m in cands]
        if self.reasoner is None:
            self.stats["silent_candidates"] += 1
            return [no_action(f"possibly relevant memories {ids}, but the rules can't tell and no model is "
                              "configured; stayed silent", ts, "audio")]
        while self._reasoner_calls and self._reasoner_calls[0] <= ts - HOUR:
            self._reasoner_calls.popleft()
        if len(self._reasoner_calls) >= self.cfg.reasoner_max_per_hour:
            return [no_action("possibly relevant memory; reasoning budget for this hour used; stayed silent", ts, "audio")]
        self._reasoner_calls.append(ts)
        self.stats["reasoner_calls"] += 1
        data = {"utterance": a.transcript[:300],
                "memories": [{"memory_id": m["id"], "fact": claim_text(m), "said_by": attribution(m),
                              "epistemic_status": m.get("epistemic_status"), "contested": bool(m.get("contested"))}
                             for m in cands]}
        try:
            out = self.reasoner.call("agent.recall", REASON_INSTRUCTIONS, data, REASON_TOOL)
        except Exception:
            self.stats["reasoner_failures"] += 1
            return [no_action("model unavailable for a possibly relevant memory; stayed silent", ts, "audio")]
        mid = out.get("memory_id")
        if out.get("decision") != "notify" or mid not in ids:
            return [no_action(f"model judged memories {ids} not relevant now", ts, "audio")]
        m = next(c for c in cands if c["id"] == mid)
        amb = str(m.get("subject") or "").split()[0].lower() in ambiguous
        return [self._decision(m, ts, "possible", None, amb, [], reasoning=f"model:{self.reasoner.model_id}",
                               confidence_cap=0.6)]

