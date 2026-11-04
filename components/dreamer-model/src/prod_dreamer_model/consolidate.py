"""Deterministic consolidation over the unified Vixy graph (the offline path).

``heuristic_dream`` is the fallback for the legacy ``Day/Frame/Class`` graph and
its signatures are tuned to ``mock_data``. On the shared graph the observed
layer is episodes (``:Visit``) and utterances (``:Note``) written by the
runtime's Episode Writer, so this module derives beliefs from those, with
three generic rule families:

1. Routines: the same place type visited on >= 3 distinct local days becomes a
   ``:Pattern`` (with a time window and weekdays) plus a ``:LongTermMemory``
   distilled from it.
2. Fact promotion: each active structured ``:Fact`` (frequent place, usual
   activity, user-stated preference) becomes one ``:LongTermMemory``
   ``DERIVED_FROM`` it. A retired Fact retires its memory.
3. Semantic claims (``vixy.semantic.v2``, see ``semantic.py``): every verified
   utterance is parsed into structured claims (subject, predicate, object, polarity,
   valid time, epistemic status). Claims about the same thing are consolidated
   into ONE ``:LongTermMemory`` (more evidence = reinforcement, never a duplicate);
   different values for the same slot become an explicit ``SUPERSEDES`` (stated
   change) or ``CONFLICTS_WITH`` (no change stated: both kept, both ``contested``).
   People are resolved conservatively (``entities.py``). Before it is used, every
   note is checked against the original context-engine event (same id, same
   text, same hash); a note that cannot be verified is skipped, not guessed. An
   optional model extractor adds claims the rules miss; its validated output is
   cached per note so it is never requested twice.

Provenance rule: evidence is always the ids of the exact Visit/Note records a
belief was computed from. Nothing is linked because it happens to fall inside
a time window. Ids are derived from content, so a re-run updates the same
nodes instead of adding new ones.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict
from statistics import median
from typing import Any, Protocol

from .entities import resolve
from .heuristic import describe_days, hhmm, third_person
from .semantic import (EXTRACTOR_VERSION, PREDICATES, RULES_EXTRACTOR, Claim, extract_claims, local_time,
                       norm_text, person_statements, stem)
from .tools import GraphToolBox

RULES_VERSION = "vixy.offline_consolidation.v2"
LEGACY_RULES_VERSIONS = ("vixy.offline_consolidation.v1",)
MIN_ROUTINE_DAYS = 3
SENSITIVE_PLACE_TYPES = {"healthcare", "worship"}

_ROLE_CONFIDENCE = {"wearer": 0.75, "other": 0.65, "unknown": 0.55}
_PREFERENCE_VERBS = re.compile(r"\b(likes|loves|hates|prefers|enjoys)\b")
_HEDGED_CAP = 0.45
_RELATION_WORDS = re.compile(
    r"\b(brother|sister|mother|father|mom|dad|son|daughter|wife|husband|partner|"
    r"friend|colleague|boss|manager|roommate|cousin|neighbour|neighbor)\b", re.I)


class EventLookup(Protocol):
    def get(self, event_id: str) -> Any: ...


def _norm(text: str) -> str:
    return norm_text(text)


def _verified(note: dict[str, Any], history: EventLookup | None) -> tuple[bool, str]:
    """Is this Note the context-engine event it claims to be? Returns (ok, text)."""
    text = (note.get("text") or "").strip()
    if history is None:
        return False, text
    record = history.get(note.get("source_event_id") or note.get("note_id") or "")
    if record is None or getattr(record, "kind", None) != "audio":
        return False, text
    original = str(record.event.get("text") or "").strip()
    if _norm(original) != _norm(text):
        return False, text
    sha = note.get("payload_sha256")
    if sha and sha != record.payload_sha256:
        return False, text
    return True, original


def _place_phrase(place_type: str) -> str:
    if place_type == "home":
        return "Is at home"
    if place_type == "transit":
        return "Travels by public transport"
    label = place_type.replace("_", " ")
    return f"Goes to {'an' if label[0] in 'aeiou' else 'a'} {label}"


_ACTIVITY = {"shopping": "shops", "working": "works", "eating": "eats", "studying": "studies",
             "exercising": "exercises"}


def _routines(toolbox: GraphToolBox, visits: list[dict[str, Any]]) -> tuple[list[str], dict[str, str]]:
    """Returns (pattern ids, place_type -> routine memory id)."""
    graph = toolbox.graph
    by_type: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for v in visits:
        pt = v.get("place_type")
        if v.get("kind") != "stay" or v.get("sensitive") or pt in (None, "unknown") or pt in SENSITIVE_PLACE_TYPES:
            continue
        day = v.get("local_date")
        if day and (day not in by_type[pt] or v["start_ms"] < by_type[pt][day]["start_ms"]):
            by_type[pt][day] = v       # first visit of that place type per day: independent evidence
    patterns, memories = [], {}
    for pt, per_day in sorted(by_type.items()):
        if len(per_day) < MIN_ROUTINE_DAYS:
            continue
        chosen = [per_day[d] for d in sorted(per_day)]
        minutes = [int(v["start_min"]) for v in chosen]
        dows = [int(v["dow"]) for v in chosen]
        tag_days: dict[str, int] = defaultdict(int)
        for v in chosen:
            for t in set(v.get("tags") or []):
                tag_days[t] += 1
        about = [t for t, n in sorted(tag_days.items(), key=lambda kv: (-kv[1], kv[0])) if n * 2 >= len(chosen)][:3]
        n = len(chosen)
        statement = (f"{_place_phrase(pt)} around {hhmm(int(median(minutes)))} {describe_days(dows)} "
                     f"(seen on {n} separate days).").replace("  ", " ")
        evidence = [v["id"] for v in chosen]
        confidence = round(min(0.95, n / (n + 2)), 2)
        pid = f"routine-{pt.replace('_', '-')}"
        res = graph.write_pattern(
            pattern_id=pid, statement=statement, kind="routine", confidence=confidence,
            evidence=evidence, about=about, days=dows,
            time_window=f"{hhmm(max(min(minutes) - 15, 0))}-{hhmm(min(max(minutes) + 15, 1439))}",
            extra={"place_type": pt, "support_days": n, "derived_by": RULES_VERSION},
        )
        patterns.append(res["pattern_id"])
        mid = f"ltm-{pid}"
        graph.write_memory(
            memory_id=mid, statement=statement, kind="routine", confidence=confidence,
            evidence=evidence, about=about, from_patterns=[pid],
            extra={"basis": "episodes", "place_type": pt, "support_days": n, "derived_by": RULES_VERSION,
                   "epistemic_status": "inferred", "category": "routine", "keywords": sorted({pt, *about})},
        )
        memories[pt] = mid
    return patterns, memories


def _facts(toolbox: GraphToolBox, routine_for: dict[str, str]) -> list[str]:
    """Promote structured Facts. A place Fact whose place type already has a routine memory
    is consolidated INTO that memory (extra DERIVED_FROM + its evidence), not duplicated."""
    graph = toolbox.graph
    memories = []
    for f in graph.fetch_facts("active"):
        evidence = [e for e in (f.get("evidence") or []) if e]
        place = f.get("place_name") or (f.get("place_type") or "place").replace("_", " ")
        kind = f.get("kind")
        if kind in ("frequent_place", "usual_activity") and f.get("place_type") in routine_for:
            mid = routine_for[f["place_type"]]
            graph.add_evidence(mid, evidence)
            graph.link_facts(mid, [f["id"]])
            continue
        if kind == "frequent_place":
            where = "at home" if f.get("place_type") == "home" else f"to the {place}"
            statement, mkind = f"Often goes {where} (seen on {f.get('support_days')} separate days).", "place"
        elif kind == "usual_activity" and f.get("activity") in _ACTIVITY:
            statement, mkind = f"Usually {_ACTIVITY[f['activity']]} at the {place}.", "routine"
        elif kind == "preference" and f.get("statement"):
            statement, mkind = third_person(f["statement"]), "preference"
        else:
            continue
        mid = f"ltm-{f['id']}"
        graph.write_memory(
            memory_id=mid, statement=statement, kind=mkind,
            confidence=float(f.get("confidence") or 0.5), evidence=evidence, from_facts=[f["id"]],
            extra={"basis": "fact", "fact_kind": kind, "derived_by": RULES_VERSION, "subject": "user",
                   "subject_id": "user", "category": "preference" if kind == "preference" else "routine",
                   "epistemic_status": "self_reported" if f.get("source") == "user" else "inferred"},
        )
        memories.append(mid)
    # a Fact that lost its support takes the memory distilled only from it along
    graph.query("""
        MATCH (m:LongTermMemory)-[:DERIVED_FROM]->(f:Fact)
        WHERE f.status <> 'active' AND m.status <> 'inactive' AND m.basis = 'fact'
        SET m.status = 'inactive', m.updated_ms = $now
    """, {"now": int(time.time() * 1000)})
    return memories


def _claim_slot(c: Claim, subject_id: str) -> tuple[str, str]:
    """(slot, value): one memory per (slot, value); different values in one slot are compared."""
    if c.predicate == "has_event":
        return f"{subject_id}|has_event|{c.object}", str(c.event_start_ms) if c.event_start_ms else "unspecified"
    if PREDICATES[c.predicate]["functional"]:
        return f"{subject_id}|{c.predicate}", c.object if c.polarity else f"not {c.object}"
    return f"{subject_id}|{c.predicate}|{c.object}", "+" if c.polarity else "-"


def _memory_id(c: Claim, slot: str, value: str, first_name: str) -> str:
    if c.predicate == "states":                       # Milestone 1 id: the same node as before
        return f"ltm-person-{first_name.lower()}-{hashlib.sha1(_norm(c.statement).encode()).hexdigest()[:10]}"
    return "ltm-claim-" + hashlib.sha1(f"{slot}|{value}".encode()).hexdigest()[:16]


def _kind(c: Claim) -> str:
    if c.predicate != "states":
        return PREDICATES[c.predicate]["kind"]
    return ("preference" if _PREFERENCE_VERBS.search(c.statement)
            else "relationship" if _RELATION_WORDS.search(c.statement) else "fact")


def _model_claims(graph, extractor, notes: list[dict], stats: dict) -> dict[str, list[Claim]]:
    """Model claims per note id: cached output first, then (budgeted) new calls."""
    from .model_extract import claims_from_cache

    if extractor is None or not notes:
        return {}
    eid = extractor.extractor_id
    cached = graph.cached_extractions(eid, [n["id"] for n in notes])
    stats["model_cached_notes"] = len(cached)
    todo = [n for n in notes if n["id"] not in cached]
    if todo:
        fresh, xstats = extractor.extract(todo)
        stats["model_calls"] = xstats["calls"]
        stats["model_errors"] = xstats["errors"]
        stats["model_rejected"] = xstats["rejected"]
        for nid, claims in fresh.items():
            graph.store_extraction(eid, nid, claims)        # only notes whose call succeeded
            cached[nid] = claims
    by_id = {n["id"]: n for n in notes}
    return {nid: claims_from_cache(by_id[nid], cl, eid, extractor.tz) for nid, cl in cached.items() if nid in by_id}


def _semantic(toolbox: GraphToolBox, notes: list[dict[str, Any]], history: EventLookup | None,
              extractor: Any = None) -> tuple[list[str], dict[str, Any]]:
    """Verified utterances -> structured claims -> consolidated LongTermMemory nodes."""
    graph = toolbox.graph
    tz = graph.config.tz
    stats: dict[str, Any] = {"notes_unverified": 0, "claims": 0, "rule_claims": 0, "model_claims": 0,
                             "conflicts": 0, "superseded": 0, "persons": 0, "ambiguous_persons": 0,
                             "model_calls": 0, "model_errors": [], "model_rejected": 0, "model_cached_notes": 0}
    verified: list[dict[str, Any]] = []
    for note in notes:
        text = note.get("text") or ""
        said = local_time(int(note.get("ts_ms") or 0), tz)
        if not extract_claims(note["id"], text, said, speaker_is_wearer=note.get("speaker_role") == "wearer") \
                and extractor is None:
            continue                     # nothing to consolidate (and no model to ask)
        ok, original = _verified(note, history)
        if not ok:
            stats["notes_unverified"] += 1    # never consolidate what we can't trace to the original event
            continue
        verified.append({**note, "text": original})

    claims: list[tuple[Claim, dict]] = []
    for note in verified:
        said = local_time(int(note.get("ts_ms") or 0), tz)
        for c in extract_claims(note["id"], note["text"], said, speaker_is_wearer=note.get("speaker_role") == "wearer"):
            claims.append((c, note))
            stats["rule_claims"] += 1
    by_note = {n["id"]: n for n in verified}
    for nid, mclaims in _model_claims(graph, extractor, verified, stats).items():
        for c in mclaims:
            if c.subject.is_user and by_note[nid].get("speaker_role") != "wearer":
                continue
            claims.append((c, by_note[nid]))
            stats["model_claims"] += 1

    entities = resolve([c.subject for c, _ in claims] + [c.object_person for c, _ in claims if c.object_person])
    for e in entities.values():
        graph.upsert_person(e.id, e.props())
        for cand in e.candidates:
            graph.relate("Person", e.id, "POSSIBLY_SAME_AS", cand, {"basis": "same first name"})
    stats["persons"] = len(entities)
    stats["ambiguous_persons"] = sum(e.status != "resolved" for e in entities.values())

    # group: one memory per (slot, value)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for c, note in claims:
        subject_id = "user" if c.subject.is_user else entities[c.subject.surface].id
        slot, value = _claim_slot(c, subject_id)
        g = groups.setdefault((slot, value), {"claims": [], "notes": {}, "subject_id": subject_id})
        g["claims"].append(c)
        g["notes"][note["id"]] = note
    slots: dict[str, list[dict[str, Any]]] = defaultdict(list)
    memories: list[str] = []
    for (slot, value), g in sorted(groups.items()):
        cs: list[Claim] = sorted(g["claims"], key=lambda c: (g["notes"][c.note_id].get("ts_ms") or 0, c.extracted_by))
        first = cs[0]
        notes_g = sorted(g["notes"].values(), key=lambda n: (n.get("ts_ms") or 0, n["id"]))
        roles = sorted({n.get("speaker_role") or "unknown" for n in notes_g})
        speakers = sorted({n.get("speaker_id") or "unknown" for n in notes_g})
        hedged = all(c.hedged for c in cs)
        subject_id = g["subject_id"]
        subject_name = "user" if subject_id == "user" else entities[first.subject.surface].name
        confidence = max(_ROLE_CONFIDENCE.get(r, 0.55) for r in roles) + 0.1 * (len(notes_g) - 1)
        confidence = round(min(_HEDGED_CAP if hedged else 0.9, confidence), 2)
        epistemic = ("hypothesis" if hedged else
                     "self_reported" if subject_id == "user" and roles == ["wearer"] else "reported")
        mid = _memory_id(first, slot, value, subject_name.split()[0] if subject_id != "user" else "user")
        people = [] if subject_id == "user" else [entities[first.subject.surface].id]
        people += [entities[c.object_person.surface].id for c in cs if c.object_person]
        keywords = sorted({stem(w) for w in norm_text(f"{first.object_text} {first.object}").split() if len(w) > 2}
                          | ({subject_name.split()[0].lower()} if subject_id != "user" else set()))
        props = {
            "basis": "reported_speech", "subject": subject_name, "subject_id": subject_id,
            "predicate": first.predicate, "object": first.object, "object_text": first.object_text,
            "polarity": first.polarity, "category": PREDICATES[first.predicate]["category"],
            "caution": PREDICATES[first.predicate]["caution"], "claim_key": slot, "claim_value": value,
            "epistemic_status": epistemic, "reported_by": roles, "speakers": speakers,
            "support_count": len(notes_g), "first_stated_ms": notes_g[0].get("ts_ms"),
            "last_stated_ms": notes_g[-1].get("ts_ms"), "valid_from_ms": notes_g[0].get("ts_ms"),
            "event_start_ms": first.event_start_ms, "event_has_time": first.event_has_time if first.event_start_ms else None,
            "time_text": first.time_text, "extracted_by": sorted({c.extracted_by for c in cs}),
            "keywords": keywords, "contested": False, "derived_by": RULES_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
        }
        slots[slot].append({"id": mid, "value": value, "first_ms": notes_g[0].get("ts_ms") or 0,
                            "last_ms": notes_g[-1].get("ts_ms") or 0, "change": any(c.change for c in cs),
                            "props": props, "statement": first.statement, "kind": _kind(first),
                            "confidence": confidence, "evidence": [n["id"] for n in notes_g], "people": people})
        stats["claims"] += len(cs)

    for slot, items in sorted(slots.items()):
        items.sort(key=lambda i: (i["first_ms"], i["id"]))
        relations: list[tuple[str, str, str, dict]] = []
        comparable = [i for i in items if i["value"] != "unspecified"]   # a plan without a time contradicts nothing
        for prev, cur in zip(comparable, comparable[1:]):
            if cur["change"] and prev["last_ms"] < cur["first_ms"]:
                prev["props"]["valid_until_ms"] = cur["first_ms"]
                prev["status"] = "superseded"
                relations.append((cur["id"], "SUPERSEDES", prev["id"], {"basis": "stated_change"}))
                stats["superseded"] += 1
            else:
                prev["props"]["contested"] = cur["props"]["contested"] = True
                relations.append((cur["id"], "CONFLICTS_WITH", prev["id"], {"basis": "different_value_no_change_stated"}))
                stats["conflicts"] += 1
        for i in items:
            graph.write_memory(memory_id=i["id"], statement=i["statement"], kind=i["kind"],
                               confidence=i["confidence"], evidence=i["evidence"],
                               status=i.get("status", "active"), extra=i["props"])
            graph.link_persons(i["id"], i["people"])
            memories.append(i["id"])
        for a, rel, b, props in relations:
            graph.relate("LongTermMemory", a, rel, b, props)

    if history is not None:
        # Statement memories no longer produced from verified evidence (including the
        # Milestone 1 generic memories a structured claim now covers) are retired, not
        # deleted; a structured memory sharing their evidence SUPERSEDES them.
        graph.query("""
            MATCH (old:LongTermMemory)
            WHERE old.basis = 'reported_speech' AND old.status IN ['active', 'unconfirmed'] AND NOT old.id IN $ids
            SET old.status = 'inactive', old.updated_ms = $now, old.retired_reason = 'not produced by ' + $rules
            WITH old
            MATCH (old)-[:SUPPORTED_BY]->(:Note)<-[:SUPPORTED_BY]-(new:LongTermMemory)
            WHERE new.id IN $ids
            MERGE (new)-[:SUPERSEDES {basis: 're-extracted'}]->(old)
        """, {"ids": memories, "now": int(time.time() * 1000), "rules": RULES_VERSION})
    stats["verified_notes"] = len(verified)
    return memories, stats


def unified_dream(toolbox: GraphToolBox, history: EventLookup | None = None, extractor: Any = None) -> dict[str, Any]:
    """Consolidate the shared graph deterministically (plus optional model extraction). Safe to re-run."""
    graph = toolbox.graph
    visits = graph.fetch_visits()
    notes = graph.fetch_notes()
    previous = graph.rows("MATCH (r:DreamRun) RETURN max(r.watermark_ms) AS w")
    watermark_before = previous[0]["w"] if previous and previous[0].get("w") is not None else None
    patterns, routine_for = _routines(toolbox, visits)
    fact_memories = _facts(toolbox, routine_for)
    claim_memories, semantic = _semantic(toolbox, notes, history, extractor)
    memories = list(routine_for.values()) + fact_memories + claim_memories
    inputs = sorted(v["id"] for v in visits) + sorted(n["id"] for n in notes)
    run_id = "dream_" + hashlib.sha1((RULES_VERSION + "|" + "|".join(inputs)).encode()).hexdigest()[:16]
    watermark = max([n.get("ts_ms") or 0 for n in notes] + [v.get("end_ms") or 0 for v in visits] + [0])
    new_notes = sum(1 for n in notes if watermark_before is None or (n.get("ts_ms") or 0) > watermark_before)
    graph.record_run(run_id, {
        "rules_version": RULES_VERSION, "extractor_version": EXTRACTOR_VERSION,
        "extractor": extractor.extractor_id if extractor is not None else RULES_EXTRACTOR,
        "visits_read": len(visits), "notes_read": len(notes), "new_notes": new_notes,
        "watermark_ms": watermark, "patterns_written": len(patterns), "memories_written": len(memories),
        "notes_unverified": semantic["notes_unverified"], "history_checked": history is not None,
        "claims": semantic["claims"], "conflicts": semantic["conflicts"], "superseded": semantic["superseded"],
        "persons": semantic["persons"], "ambiguous_persons": semantic["ambiguous_persons"],
        "model_calls": semantic["model_calls"], "model_claims": semantic["model_claims"],
        "model_errors": len(semantic["model_errors"]), "finished_ms": int(time.time() * 1000),
    })
    return {
        "patterns": len(patterns),
        "memories": len(memories),
        "pattern_ids": patterns,
        "memory_ids": memories,
        "notes_unverified": semantic["notes_unverified"],
        "visits": len(visits),
        "notes": len(notes),
        "new_notes": new_notes,
        "run_id": run_id,
        "semantic": semantic,
    }
