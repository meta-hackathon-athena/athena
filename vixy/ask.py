"""Natural-language questions about the user's past, answered from the unified graph.

    question -> interpret (rules: question type, people, topic words, time range)
             -> read-only graph queries (SharedMemory / MemoryRetriever / AgentLog)
             -> original context-engine events for the sources (text + sha256 re-checked)
             -> grounded answer (deterministic composer; optional model phrasing)
             -> vixy.ask.v1 JSON: answer, status, items with epistemic status, sources

Grounding rules:
* every sentence of a deterministic answer is built from a retrieved record, and every
  record used is returned in ``items`` and ``sources``;
* reported speech is presented as reported ("someone said ..., not verified"); the
  speaker of a Note is a diarization id, never a name, so "what did Marta tell me" says
  plainly that it cannot confirm who spoke;
* nothing found -> ``status: insufficient_evidence`` and an answer that says so;
* an optional model may only *rephrase* a grounded answer: it gets the retrieved items as
  data, must cite only their ids, and is discarded (deterministic answer kept) otherwise.

The service never writes to the graph. Follow-ups ("what evidence supports that?",
"why?") use a small per-conversation context stored in the runtime ledger (SQLite), so a
conversation survives a restart.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from memory.store import TZ

from .contracts import ASK_SCHEMA
from .retrieval import Cues, MemoryRetriever, keywords

ASK_VERSION = "rules:vixy.ask.v1"
DAY_MS = 86_400_000
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
QUESTION_WORDS = {"remember", "know", "tell", "say", "said", "mention", "mentioned", "told", "evidence", "support",
                  "supports", "memory", "remind", "reminded", "notify", "warn", "warned", "happen", "happened",
                  "spend", "spent", "meeting", "meet", "see", "seeing", "plan", "plans", "where", "go", "went",
                  "been", "proof", "source", "sources", "think", "believe", "why", "like", "likes"}
_TYPES: list[tuple[str, re.Pattern]] = [
    ("evidence", re.compile(r"\b(what|which) evidence\b|\bhow do you know\b|\bwhy do you (think|believe|remember)\b|"
                            r"\bsources?\b|\bproof\b|\bevidence\b", re.I)),
    ("why_decision", re.compile(r"\bwhy did you (remind|tell|notify|warn|ask|alert|bring|say|mention)\b|"
                                r"\bwhy (that|this|the) (reminder|notification|alert|warning)\b", re.I)),
    ("what_said", re.compile(r"\bwhat did (?P<who>[A-Za-z]+) (?:tell|say|mention|said)(?: (?:to )?me)?"
                             r"(?: about (?P<topic>[^?]+))?", re.I)),
    ("day_activity", re.compile(r"\bwhat did i (do|eat|buy|have|see)\b|\bwhere (did|was) i\b|\bwhere have i been\b|"
                                r"\bwhat happened\b|\bhow did i spend\b|\bwhat was i doing\b", re.I)),
    ("schedule", re.compile(r"\bwhen (am|do|will|is|are|was|did|'s)\b|\bwhat time\b|\bany plans?\b|"
                            r"\bwhat'?s (on|planned)\b", re.I)),
    ("about_entity", re.compile(r"\bwhat do you (remember|know)\b|\btell me (about|what)\b|\bwho is\b|"
                                r"\bwhat does \w+ (like|eat|do|need)\b|\bis \w+ (allergic|vegetarian|vegan)\b|"
                                r"\banything about\b", re.I)),
]
_FOLLOW_UP = re.compile(r"\b(that|this|it|those|these|him|her|them|the memory|the reminder)\b", re.I)
_MEM_ID = re.compile(r"\b(ltm-[a-z0-9-]+|routine-[a-z0-9-]+)\b")
_DEC_ID = re.compile(r"\b(dec_[0-9a-f]{12})\b")


@dataclass
class TimeRange:
    start_ms: int
    end_ms: int
    label: str

    def as_dict(self) -> dict:
        return {"start": _iso(self.start_ms), "end": _iso(self.end_ms), "label": self.label}


@dataclass
class Interpretation:
    type: str
    people: list[str] = field(default_factory=list)
    topic: list[str] = field(default_factory=list)
    time: TimeRange | None = None
    speaker: str | None = None             # what_said: the person asked about ("I" = the wearer)
    memory_id: str | None = None
    decision_id: str | None = None
    follow_up: bool = False
    places_only: bool = False              # "where did I go": visits, not what was said

    def as_dict(self) -> dict:
        out = {"type": self.type, "people": self.people, "topic_keywords": self.topic,
               "time_range": self.time.as_dict() if self.time else None, "follow_up": self.follow_up}
        for k in ("speaker", "memory_id", "decision_id"):
            if getattr(self, k):
                out[k] = getattr(self, k)
        return out


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).isoformat()


def _local(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, TZ)


def _day_label(d) -> str:
    return d.strftime("%A %-d %b")


def _hm(ms: int) -> str:
    return _local(ms).strftime("%H:%M")


def _when(ms: int) -> str:
    return f"{_day_label(_local(ms).date())} at {_hm(ms)}"


def _day_range(day, label: str | None = None) -> TimeRange:
    start = datetime.combine(day, datetime.min.time(), TZ)
    return TimeRange(int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000),
                     label or _day_label(day))


def parse_time_range(text: str, now: datetime) -> TimeRange | None:
    """Past-oriented time references ("yesterday", "last Saturday", "on Monday", "2 Nov")."""
    low = text.lower()
    today = now.date()
    if re.search(r"\bday before yesterday\b", low):
        return _day_range(today - timedelta(days=2))
    if re.search(r"\byesterday\b", low):
        return _day_range(today - timedelta(days=1), f"yesterday ({_day_label(today - timedelta(days=1))})")
    if re.search(r"\b(today|this morning|this afternoon|this evening|tonight)\b", low):
        return _day_range(today, f"today ({_day_label(today)})")
    if m := re.search(r"\b(\d+) days? ago\b", low):
        return _day_range(today - timedelta(days=int(m.group(1))))
    if re.search(r"\blast week\b", low):
        start = today - timedelta(days=today.weekday() + 7)
        r = _day_range(start)
        return TimeRange(r.start_ms, r.start_ms + 7 * DAY_MS, f"last week (from {_day_label(start)})")
    if re.search(r"\bthis week\b", low):
        start = today - timedelta(days=today.weekday())
        r = _day_range(start)
        return TimeRange(r.start_ms, int(now.timestamp() * 1000) + 1, f"this week (since {_day_label(start)})")
    if m := re.search(r"\b(?:(last|on|this|past)\s+)?(" + "|".join(WEEKDAYS) + r")\b", low):
        back = (today.weekday() - WEEKDAYS.index(m.group(2))) % 7
        if back == 0 and m.group(1) in ("last", "past"):
            back = 7
        return _day_range(today - timedelta(days=back))
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low):
        try:
            return _day_range(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date())
        except ValueError:
            return None
    if m := re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) + r"|" + "|".join(x[:3] for x in MONTHS)
                      + r")\b|\b(" + "|".join(MONTHS) + r"|" + "|".join(x[:3] for x in MONTHS) + r")\s+(\d{1,2})\b", low):
        dnum, mon = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        month = next(i for i, x in enumerate(MONTHS) if x.startswith(mon[:3])) + 1
        try:
            d = today.replace(month=month, day=int(dnum))
        except ValueError:
            return None
        return _day_range(d if d <= today else d.replace(year=d.year - 1))
    return None


class AskService:
    def __init__(self, runtime, model=None, max_items: int = 8):
        self.rt = runtime                      # vixy.runtime.VixyRuntime (graph, engine, ledger, memory)
        self.memory = runtime.memory           # SharedMemory (read-only)
        self.retriever = MemoryRetriever(runtime.graph)
        self.model = model                     # optional BudgetedModel used only to rephrase
        self.max_items = max_items

    # ----------------------------------------------------------------- public
    def ask(self, question: str, conversation_id: str | None = None, now_ms: int | None = None) -> dict:
        question = " ".join(str(question or "").split())[:500]
        now_ms = now_ms or self.rt.clock_ms()
        conv_id = conversation_id or "conv_" + uuid.uuid4().hex[:12]
        ctx = self.rt.ledger.get_kv(f"ask:{conv_id}", {}) or {}
        if not question:
            return self._response(question, conv_id, now_ms, Interpretation("unsupported"), "unsupported",
                                  "Please ask a question.", [], [])
        it = self.interpret(question, now_ms, ctx)
        handler = {"day_activity": self._day_activity, "schedule": self._schedule, "what_said": self._what_said,
                   "about_entity": self._about, "why_decision": self._why, "evidence": self._evidence,
                   "search": self._search}[it.type]
        status, answer, items = handler(it, now_ms, ctx)
        sources = self._sources(items)
        answered_by, usage = ASK_VERSION, None
        if self.model is not None and status == "answered" and items:
            answer, answered_by = self._rephrase(question, answer, items)
            usage = self.model.usage.as_dict()
        out = self._response(question, conv_id, now_ms, it, status, answer, items, sources, answered_by, usage)
        self.rt.ledger.set_kv(f"ask:{conv_id}", {
            "turns": int(ctx.get("turns", 0)) + 1, "last_type": it.type,
            "people": it.people or ctx.get("people") or [],
            "memory_ids": [i["id"] for i in items if i["label"] in ("LongTermMemory", "Pattern")][:5]
                          or ctx.get("memory_ids") or [],
            "decision_id": next((i["id"] for i in items if i["label"] == "AgentLog"), None) or ctx.get("decision_id"),
        })
        return out

    def interpret(self, question: str, now_ms: int, ctx: dict | None = None) -> Interpretation:
        ctx = ctx or {}
        qtype = next((t for t, rx in _TYPES if rx.search(question)), "search")
        known = self._known_people()
        tokens = re.findall(r"[A-Za-z][a-z'-]+", question)
        people = list(dict.fromkeys(t.capitalize() for t in tokens if t.lower() in known and t.lower() not in ("i",)))
        it = Interpretation(qtype, people=people, time=parse_time_range(question, _local(now_ms)),
                            places_only=bool(re.search(r"\bwhere\b", question, re.I)))
        if qtype == "what_said":
            m = _TYPES[2][1].search(question)
            who = m.group("who")
            it.speaker = "I" if who.lower() in ("i", "me") else who.capitalize()
            if it.speaker != "I" and it.speaker not in it.people:
                it.people = [it.speaker] + it.people
            it.topic = keywords(m.group("topic") or "")
        else:
            lowered = {p.lower() for p in people}
            it.topic = [k for k in keywords(question) if k not in lowered and k not in QUESTION_WORDS]
        if m := _MEM_ID.search(question):
            it.memory_id = m.group(1)
        if m := _DEC_ID.search(question):
            it.decision_id = m.group(1)
        refers = bool(_FOLLOW_UP.search(question))
        if not it.people and refers and ctx.get("people") and qtype in ("about_entity", "schedule", "what_said", "search"):
            it.people, it.follow_up = list(ctx["people"]), True
        if qtype == "evidence" and not it.memory_id and not it.people and ctx.get("memory_ids"):
            it.memory_id, it.follow_up = ctx["memory_ids"][0], True
        if qtype == "why_decision" and not it.decision_id and refers and ctx.get("decision_id"):
            it.decision_id, it.follow_up = ctx["decision_id"], True
        if qtype == "search" and it.people and not it.topic:
            it.type = "about_entity"
        return it

    # --------------------------------------------------------------- handlers
    def _day_activity(self, it: Interpretation, now_ms: int, ctx: dict):
        tr = it.time or _day_range(_local(now_ms).date(), f"today ({_day_label(_local(now_ms).date())})")
        it.time = tr
        rows = self.memory.timeline(tr.start_ms, min(tr.end_ms, now_ms + 1))
        visits = [r for r in rows if r["label"] == "Visit" and r.get("kind") == "stay"]
        said = [] if it.places_only else [r for r in rows if r["label"] == "Note" and r.get("speaker_role") == "wearer"]
        if not visits and not said:
            return ("insufficient_evidence", f"I have no recorded observations for {tr.label}.", [])
        items = [self._visit_item(v) for v in visits] + [self._note_item(n) for n in said[:3]]
        parts = []
        if visits:
            stays = "; ".join(f"{_hm(v['start_ms'])}–{_hm(v['end_ms'])} {self._where(v)}" for v in visits)
            parts.append(f"{tr.label[0].upper() + tr.label[1:]} the camera recorded you {stays}.")
        if said:
            quoted = "; ".join(f"at {_hm(n['ts_ms'])} “{n['text']}”" for n in said[:3])
            parts.append(f"You said: {quoted}.")
        topic = [k for k in it.topic if k not in {"camera", "recorded", "do", "doing"}]
        if topic:
            hay = " ".join(str(i.get("statement") or "") for i in items).lower()
            missing = [k for k in topic if k not in hay]
            if missing:
                parts.append(f"I have no record about {', '.join(missing)} on that day.")
                if len(missing) == len(topic):
                    return ("insufficient_evidence", " ".join(parts), items)
        parts.append("Places are inferred from what the camera detected.")
        return ("answered", " ".join(parts), items)

    def _schedule(self, it: Interpretation, now_ms: int, ctx: dict):
        cues = Cues(people=it.people, keywords=it.topic, now_ms=now_ms)
        if cues.empty:
            upcoming = self.memory.commitments(now_ms, now_ms + 7 * DAY_MS)
            if not upcoming:
                return ("insufficient_evidence", "I don't have any plans on record for the next 7 days.", [])
            items = [self._commitment_item(c) for c in upcoming[: self.max_items]]
            return ("answered", "Coming up: " + "; ".join(self._commitment_text(c) for c in items) + ".", items)
        found = self.retriever.retrieve(cues, include=("commitment", "memory"), limit=self.max_items)
        cmts = [r for r in found["items"] if r["label"] == "Commitment"]
        plans = [r for r in found["items"] if r["label"] == "LongTermMemory" and r.get("predicate") == "has_event"]
        items = [self._commitment_item(c) for c in cmts] + [self._memory_item(m) for m in plans]
        who = " and ".join(it.people) or " ".join(it.topic)
        parts = []
        if cmts:
            parts.append("You have " + "; ".join(self._commitment_text(self._commitment_item(c)) for c in cmts) + ".")
        else:
            parts.append(f"I don't have a plan with {who} on record.")
        for m in plans:
            parts.append(self._memory_sentence(m, prefix="I do remember that "))
        status = "answered" if cmts else "insufficient_evidence"
        return (status, " ".join(parts), items)

    def _what_said(self, it: Interpretation, now_ms: int, ctx: dict):
        terms = [p for p in it.people] + it.topic
        if it.speaker == "I":
            rows = [r for r in self._notes_matching(it.topic or terms, it.time) if r.get("speaker_role") == "wearer"]
            if not rows:
                return ("insufficient_evidence", "I have no record of you saying that.", [])
            items = [self._note_item(r) for r in rows[: self.max_items]]
            return ("answered", "You said: " + "; ".join(f"{_when(r['ts_ms'])} “{r['text']}”" for r in rows[:4]) + ".",
                    items)
        person = it.speaker
        rows = [r for r in self._notes_matching([person.lower()], it.time) if r.get("speaker_role") != "wearer"]
        if it.topic:
            rows = [r for r in rows if any(k in keywords(r["text"]) or k in r["text"].lower() for k in it.topic)]
        if not rows:
            about = f" about {' '.join(it.topic)}" if it.topic else ""
            return ("insufficient_evidence",
                    f"I have no recorded conversation mentioning {person}{about}, so I can't say what {person} told you.",
                    [])
        items = [self._note_item(r) for r in rows[: self.max_items]]
        lines = [f"{_when(r['ts_ms'])} {self._speaker(r)} said: “{r['text']}”" for r in rows[:4]]
        answer = (f"I can't identify speakers by name, so I can't confirm it was {person} who said it. "
                  f"What was said about {person}{' and ' + ' '.join(it.topic) if it.topic else ''}: "
                  + "; ".join(lines) + ".")
        return ("answered", answer, items)

    def _about(self, it: Interpretation, now_ms: int, ctx: dict):
        if not it.people and not it.topic:
            return ("insufficient_evidence", "Who or what would you like me to recall?", [])
        cues = Cues(people=it.people, keywords=it.topic, now_ms=now_ms)
        found = self.retriever.retrieve(cues, statuses=("active", "unconfirmed", "superseded"),
                                        include=("memory", "commitment", "reminder", "note"), limit=self.max_items)
        mems = [r for r in found["items"] if r["label"] in ("LongTermMemory", "Pattern")]
        cmts = [r for r in found["items"] if r["label"] == "Commitment"]
        notes = [r for r in found["items"] if r["label"] == "Note"
                 and not any(r["id"] in [e.get("id") for e in m.get("evidence") or []] for m in mems)]
        who = " and ".join(it.people) or " ".join(it.topic)
        if not (mems or cmts or notes):
            return ("insufficient_evidence", f"I don't have anything in memory about {who}.", [])
        parts = []
        if found["ambiguous_people"]:
            names = sorted({p["name"] for ps in found["people"].values() for p in ps})
            parts.append(f"Note: I know more than one person called {', '.join(found['ambiguous_people'])} "
                         f"({', '.join(names)}) and can't tell them apart.")
        current = [m for m in mems if m.get("status") != "superseded"]
        history = [m for m in mems if m.get("status") == "superseded"]
        if current:
            parts.append(f"About {who}: " + " ".join(self._memory_sentence(m) for m in current))
        if history:
            parts.append("Earlier (since changed): " + " ".join(self._memory_sentence(m) for m in history))
        if cmts:
            parts.append("Plans: " + "; ".join(self._commitment_text(self._commitment_item(c)) for c in cmts) + ".")
        if notes and not current:
            parts.append("Mentioned in conversation: " + "; ".join(
                f"{_when(n['ts_ms'])} {self._speaker(n)} said “{n['statement']}”" for n in notes[:3]) + ".")
        items = [self._memory_item(m) for m in mems] + [self._commitment_item(c) for c in cmts] + \
                ([self._note_item({**n, "text": n["statement"]}) for n in notes[:3]] if not current else [])
        return ("ambiguous" if found["ambiguous_people"] and not current else "answered", " ".join(parts), items)

    def _why(self, it: Interpretation, now_ms: int, ctx: dict):
        d = self.memory.decision(it.decision_id) if it.decision_id else {}
        if not d:
            recent = self.memory.decisions(since_ms=now_ms - 7 * DAY_MS, limit=50, delivered_only=True)
            recent = [r for r in recent if r["action"] in ("NOTIFY", "ASK_USER") and r["ts_ms"] <= now_ms]
            topic = set(it.topic) - {"reminder", "notification", "alert", "remind"}
            if topic:
                recent = [r for r in recent if topic & set(keywords(str(r.get("message") or "")))] or []
            elif ctx.get("people"):
                # "this" = what we were just talking about: prefer a notification about those people
                about = [r for r in recent if any(p.lower() in str(r.get("message") or "").lower() for p in ctx["people"])]
                recent = about or recent
            if not recent:
                return ("insufficient_evidence", "I can't find a recent notification that matches.", [])
            d = self.memory.decision(recent[0]["id"])
        it.decision_id = d["id"]
        items = [self._decision_item(d)]
        answer = f"At {_when(d['ts_ms'])} I said: “{d.get('message') or d['action']}”. Reason: {d['reason']}."
        if d.get("memories"):
            for m in d["memories"]:
                if not m:
                    continue
                items.append(self._memory_item({**m, "evidence": m.get("evidence")}))
                ev = m.get("evidence") or []
                said = "; ".join(f"{e['label']} {e['id']} ({self._speaker(e)} at {_when(e.get('ts_ms') or 0)}, "
                                 f"context-engine event {e.get('source_event_id')})" for e in ev[:3]
                                 if e.get("ts_ms"))
                answer += (f" It was based on a remembered fact: “{m.get('statement')}” "
                           f"({self._epistemic_phrase(m)}). Evidence: {said or 'none recorded'}.")
        elif d.get("concerns"):
            answer += " It concerned: " + ", ".join(f"{c['label']} {c['id']}" for c in d["concerns"]) + "."
        decided = d.get("reasoning") or "rules"
        answer += f" Decided by: {decided}."
        return ("answered", answer, items)

    def _evidence(self, it: Interpretation, now_ms: int, ctx: dict):
        mid = it.memory_id
        if not mid and it.people:
            found = self.retriever.retrieve(Cues(people=it.people, keywords=it.topic, now_ms=now_ms),
                                            include=("memory",), limit=1)
            mid = found["items"][0]["id"] if found["items"] else None
        if not mid:
            return ("insufficient_evidence", "Which memory do you mean? Ask about a person or topic first.", [])
        m = self.memory.explain(mid)
        if not m:
            return ("insufficient_evidence", f"I have no memory {mid}.", [])
        it.memory_id = mid
        items = [self._memory_item(m)]
        ev = m.get("evidence") or []
        if not ev:
            return ("insufficient_evidence",
                    f"“{m.get('statement')}” has no supporting observation, so I don't treat it as known.", items)
        lines = []
        for e in ev[:5]:
            item = self._note_item(e) if e["label"] == "Note" else self._visit_item(e)
            items.append(item)
            check = item.get("verified")
            mark = " (matches the original recording)" if check else " (could not be re-checked)" if check is False else ""
            if e["label"] == "Note":
                lines.append(f"{_when(e['ts_ms'])} {self._speaker(e)} said “{e.get('text')}”{mark}")
            else:
                lines.append(f"a {e.get('place_type') or 'place'} visit {_when(e.get('start_ms') or 0)}{mark}")
        answer = (f"“{m.get('statement')}” ({self._epistemic_phrase(m)}) is supported by {len(ev)} "
                  f"observation{'s' if len(ev) != 1 else ''}: " + "; ".join(lines) + ".")
        if m.get("conflicts_with"):
            answer += " It conflicts with: " + "; ".join(f"“{c['statement']}”" for c in m["conflicts_with"]) + "."
        if m.get("superseded_by"):
            answer += " It was later replaced by: " + "; ".join(f"“{c['statement']}”" for c in m["superseded_by"]) + "."
        return ("answered", answer, items)

    def _search(self, it: Interpretation, now_ms: int, ctx: dict):
        cues = Cues(people=it.people, keywords=it.topic, since_ms=it.time.start_ms if it.time else None,
                    until_ms=it.time.end_ms if it.time else None, now_ms=now_ms)
        found = self.retriever.retrieve(cues, limit=self.max_items)
        if not found["items"]:
            return ("insufficient_evidence", "I don't have anything in memory about that.", [])
        items, parts = [], []
        for r in found["items"][:5]:
            if r["label"] in ("LongTermMemory", "Pattern"):
                items.append(self._memory_item(r))
                parts.append(self._memory_sentence(r))
            elif r["label"] == "Commitment":
                items.append(self._commitment_item(r))
                parts.append("Plan: " + self._commitment_text(items[-1]) + ".")
            elif r["label"] == "Note":
                items.append(self._note_item({**r, "text": r["statement"]}))
                parts.append(f"{_when(r['ts_ms'])} {self._speaker(r)} said “{r['statement']}”.")
            else:
                items.append(self._generic_item(r))
                parts.append(f"Open reminder: {r['statement']}.")
        return ("answered", " ".join(parts), items)

    # ----------------------------------------------------------------- pieces
    def _known_people(self) -> set[str]:
        rows = self.memory._ro("MATCH (p:Person) RETURN p.name AS name, p.first_name AS first LIMIT 2000")
        return {str(v).lower() for r in rows for v in r.values() if v}

    def _notes_matching(self, terms: list[str], tr: TimeRange | None) -> list[dict]:
        if not terms:
            return []
        return self.memory._ro("""
            MATCH (n:Note) WHERE n.ts_ms >= $lo AND n.ts_ms < $hi AND any(k IN $terms WHERE toLower(n.text) CONTAINS k)
            RETURN n.id AS id, n.text AS text, n.ts_ms AS ts_ms, n.timestamp AS timestamp,
                   n.speaker_id AS speaker_id, n.speaker_role AS speaker_role, n.source_event_id AS source_event_id,
                   n.payload_sha256 AS payload_sha256
            ORDER BY n.ts_ms LIMIT 20
        """, {"lo": tr.start_ms if tr else 0, "hi": tr.end_ms if tr else 2 ** 62, "terms": [t.lower() for t in terms]})

    @staticmethod
    def _speaker(r: dict) -> str:
        role = r.get("speaker_role")
        if role == "wearer":
            return "you"
        spk = r.get("speaker_id")
        tag = f" (speaker {spk})" if spk and spk != "unknown" else ""
        return ("someone else" if role == "other" else "an unidentified speaker") + tag

    @staticmethod
    def _epistemic_phrase(m: dict) -> str:
        e = m.get("epistemic_status")
        conf = m.get("confidence")
        c = f", confidence {conf:.2f}" if isinstance(conf, (int, float)) else ""
        if e == "reported":
            who = "you" if m.get("reported_by") == ["wearer"] else "someone else"
            return f"reported by {who}; not verified{c}"
        if e == "hypothesis":
            return f"an uncertain remark (hedged); not verified{c}"
        if e == "self_reported":
            return f"you said this about yourself{c}"
        if e == "inferred":
            return f"inferred from repeated observations{c}"
        return f"{e or 'stored memory'}{c}"

    def _memory_sentence(self, m: dict, prefix: str = "") -> str:
        from agent.recall import claim_text

        first = min([e.get("ts_ms") for e in m.get("evidence") or [] if e.get("ts_ms")] or
                    [m.get("first_stated_ms") or 0]) or None
        text = claim_text(m) if m.get("predicate") else str(m.get("statement") or "").rstrip(".")
        text = text[:1].upper() + text[1:] if not prefix else text
        when = f", first said {_day_label(_local(first).date())}" if first and m.get("epistemic_status") != "inferred" else ""
        extra = ""
        if m.get("contested"):
            extra += " Another statement contradicts this."
        if m.get("status") == "superseded" and m.get("valid_until_ms"):
            extra += f" (until {_day_label(_local(m['valid_until_ms']).date())})"
        return f"{prefix}{text} ({self._epistemic_phrase(m)}{when}).{extra}"

    def _commitment_text(self, c: dict) -> str:
        status = {"tentative": "tentative: from what you said, not confirmed", "confirmed": "confirmed"}.get(
            c.get("status"), c.get("status") or "")
        return f"“{c['statement']}” on {_when(c['time'])} ({status})" if c.get("time") else f"“{c['statement']}”"

    def _where(self, v: dict) -> str:
        if v.get("place_name") and not v.get("sensitive"):
            return f"at {v['place_name']}"
        pt = (v.get("place_type") or "unknown place").replace("_", " ")
        return "at home" if pt == "home" else f"at the {pt}" + (f" ({v['activity']})" if v.get("activity") not in
                                                                (None, "unknown", "relaxing") else "")

    # ------------------------------------------------------------------ items
    @staticmethod
    def _base(r: dict, label: str, layer: str, epistemic: str, statement: str, time_ms: int | None) -> dict:
        return {"id": r["id"], "label": label, "layer": layer, "epistemic_status": epistemic, "statement": statement,
                "time": _iso(time_ms) if time_ms else None, "status": r.get("status"),
                "confidence": r.get("confidence")}

    def _memory_item(self, m: dict) -> dict:
        out = self._base(m, m.get("label") or "LongTermMemory", "inferred", m.get("epistemic_status") or "inferred",
                         m.get("statement"), m.get("first_stated_ms") or m.get("updated_ms"))
        out.update({k: m.get(k) for k in ("subject", "predicate", "object", "polarity", "contested", "reported_by",
                                            "valid_until_ms", "event_start_ms") if m.get(k) is not None})
        out["source_ids"] = [e.get("id") for e in m.get("evidence") or [] if e.get("id")]
        out["_evidence"] = m.get("evidence") or []
        return out

    def _note_item(self, n: dict) -> dict:
        out = self._base(n, "Note", "observed", "observed", n.get("text") or n.get("statement"), n.get("ts_ms"))
        out.update({"speaker_role": n.get("speaker_role"), "speaker_id": n.get("speaker_id"),
                    "source_event_id": n.get("source_event_id")})
        out["verified"] = self._verify_note(n)
        return out

    def _visit_item(self, v: dict) -> dict:
        out = self._base(v, "Visit", "observed", "observed",
                         f"{(v.get('place_type') or 'unknown').replace('_', ' ')} "
                         f"{_hm(v['start_ms'])}–{_hm(v['end_ms'])}" if v.get("start_ms") and v.get("end_ms")
                         else str(v.get("place_type")), v.get("start_ms"))
        out.update({"place_type": v.get("place_type"), "activity": v.get("activity"),
                    "first_event_id": v.get("first_event_id"), "last_event_id": v.get("last_event_id"),
                    "basis": "camera detections (place type inferred by the segmenter)"})
        if v.get("first_event_id"):
            out["verified"] = self.rt.engine.get_event_record(v["first_event_id"]) is not None
        return out

    def _commitment_item(self, c: dict) -> dict:
        out = self._base(c, "Commitment", "operational", "operational", c.get("title") or c.get("statement"),
                         c.get("start_ms"))
        out.update({"status": c.get("status"), "source": c.get("source"), "people": c.get("people_named") or c.get("people")})
        out["source_ids"] = [e.get("id") for e in c.get("evidence") or [] if isinstance(e, dict) and e.get("id")]
        out["_evidence"] = [e for e in c.get("evidence") or [] if isinstance(e, dict)]
        out["time"] = c.get("start_ms")
        return out

    def _decision_item(self, d: dict) -> dict:
        out = self._base(d, "AgentLog", "operational", "operational", d.get("message") or d.get("reason"), d.get("ts_ms"))
        out.update({"action": d.get("action"), "reason": d.get("reason"), "reasoning": d.get("reasoning"),
                    "trigger_event_id": d.get("trigger_event_id"),
                    "supporting_memory_ids": d.get("supporting_memory_ids") or []})
        return out

    def _generic_item(self, r: dict) -> dict:
        return self._base(r, r.get("label") or "Record", "operational", r.get("epistemic_status") or "operational",
                          r.get("statement"), r.get("ts_ms"))

    def _verify_note(self, n: dict) -> bool | None:
        eid = n.get("source_event_id")
        if not eid:
            return None
        rec = self.rt.engine.get_event_record(eid)
        if rec is None:
            return False
        text_ok = str(rec.event.get("text") or "").strip() == str(n.get("text") or n.get("statement") or "").strip()
        sha = n.get("payload_sha256")
        return text_ok and (not sha or sha == rec.payload_sha256)

    def _sources(self, items: list[dict]) -> list[dict]:
        """The observed records behind the answer, each re-checked against context-engine."""
        out, seen = [], set()
        for i in items:                                   # observations used directly first
            if i["label"] in ("Note", "Visit") and i["id"] not in seen:
                seen.add(i["id"])
                out.append({k: i.get(k) for k in ("id", "label", "source_event_id", "first_event_id", "time",
                                                  "speaker_role", "speaker_id", "statement", "verified") if i.get(k) is not None})
        for i in items:                                   # then the evidence of memories / plans / decisions
            for e in i.pop("_evidence", None) or []:
                if not e.get("id") or e["id"] in seen:
                    continue
                seen.add(e["id"])
                src = {k: e.get(k) for k in ("id", "label", "source_event_id", "first_event_id", "speaker_role",
                                             "speaker_id") if e.get(k) is not None}
                if e.get("ts_ms"):
                    src["time"] = _iso(e["ts_ms"])
                if e.get("label") == "Note":
                    note = self.memory._ro("MATCH (n:Note {id: $id}) RETURN n.text AS text, n.source_event_id AS "
                                           "source_event_id, n.payload_sha256 AS payload_sha256", {"id": e["id"]})
                    if note:
                        src["statement"], src["verified"] = note[0].get("text"), self._verify_note(note[0])
                elif e.get("first_event_id"):
                    src["verified"] = self.rt.engine.get_event_record(e["first_event_id"]) is not None
                out.append(src)
        for i in items:
            if isinstance(i.get("time"), int):
                i["time"] = _iso(i["time"])
        return out

    def _rephrase(self, question: str, answer: str, items: list[dict]) -> tuple[str, str]:
        tool = {"type": "function", "name": "compose_answer", "description": "A short grounded answer.",
                "parameters": {"type": "object", "required": ["answer", "cited_ids"], "properties": {
                    "answer": {"type": "string", "maxLength": 800},
                    "cited_ids": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 80}}}}}
        data = {"question": question, "draft_answer": answer,
                "records": [{k: i.get(k) for k in ("id", "label", "epistemic_status", "statement", "time", "status")}
                            for i in items[: self.max_items]]}
        try:
            out = self.model.call("ask.compose", "Rewrite the draft answer to the question in plain, short language. "
                                  "Use only the records. Keep every 'not verified' / 'someone said' qualifier and "
                                  "every 'I don't have a record' statement. Cite the record ids you used.", data, tool)
        except Exception:
            return answer, ASK_VERSION
        ids = {i["id"] for i in items}
        cited = set(out.get("cited_ids") or [])
        if not cited or not cited <= ids or not out.get("answer", "").strip():
            return answer, ASK_VERSION                     # ungrounded rewrite: keep the deterministic answer
        return out["answer"].strip(), f"model:{self.model.model_id}"

    @staticmethod
    def _response(question, conv_id, now_ms, it, status, answer, items, sources, answered_by=ASK_VERSION, usage=None):
        for i in items:
            i.pop("_evidence", None)
            if isinstance(i.get("time"), int):
                i["time"] = _iso(i["time"])
        return {"schema": ASK_SCHEMA, "question": question, "conversation_id": conv_id, "now": _iso(now_ms),
                "status": status, "answer": answer, "interpretation": it.as_dict(),
                "items": [{k: v for k, v in i.items() if v is not None} for i in items],
                "sources": sources, "answered_by": answered_by, "model_usage": usage}
