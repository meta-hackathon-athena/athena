"""The Active Agent: observations in, decisions out.

  frame  -> ContextTracker (no DB, no LLM) -> trigger? -> handler -> decisions
  audio  -> dedup -> interpret (rules, LLM only if unsure) -> handler -> decisions
  decisions -> execute in order: MEMORY_UPDATE, NOTIFY, ASK_USER
            -> NotificationGate (dedup / cooldown / rate limit / expiry) -> Notifier
            -> AgentLog (audit)

NO_ACTION is the default. Most frames produce no trigger and return [] without any
evaluation at all; an evaluation that finds nothing returns a NO_ACTION decision.
Times are taken from the observations themselves, so a replayed day behaves like
a live one.
"""

from __future__ import annotations

import re
import statistics
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from memory.store import TZ

from .context import ContextTracker, LRUSet, Trigger, TrackerConfig
from .interpret import HybridInterpreter, Interpretation, content_words
from .memory_tools import AgentMemory, InvalidWrite, MemoryUnavailable, commitment_id
from .models import (ACTION_ORDER, Action, AudioObservation, Decision, InvalidObservation, Priority,
                     VisionObservation, no_action)
from .policy import CollectingNotifier, NotificationGate, Notifier, PolicyConfig
from .temporal import fmt_min, overlap, parse_time_expression
from .transit import TransitProvider

MIN = 60_000
HOUR = 60 * MIN
GENERIC = {"appointment", "meeting", "meet", "call", "plan", "with", "class", "tomorrow", "today",
           "tonight", "actually", "now", "will", "going", "have", "see", "you", "let", "lets"}


@dataclass
class AgentConfig:
    upcoming_lead_ms: int = 30 * MIN      # "Dentist in 25 min"
    answer_window_ms: int = 5 * MIN       # a spoken "yes" counts as an answer this long after asking
    question_defer_ms: int = 10 * MIN     # low-priority questions wait; don't interrupt the conversation
    question_block_ms: int = 20 * MIN     # an unanswered question holds back new ones this long, then
                                          # stays answerable in the app but stops blocking
    ask_horizon_ms: int = 48 * HOUR       # only interrupt about plans in the next 2 days
    question_check_ms: int = 2 * MIN      # how often ticks look for due questions
    upcoming_cache_ms: int = 10 * MIN
    retry_queue: int = 200


class Metrics:
    def __init__(self):
        self.c: Counter = Counter()
        self.lat: dict[str, list[float]] = {}
        self.first_ts = self.last_ts = None

    def time(self, key: str, ms: float) -> None:
        self.lat.setdefault(key, []).append(ms)

    def seen_ts(self, ts: int) -> None:
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

    def summary(self, llm_calls: int = 0, db_queries: int = 0, db_ms: list[float] | None = None) -> dict:
        def pct(xs, p):
            if not xs:
                return None
            xs = sorted(xs)
            return round(xs[min(len(xs) - 1, int(p * len(xs)))], 3)
        hours = max((self.last_ts - self.first_ts) / HOUR, 1e-9) if self.first_ts is not None else None
        obs = self.c["vision"] + self.c["audio"]
        out = {"counters": dict(sorted(self.c.items())), "llm_calls": llm_calls, "db_queries": db_queries,
               "llm_calls_per_observation": round(llm_calls / obs, 5) if obs else 0,
               "db_queries_per_observation": round(db_queries / obs, 4) if obs else 0,
               "simulated_hours": round(hours, 2) if hours else 0,
               "notifications_per_hour": round(self.c["delivered"] / hours, 2) if hours else 0,
               "latency_ms": {k: {"n": len(v), "p50": pct(v, .5), "p95": pct(v, .95), "max": pct(v, 1)}
                              for k, v in self.lat.items()}}
        if db_ms:
            out["latency_ms"]["db_query"] = {"n": len(db_ms), "p50": pct(db_ms, .5), "p95": pct(db_ms, .95),
                                             "max": pct(db_ms, 1)}
        return out


def _local(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, TZ)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _day_phrase(day, now: datetime) -> str:
    delta = (day - now.date()).days
    return {0: "today", 1: "tomorrow"}.get(delta, f"on {day.strftime('%a %d %b')}")


def _clock(ms: int) -> str:
    t = _local(ms)
    return fmt_min(t.hour * 60 + t.minute)


def _task(reminder_text: str) -> str:
    return re.sub(r"^(remind me to|remind me|don't forget to|dont forget to)\s+", "", reminder_text,
                  flags=re.I).rstrip(" .!")


def _span(c: dict) -> str:
    return _clock(c["start_ms"]) + (f"–{_clock(c['end_ms'])}" if c.get("end_ms") else "")


class ActiveAgent:
    def __init__(self, memory: AgentMemory, notifier: Notifier | None = None,
                 interpreter: HybridInterpreter | None = None, transit: TransitProvider | None = None,
                 config: AgentConfig | None = None, tracker_config: TrackerConfig | None = None,
                 policy_config: PolicyConfig | None = None,
                 context_provider: Callable[[int], Any] | None = None, recall=None):
        self.mem = memory
        # recall: agent.recall.MemoryRecall (memory-aware decisions from consolidated
        # memories); None = Milestone 1 behaviour (the standalone eval and simulator)
        self.recall = recall
        # context_provider(ts_ms) -> recent observed context (a context-engine
        # ContextSnapshot) for the integrated runtime; None when run standalone
        self.context_provider = context_provider
        self._event_id: str | None = None
        self.notifier = notifier or CollectingNotifier()
        self.interp = interpreter or HybridInterpreter()
        self.transit = transit
        self.cfg = config or AgentConfig()
        self.tracker = ContextTracker(tracker_config)
        self.audio_seen = LRUSet(5000)
        self.metrics = Metrics()
        self.retry: deque = deque(maxlen=self.cfg.retry_queue)
        self.log: list[Decision] = []          # everything decided (also when the DB is down)
        try:                                   # restart-safe dedup: reload recent notification keys
            # relative to the newest logged decision as well as the wall clock, so a
            # replayed (simulated-time) day reloads its own history after a restart
            now, latest = int(time.time() * 1000), memory.latest_log_ms()
            history = memory.recent_dedup_keys(min(now, latest or now) - 7 * 24 * HOUR)
        except MemoryUnavailable:
            history = {}
        self.gate = NotificationGate(policy_config, history)
        self._upcoming: tuple[int, list[dict]] | None = None
        self._last_q_check = self._last_expire = 0

    # ============================================================ entry points
    def handle(self, raw: dict) -> list[Decision]:
        return self.handle_audio(raw) if "transcript" in raw else self.handle_vision(raw)

    def handle_vision(self, raw) -> list[Decision]:
        t0 = time.perf_counter()
        self.metrics.c["vision"] += 1
        try:
            obs = raw if isinstance(raw, VisionObservation) else VisionObservation.parse(raw)
        except InvalidObservation as e:
            self.metrics.c["invalid"] += 1
            return [no_action(f"invalid observation: {e}", 0)]
        self.metrics.seen_ts(obs.ts_ms)
        self._event_id = obs.frame_id
        before = dict(self.tracker.stats)
        triggers = self.tracker.observe(obs)
        if self.tracker.stats["duplicate"] > before["duplicate"]:
            self.metrics.c["duplicate_frames"] += 1
        if self.tracker.stats["stale"] > before["stale"]:
            self.metrics.c["stale_frames"] += 1
        out = self._run(triggers) if triggers else []
        if not triggers:
            self.metrics.c["frames_filtered"] += 1
        self.metrics.time("frame" if not triggers else "frame_with_trigger", (time.perf_counter() - t0) * 1000)
        return out

    def handle_audio(self, raw) -> list[Decision]:
        t0 = time.perf_counter()
        self.metrics.c["audio"] += 1
        try:
            a = raw if isinstance(raw, AudioObservation) else AudioObservation.parse(raw)
        except InvalidObservation as e:
            self.metrics.c["invalid"] += 1
            return [no_action(f"invalid observation: {e}", 0, "audio")]
        self.metrics.seen_ts(a.ts_ms)
        self._event_id = a.segment_id
        if a.segment_id in self.audio_seen.d or a.content_key in self.audio_seen.d:
            self.metrics.c["duplicate_audio"] += 1
            return [no_action("duplicate audio segment", a.ts_ms, "audio")]
        out = self._run(self.tracker.advance(a.ts_ms))
        try:
            decisions = self._on_audio(a)
        except MemoryUnavailable as e:
            out += self._failed(a, a.ts_ms, "audio", e)
        else:
            self.audio_seen.seen(a.segment_id)      # only once it was really handled (retry-safe)
            self.audio_seen.seen(a.content_key)
            out += self._execute(decisions, log_no_action=True)
        self.metrics.time("audio", (time.perf_counter() - t0) * 1000)
        return out

    def answer(self, question_id: str, answer: str, ts_ms: int | None = None) -> list[Decision]:
        """The user answered a question in the app (or the notification's buttons)."""
        ts_ms = ts_ms or self.tracker.clock_ms or int(time.time() * 1000)
        try:
            q = self.mem.get_question(question_id)
            if not q or q["status"] not in ("asked", "pending") or q["expires_ms"] <= ts_ms:
                return [no_action("question not open (unknown, answered or expired)", ts_ms, "answer")]
            return self._execute(self._resolve(q, answer, ts_ms), log_no_action=True)
        except MemoryUnavailable as e:
            return self._failed(None, ts_ms, "answer", e)

    def advance_clock(self, ts_ms: int, event_id: str) -> list[Decision]:
        """Run time-based policy without inventing a sensor observation."""
        self.metrics.seen_ts(ts_ms)
        self._event_id = event_id
        return self._run(self.tracker.advance(ts_ms))

    def retry_failed(self) -> list[Decision]:
        """Re-run work that failed while FalkorDB was down. Dedup keys make this safe."""
        out, pending = [], list(self.retry)
        self.retry.clear()
        for item in pending:
            if isinstance(item, AudioObservation):
                out += self.handle_audio(item)
            else:
                self._event_id = item.data.get("event_id")       # cite the frame that raised it
                out += self._run([item])
        return out

    # ================================================================ plumbing
    def _failed(self, item, ts_ms: int, trigger: str, err: Exception) -> list[Decision]:
        self.metrics.c["failed_evaluations"] += 1
        if item is not None:
            if isinstance(item, Trigger):
                item.data.setdefault("event_id", self._event_id)
            self.retry.append(item)
        d = no_action("memory unavailable; evaluation skipped, will retry (no memory was guessed)", ts_ms, trigger)
        d.status = "failed"
        d.trigger_event_id = self._event_id
        self.log.append(d)
        return [d]

    def _run(self, triggers: list[Trigger]) -> list[Decision]:
        out = []
        handlers = {"tick": self._on_tick, "place_entered": self._on_place, "location_changed": self._on_location,
                    "bus_stop_wait": self._on_bus_stop, "boarded": lambda t: []}
        for t in triggers:
            self.metrics.c[f"trigger_{t.kind}"] += 1
            t0 = time.perf_counter()
            try:
                ds = handlers[t.kind](t)
            except MemoryUnavailable as e:
                out += self._failed(t, t.ts_ms, t.kind, e)
                continue
            out += self._execute(ds, log_no_action=t.kind != "tick")
            self.metrics.time(f"trigger_{t.kind}", (time.perf_counter() - t0) * 1000)
        return out

    def _execute(self, decisions: list[Decision], log_no_action: bool = False) -> list[Decision]:
        """Run decisions in a fixed order. Each one succeeds or fails on its own (no false
        atomicity); every outcome is recorded on the decision and in the audit log."""
        for d in sorted(decisions, key=lambda d: ACTION_ORDER[d.action]):
            self.metrics.c[f"decision_{d.action.value}"] += 1
            if d.trigger_event_id is None:
                d.trigger_event_id = self._event_id
            if d.action == Action.MEMORY_UPDATE:
                op = d.proposed_memory_update or {}
                try:
                    res = self.mem.apply(op.get("op", ""), {**op.get("args", {}), "now_ms": d.ts_ms})
                    d.status = "done"
                    self.metrics.c["memory_writes"] += 1
                    self._upcoming = None             # commitments may have changed
                    if isinstance(res, str) and res not in d.related_memory_ids:
                        d.related_memory_ids.append(res)
                except (InvalidWrite, TypeError) as e:
                    d.status, d.reason = "failed", f"{d.reason} (write rejected: {e})"
                    self.metrics.c["invalid_writes"] += 1
                except MemoryUnavailable:
                    d.status = "failed"
                    self.metrics.c["failed_writes"] += 1
            elif d.action in (Action.NOTIFY, Action.ASK_USER):
                why = self.gate.check(d, d.ts_ms)
                if why:
                    d.status = "deferred" if d.action == Action.ASK_USER and why == "rate_limited" else "suppressed"
                    d.reason = f"{d.reason} [{why}]"
                    self.metrics.c[f"suppressed_{why}"] += 1
                else:
                    try:
                        ok = self.notifier.deliver(d)
                    except Exception:
                        ok = False
                    if ok:
                        self.gate.record(d, d.ts_ms)
                        d.status = "done"
                        self.metrics.c["delivered"] += 1
                        if d.question_id:
                            try:
                                self.mem.mark_question(d.question_id, "asked", d.ts_ms)
                            except MemoryUnavailable:
                                pass
                    else:
                        d.status = "failed"            # not recorded in the gate, so it can be retried
                        self.metrics.c["delivery_failures"] += 1
            else:
                d.status = "done"
            self.log.append(d)
            if d.status == "suppressed" and "[duplicate]" in d.reason:
                continue                               # already logged the first time
            if d.action != Action.NO_ACTION or log_no_action:
                try:
                    self.mem.log(d)
                except MemoryUnavailable:
                    self.metrics.c["log_failures"] += 1
        return decisions

    def _ask(self, *, kind: str, text: str, key: str, ts: int, expires: int, priority: Priority,
             on_yes: dict | None, about: str | None = None, defer: bool = False, extra: dict | None = None,
             reason: str, related: list[str] | None = None) -> Decision:
        """Every question is stored first (so it survives restarts and can be answered later),
        then asked now, or left pending for a later tick."""
        not_before = ts + self.cfg.question_defer_ms if defer else None
        qid, created = self.mem.save_question(
            kind=kind, text=text, dedup_key=key, now_ms=ts, expires_ms=expires, priority=priority.value,
            payload={"on_yes": on_yes, **(extra or {})}, about=about, not_before_ms=not_before)
        if not created:
            return no_action(f"same question already exists ({qid})", ts, "audio")
        if defer:
            return no_action(f"clarification queued for later: {text}", ts, "audio")
        open_q = self.mem.outstanding_question(ts)
        if open_q and priority != Priority.HIGH and ts - open_q.get("asked_ms", 0) < self.cfg.question_block_ms:
            return no_action("clarification queued: another question is still open", ts, "audio")
        return Decision(Action.ASK_USER, reason, ts, priority, message=text, question_id=qid,
                        deduplication_key=f"question:{qid}", expires_ms=expires,
                        related_memory_ids=[qid] + (related or []), trigger="audio")

    # ============================================================ vision paths
    def _on_tick(self, t: Trigger) -> list[Decision]:
        now, out = t.ts_ms, []
        if self._upcoming is None or now - self._upcoming[0] >= self.cfg.upcoming_cache_ms:
            self._upcoming = (now, self.mem.upcoming_commitments(now, self.cfg.upcoming_cache_ms + self.cfg.upcoming_lead_ms))
        for c in self._upcoming[1]:
            if not (now < c["start_ms"] <= now + self.cfg.upcoming_lead_ms) or c.get("status") == "cancelled":
                continue
            if self.gate.recently_sent(f"upcoming:{c['id']}", now):
                continue
            mins = max(1, (c["start_ms"] - now) // MIN)
            lead = "You mentioned " if c.get("status") == "tentative" else ""
            out.append(Decision(Action.NOTIFY, "commitment starting soon", now, Priority.NORMAL,
                                message=f"{lead}{c['title']} at {_span(c)} (in {mins} min).",
                                related_memory_ids=[c["id"]], deduplication_key=f"upcoming:{c['id']}",
                                expires_ms=c["start_ms"], trigger="tick", confidence=0.9))
        if now - self._last_q_check >= self.cfg.question_check_ms:
            self._last_q_check = now
            if now - self._last_expire >= 30 * MIN:          # tidy-up only; due_questions already skips expired
                self._last_expire = now
                self.mem.expire_questions(now)
            open_q = self.mem.outstanding_question(now)
            if not open_q or now - open_q.get("asked_ms", 0) >= self.cfg.question_block_ms:
                for q in self.mem.due_questions(now, limit=1):     # one at a time, oldest first
                    out.append(Decision(Action.ASK_USER, f"pending {q['source']} question is due", now,
                                        Priority(q.get("priority", "low")), message=q["text"],
                                        question_id=q["id"], deduplication_key=f"question:{q['id']}",
                                        expires_ms=q["expires_ms"], related_memory_ids=[q["id"]], trigger="tick"))
        return out

    def _reminder_decisions(self, place_type: str, ts: int, trigger: str, place: dict | None = None) -> list[Decision]:
        if place_type in ("unknown", None):
            return []
        out = []
        where = place["name"] if place and place.get("name") and not place.get("sensitive") else place_type.replace("_", " ")
        for r in self.mem.open_reminders(place_type):
            if self.gate.recently_sent(f"reminder:{r['id']}", ts):
                continue
            what = _task(r["text"])
            out.append(Decision(Action.NOTIFY, f"open reminder matches current place ({place_type})", ts,
                                Priority.NORMAL, message=f"You're at the {where}. Reminder: {what}",
                                related_memory_ids=[r["id"]] + ([place["id"]] if place else []),
                                deduplication_key=f"reminder:{r['id']}", trigger=trigger, confidence=0.85,
                                expires_ms=ts + 15 * MIN))
        return out

    def _on_place(self, t: Trigger) -> list[Decision]:
        pt = t.data["place_type"]
        return (self._reminder_decisions(pt, t.ts_ms, "place_entered")
                or [no_action(f"arrived at {pt}; nothing relevant", t.ts_ms, "place_entered")])

    def _on_location(self, t: Trigger) -> list[Decision]:
        place = self.mem.place_near(t.data["lat"], t.data["lon"])
        if not place:
            return [no_action("no known place here", t.ts_ms, "location_changed")]
        return (self._reminder_decisions(place.get("place_type"), t.ts_ms, "location_changed", place)
                or [no_action(f"at known place {place['id']}; nothing relevant", t.ts_ms, "location_changed")])

    def _on_bus_stop(self, t: Trigger) -> list[Decision]:
        now = t.ts_ms
        if not self.transit:
            return [no_action("waiting at a bus stop; no transit service configured", now, "bus_stop_wait")]
        if self.tracker.boarded_ms:
            return [no_action("already on the bus", now, "bus_stop_wait")]
        try:
            raw = self.transit.arrivals(t.data.get("lat"), t.data.get("lon"), now)
        except Exception:
            return [no_action("transit service unavailable; no arrival times invented", now, "bus_stop_wait")]
        arrivals = [a for a in raw if isinstance(a, dict) and re.fullmatch(r"[A-Za-z0-9]{1,5}", str(a.get("route", "")))
                    and isinstance(a.get("minutes"), int) and 0 <= a["minutes"] <= 60][:10]
        preferred = {m.group(1).upper() for s in self.mem.preference_statements()
                     for m in re.finditer(r"\bbus (\d+[a-z]?)\b", s.lower())}
        if preferred:
            arrivals = [a for a in arrivals if str(a["route"]).upper() in preferred] or arrivals
        if not arrivals:
            return [no_action("no usable arrival times", now, "bus_stop_wait")]
        arrivals.sort(key=lambda a: a["minutes"])
        label = " (mock data)" if getattr(self.transit, "source", "") == "mock" else ""
        msg = ", ".join(f"Bus {a['route']} in {a['minutes']} min" for a in arrivals[:2]) + label
        loc = f"{t.data['lat']:.3f},{t.data['lon']:.3f}" if t.data.get("lat") is not None else "nogps"
        return [Decision(Action.NOTIFY, f"waited {t.data.get('waited_s')}s at a bus stop", now, Priority.LOW,
                         message=msg, deduplication_key=f"transit:{loc}:{now // (10 * MIN)}",
                         expires_ms=now + 2 * MIN, trigger="bus_stop_wait", confidence=0.6)]

    def recent_context(self, ts_ms: int):
        """Recent observed context from the context engine (None when unavailable).
        Context is an aid, never a dependency: failures here never block a decision."""
        if not self.context_provider:
            return None
        try:
            return self.context_provider(ts_ms)
        except Exception:
            self.metrics.c["context_failures"] += 1
            return None

    def _context_text(self, ts_ms: int) -> str | None:
        snap = self.recent_context(ts_ms)
        if snap is None:
            return None
        if isinstance(snap, str):
            return snap
        from context_engine import render_prompt         # only in the integrated runtime
        return render_prompt(snap).text

    # ============================================================= audio paths
    def _on_audio(self, a: AudioObservation) -> list[Decision]:
        ts, now = a.ts_ms, _local(a.ts_ms)
        it = self.interp.interpret(a, now, context=lambda: self._context_text(ts))
        decisions = self._on_audio_rules(a, it, ts, now)
        if self.recall is not None:
            t0 = time.perf_counter()
            recalled = self.recall.assess(a, it, ts)
            self.metrics.time("recall", (time.perf_counter() - t0) * 1000)
            self.metrics.c["recall_decisions"] += sum(d.action != Action.NO_ACTION for d in recalled)
            decisions += recalled
        return decisions

    def _on_audio_rules(self, a: AudioObservation, it: Interpretation, ts: int, now: datetime) -> list[Decision]:
        self.metrics.c[f"intent_{it.intent}"] += 1
        if it.suspicious and it.intent != "command":
            return [no_action("instruction-like content in a transcript was treated as conversation", ts, "audio")]

        open_q = self.mem.outstanding_question(ts) if a.speaker_is_user else None
        if open_q and ts - open_q.get("asked_ms", 0) > self.cfg.answer_window_ms:
            open_q = None
        if it.intent == "answer":
            if not open_q:
                return [no_action("yes/no heard but no question is open", ts, "audio")]
            return self._resolve(open_q, it.answer, ts)
        if open_q and open_q["kind"] == "clarify_time" and it.time and it.time.has_time and it.intent != "command":
            return self._resolve(open_q, a.transcript, ts)

        if it.intent == "command":
            return self._on_command(it, a)
        if it.intent == "completion":
            return self._on_completion(it, a, explicit=False)
        if it.intent == "commitment":
            return self._on_commitment(it, a)
        if it.intent == "reminder":
            return [no_action("reminder stored by ingestion; it triggers at the matching place", ts, "audio")]
        return [no_action("nothing actionable in the utterance", ts, "audio")]

    def _on_command(self, it: Interpretation, a: AudioObservation) -> list[Decision]:
        ts = a.ts_ms
        if it.command == "forbidden":
            return [Decision(Action.NOTIFY, "voice command outside the agent's permissions", ts, Priority.LOW,
                             message="I can't delete or rewrite memories by voice. You can do that in the app.",
                             deduplication_key=f"refuse:{a.speaker_id or 'user'}", trigger="audio")]
        if it.command == "complete":
            return self._on_completion(it, a, explicit=True)
        if it.command == "add_commitment":
            return self._on_commitment(it, a, explicit=True)
        return [no_action("unrecognised command", ts, "audio")]

    def _on_completion(self, it: Interpretation, a: AudioObservation, explicit: bool) -> list[Decision]:
        ts = a.ts_ms
        words = set(it.keywords)
        scored = sorted(((len(words & set(content_words(r["text"]))), r) for r in self.mem.open_reminders()),
                        key=lambda x: -x[0])
        matches = [r for s, r in scored if s > 0 and s == scored[0][0]]
        if not matches:
            return [no_action("no open reminder matches", ts, "audio")]
        r = matches[0]
        on_yes = {"op": "complete_reminder", "args": {"note_id": r["id"]}}
        if explicit and len(matches) == 1:
            return [Decision(Action.MEMORY_UPDATE, "user asked to mark the reminder done", ts, Priority.LOW,
                             message=f"Marked done: {r['text']}", proposed_memory_update=on_yes,
                             related_memory_ids=[r["id"]], trigger="audio")]
        return [self._ask(kind="complete_reminder", text=f"Mark \"{_task(r['text'])}\" as done?", key=f"complete:{r['id']}",
                          ts=ts, expires=ts + 6 * HOUR, priority=Priority.LOW, on_yes=on_yes, about=r["id"],
                          reason="user may have completed an open reminder", related=[r["id"]])]

    def _on_commitment(self, it: Interpretation, a: AudioObservation, explicit: bool = False) -> list[Decision]:
        ts, now = a.ts_ms, _local(a.ts_ms)
        spec, title = it.time, it.title or "Plan"
        user = it.speaker == "user"
        if spec is None or spec.date is None:
            return [no_action("plan mentioned without a usable date or time", ts, "audio")]
        day = _day_phrase(spec.date, now)
        day_start = _ms(datetime.combine(spec.date, datetime.min.time(), TZ))
        evidence = {"evidence_segment": a.segment_id, "evidence_text": a.transcript, "evidence_ts_ms": a.ts_ms}

        if not spec.has_time:
            if user and (explicit or it.strength == "definite") and day_start - ts < 7 * 24 * HOUR:
                return [self._ask(kind="clarify_time", text=f"What time is {title} {day}?", key=f"time:{title.lower()}:{spec.date}",
                                  ts=ts, expires=day_start + 24 * HOUR, priority=Priority.LOW, on_yes=None,
                                  defer=not explicit, reason="plan has a day but no time",
                                  extra={"date": spec.date.isoformat(), "title": title, "people": it.people,
                                         "place_text": it.place_text, "source": "user_answer"})]
            return [no_action("plan without a time; nothing to check yet", ts, "audio")]

        cands = spec.intervals(TZ)
        start, end = cands[0]
        where = f" at the {it.place_text}" if it.place_text else ""
        when = f"{day}{where} at {fmt_min(spec.starts[0])}"
        if spec.time_ambiguous:
            when += f" (or {fmt_min(spec.starts[1])})"
        cid = commitment_id(title, start)
        record = {"op": "record_commitment", "args": {
            "title": title, "start_ms": start, "end_ms": end, "people": it.people, "place_text": it.place_text,
            "time_ambiguous": spec.time_ambiguous, **evidence}}

        # 1. Same plan we already know? A different time for it? (never overwrite silently)
        kw = (set(it.keywords) - GENERIC) | {p.lower() for p in it.people}
        nearby = self.mem.commitments_between(ts, ts + 14 * 24 * HOUR)
        for e in nearby:
            ekw = (set(content_words(e["title"])) - GENERIC) | {p.lower() for p in e.get("people", [])}
            if not (kw & ekw):
                continue
            if any(abs(s - e["start_ms"]) < MIN for s, _ in cands):
                return [no_action(f"already known: {e['title']} {_span(e)}", ts, "audio")]
            if it.speaker == "other":
                continue
            fix = {"op": "update_commitment_time", "args": {"cid": e["id"], "start_ms": start, "end_ms": end}}
            old_day = _local(e["start_ms"]).date()
            new_t = fmt_min(spec.starts[0]) + (f" (or {fmt_min(spec.starts[1])})" if spec.time_ambiguous else "")
            old_at = _span(e) if old_day == spec.date else f"{_span(e)} {_day_phrase(old_day, now)}"
            return [self._ask(kind="confirm_correction", key=f"correct:{e['id']}:{start}", ts=ts,
                              text=f"Did {e['title']} change from {old_at} to {new_t} {day}{where}?",
                              expires=min(e["start_ms"], start), priority=Priority.NORMAL, on_yes=fix, about=e["id"],
                              reason="new statement contradicts a stored commitment; not changed without confirmation",
                              related=[e["id"]])]

        # 2. Deterministic overlap check against that day's commitments only
        conflicts = []
        for e in self.mem.commitments_on(spec.date):
            kinds = [overlap(s, en, e["start_ms"], e.get("end_ms")) for s, en in cands]
            if all(k == "conflict" for k in kinds):
                conflicts.append((e, "conflict"))
            elif any(kinds):
                conflicts.append((e, "possible"))
        resolved_definite = user and (explicit or it.strength == "definite") and spec.resolved
        soon = start - ts <= self.cfg.ask_horizon_ms

        if conflicts:
            e, level = sorted(conflicts, key=lambda x: x[1] != "conflict")[0]
            certain = level == "conflict" and spec.resolved
            pri = Priority.HIGH if level == "conflict" else Priority.NORMAL
            clash = f"{e['title']} ({_span(e)})"
            key = f"conflict:{e['id']}:{cid}"
            if resolved_definite:
                status = "confirmed" if explicit else "tentative"
                rec = {**record, "args": {**record["args"], "status": status,
                                          "source": "user_command" if explicit else "user_speech"}}
                verb = "overlaps" if certain else "may overlap"
                return [Decision(Action.MEMORY_UPDATE, f"user stated a plan; saved as {status}", ts, Priority.LOW,
                                 proposed_memory_update=rec, related_memory_ids=[cid], trigger="audio", confidence=0.8),
                        Decision(Action.NOTIFY, f"{level} with a stored commitment", ts, pri,
                                 message=f"Heads up: {title} {when} {verb} {clash}.",
                                 related_memory_ids=[e["id"], cid], deduplication_key=key, trigger="audio",
                                 expires_ms=start, confidence=0.9 if certain else 0.6)]
            who = "" if user else " (I'm not sure who said it)"
            confirm = {**record, "args": {**record["args"], "status": "confirmed", "source": "user_answer"}}
            return [self._ask(kind="confirm_commitment", key=key, ts=ts, expires=start, priority=pri, on_yes=confirm,
                              text=f"Did you agree to {title} {when}?{who} It would {'clash' if certain else 'possibly clash'} "
                                   f"with {clash}.",
                              reason=f"{level} with a stored commitment, but the plan is unconfirmed", related=[e["id"]])]

        if resolved_definite:
            status = "confirmed" if explicit else "tentative"
            rec = {**record, "args": {**record["args"], "status": status,
                                      "source": "user_command" if explicit else "user_speech"}}
            return [Decision(Action.MEMORY_UPDATE, f"user stated a plan; no conflict; saved as {status}", ts,
                             Priority.LOW, proposed_memory_update=rec, related_memory_ids=[cid], trigger="audio",
                             message=f"Saved: {title} {when}." if explicit else None, confidence=0.8)]
        if user and soon and (explicit or it.strength in ("definite", "proposal")):
            confirm = {**record, "args": {**record["args"], "status": "confirmed", "source": "user_answer"}}
            ambiguous = spec.time_ambiguous or spec.date_ambiguous or spec.date_assumed
            text = (f"Is {title} {_day_phrase(spec.date, now)} at {fmt_min(spec.starts[0])}?" if ambiguous
                    else f"Did you agree to {title} {when}? I can save it.")
            return [self._ask(kind="clarify_time" if ambiguous else "confirm_commitment", text=text,
                              key=f"plan:{cid}", ts=ts, expires=start, priority=Priority.LOW, on_yes=confirm,
                              defer=not explicit, reason="plan needs confirmation before it is saved",
                              extra={"date": spec.date.isoformat(), "title": title, "people": it.people,
                                     "place_text": it.place_text, "source": "user_answer"})]
        if not user:
            return [no_action("possible plan from an unverified speaker; not attributed to the user", ts, "audio")]
        return [no_action("plan is far off or vague; left for later review", ts, "audio")]

    # ================================================================ answers
    def _resolve(self, q: dict, answer: str, ts: int) -> list[Decision]:
        self.mem.mark_question(q["id"], "answered", ts, answer[:100])
        p, low = q.get("payload") or {}, answer.strip().lower()
        yes = low in ("yes", "y") or bool(re.match(r"^(yes|yeah|yep|sure|correct|ok|okay|confirm)\b", low))
        no = low in ("no", "n") or bool(re.match(r"^(no|nope|nah|cancel)\b", low))
        if q["kind"] == "dreamer":
            return [no_action("answer stored on the question for the Dreamer", ts, "answer")]
        if no:
            return [no_action("user declined; nothing changed", ts, "answer")]
        op = p.get("on_yes")
        if not yes and q["kind"] == "clarify_time" and p.get("date"):
            day = datetime.fromisoformat(p["date"]).date()
            spec = parse_time_expression(answer, _local(ts))
            if not spec or not spec.starts:
                return [no_action("could not read a time from the answer", ts, "answer")]
            spec.date = day
            start, end = spec.intervals(TZ)[0]
            op = {"op": "record_commitment", "args": {
                "title": p["title"], "start_ms": start, "end_ms": end, "people": p.get("people") or [],
                "place_text": p.get("place_text"), "time_ambiguous": spec.time_ambiguous,
                "status": "confirmed", "source": "user_answer"}}
        elif not yes:
            return [no_action("answer not understood as yes or no", ts, "answer")]
        if not op:
            return [no_action("answer recorded", ts, "answer")]
        return [Decision(Action.MEMORY_UPDATE, f"user confirmed ({q['kind']})", ts, Priority.LOW,
                         proposed_memory_update=op, related_memory_ids=[q["id"]], trigger="answer", confidence=0.98)]

    def summary(self) -> dict:
        out = self.metrics.summary(self.interp.llm_calls, self.mem.queries, self.mem.query_ms)
        if self.recall is not None:
            out["recall"] = dict(self.recall.stats)
        return out
