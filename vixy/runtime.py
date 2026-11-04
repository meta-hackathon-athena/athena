"""The integrated Vixy runtime (in-process, one user).

    normalized event (or perception output -> adapters)
        -> ContextEngine.ingest_*        validate, dedupe, commit to SQLite (canonical record)
        -> ledger.begin                  side effects of this event are now "in progress"
        -> EpisodeWriter                 audio: Note (before the agent!)  video: Segmenter -> Visit
        -> ActiveAgent.handle            the CE event mapped to the agent's input; the agent can
                                         pull the recent ContextSnapshot through context_provider
        -> ledger.done                   decision ids recorded; AgentLog nodes carry trigger_event_id

Milestone 2: the agent is memory-aware (``agent.recall.MemoryRecall`` over the Dreamer's
consolidated memories, through bounded read-only retrieval), and the runtime answers
natural-language questions (``ask``, see ``vixy.ask``) and runs the Dreamer (``dream``).

There is no other way into the agent: ``VixyRuntime`` never hands it anything that did not
come back out of Context Engine. An identical re-send is a no-op once its side effects are
done; an event whose side effects were interrupted (crash, FalkorDB down) is finished by
``recover()`` (also run at start-up). Each failure is reported with its stage:

    rejected   invalid payload or a session not mapped to this user (nothing stored)
    conflict   event_id reused with different content (CE 409; nothing changed)
    failed     stored in CE, but the episode or agent stage failed; recover() retries it
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import ValidationError

from agent.engine import ActiveAgent
from agent.interpret import HybridInterpreter
from agent.memory_tools import AgentMemory
from agent.policy import CollectingNotifier, Notifier
from agent.recall import MemoryRecall
from context_engine import AudioSegment, ContextEngine, EventConflictError, VideoObservation
from memory.store import MemoryStore

from . import adapters, schema
from .config import VixyConfig, connect_graph
from .episode_writer import EpisodeWriter
from .ledger import Ledger
from .memory_api import SharedMemory

logger = logging.getLogger("vixy.runtime")   # ids and statuses only, never transcript text

Status = Literal["processed", "recovered", "duplicate", "rejected", "conflict", "failed"]


@dataclass
class EventOutcome:
    status: Status
    event_id: str | None
    kind: str | None
    stage: str | None = None              # ingest | episode | agent (where it stopped)
    error: str | None = None
    ingest_seq: int | None = None
    note_id: str | None = None
    visit_ids: list[str] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("processed", "recovered", "duplicate")

    def to_dict(self) -> dict:
        return asdict(self)


class VixyRuntime:
    def __init__(self, config: VixyConfig | None = None, *, graph=None, engine: ContextEngine | None = None,
                 notifier: Notifier | None = None, interpreter: HybridInterpreter | None = None,
                 transit=None, recover: bool = True, reasoner=None, answer_model=None,
                 memory_recall: bool = True):
        """reasoner / answer_model: optional budgeted structured models
        (prod_dreamer_model.model_extract.BudgetedModel) for the agent's uncertain recall
        cases and for rephrasing /ask answers. None (the default) = deterministic only."""
        self.config = config or VixyConfig.from_env()
        from prod_dreamer_model.config import is_contributor_model
        for model in (reasoner, answer_model):
            if model and is_contributor_model(model.model_id):
                raise ValueError("contributor models are forbidden for the integrated wearable runtime")
        self.session_id = self.config.default_session
        self.engine = engine or ContextEngine(self.config.events_db)
        self._owns_engine = engine is None
        self.graph = graph if graph is not None else connect_graph(self.config)
        schema.bootstrap(self.graph, self.config.user_id)
        self.store = MemoryStore(self.graph)
        self.agent_memory = AgentMemory(self.store)
        self.agent_memory.setup()
        self.ledger = Ledger(self.config.runtime_db)
        self.writer = EpisodeWriter(self.store, self.config.user_id, self.session_id, self.ledger)
        self.writers = {session: (self.writer if session == self.session_id else
                        EpisodeWriter(self.store, self.config.user_id, session, self.ledger))
                        for session in self.config.session_ids}
        self._current_session = self.session_id
        self.notifier = notifier or CollectingNotifier()
        # rules only unless an interpreter (with an LLM) is passed explicitly: no paid calls by default
        self.recall = MemoryRecall(self.agent_memory, reasoner) if memory_recall else None
        self.agent = ActiveAgent(self.agent_memory, self.notifier, interpreter or HybridInterpreter(None),
                                 transit=transit, context_provider=self.context_at, recall=self.recall)
        self.memory = SharedMemory(self.graph)
        from .ask import AskService                      # after self.memory: it reads through it
        self.asker = AskService(self, model=answer_model)
        self.retried_decisions: list[dict] = []
        self.startup: dict[str, Any] = {}
        if recover:
            self.startup = {"rehydrate": self.writer.rehydrate(self.engine)}
            for session, writer in self.writers.items():
                if session != self.session_id:
                    writer.rehydrate(self.engine)
            self.startup["recovered"] = [o.event_id for o in self.recover()]

    def close(self) -> None:
        self.ledger.close()
        if self._owns_engine:
            self.engine.close()

    def __enter__(self) -> "VixyRuntime":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ entry points
    def ingest_video(self, observation: VideoObservation | dict) -> EventOutcome:
        return self._ingest("video", observation)

    def ingest_audio(self, segment: AudioSegment | dict) -> EventOutcome:
        return self._ingest("audio", segment)

    def ingest_perception_frame(self, raw: dict, stream: str = "cam0") -> EventOutcome:
        try:
            obs = adapters.video_from_perception(raw, session_id=self.session_id, user_id=self.config.user_id,
                                                 stream=stream)
        except (ValidationError, ValueError, KeyError, TypeError) as e:
            return EventOutcome("rejected", raw.get("frame_id"), "video", "ingest", _err(e))
        return self.ingest_video(obs)

    def ingest_transcription(self, raw: dict, stream: str = "mic0", **kw) -> EventOutcome:
        try:
            seg = adapters.audio_from_transcription(raw, session_id=self.session_id, user_id=self.config.user_id,
                                                    stream=stream, **kw)
        except (ValidationError, ValueError, KeyError, TypeError) as e:
            return EventOutcome("rejected", raw.get("segment_id"), "audio", "ingest", _err(e))
        return self.ingest_audio(seg)

    def flush_episodes(self, session_id: str | None = None) -> list[str]:
        """End of a recording/day: close and store the open visit."""
        writers = [self.writers[session_id]] if session_id else self.writers.values()
        return [vid for writer in writers for vid in writer.flush()]

    def context_at(self, ts_ms: int):
        """The recent ContextSnapshot the agent sees (window ending at the event's time)."""
        as_of = datetime.fromtimestamp(ts_ms / 1000, timezone.utc)
        return self.engine.get_context(self._current_session, as_of=as_of, window_seconds=self.config.context_window_s)

    def clock_ms(self) -> int:
        """The runtime's notion of "now": the newest observation it has seen (simulated time
        for replays), else the newest episode in the graph, else the wall clock."""
        latest = max(self.agent.metrics.last_ts or 0, self.ledger.get_kv("clock_ms", 0))
        if latest:
            return int(latest)
        rows = self.store.q("MATCH (n) WHERE n:Note OR n:Visit RETURN max(coalesce(n.end_ms, n.ts_ms))")
        if rows and rows[0][0]:
            return int(rows[0][0])
        return int(datetime.now(timezone.utc).timestamp() * 1000)

    def advance_clock(self, ts_ms: int, clock_id: str) -> list[dict]:
        """Idempotent simulated ticks; persisted separately from observed CE events."""
        key = f"clock:{clock_id}"
        if self.ledger.get_kv(key):
            return []
        if ts_ms < self.ledger.get_kv("clock_ms", 0):
            raise ValueError("simulated clock cannot move backwards")
        decisions = [d.to_dict() for d in self.agent.advance_clock(ts_ms, clock_id)]
        if any(d["status"] == "failed" for d in decisions):
            raise RuntimeError("clock evaluation failed; replay can retry")
        self.ledger.set_kv("clock_ms", ts_ms)
        self.ledger.set_kv(key, True)
        return decisions

    def dream(self, **kw) -> dict:
        """Run the Dreamer on this graph and event history, then let the agent see the new memories."""
        from .dream_job import run_dream
        kw.setdefault("now_ms", self.clock_ms())
        result = run_dream(self.config, self.graph, self.engine, **kw)
        if self.recall is not None:
            self.recall.invalidate()
        return result

    def ask(self, question: str, conversation_id: str | None = None, now_ms: int | None = None) -> dict:
        """A natural-language question about the past, answered from the graph (vixy.ask.v1)."""
        return self.asker.ask(question, conversation_id=conversation_id, now_ms=now_ms)

    def recover(self) -> list[EventOutcome]:
        """Finish every event that is in Context Engine but whose side effects are not done."""
        out = []
        for session in self.config.session_ids:
            for rec in self.engine.records_after(session, self.ledger.resume_after(session)):
                if self.ledger.status(rec.event_id) != "done":
                    out.append(self._process(rec, recovering=True))
        # place/tick triggers the agent queued while memory was down (in this process);
        # its dedup keys make the re-run safe
        self.retried_decisions = [d.to_dict() for d in self.agent.retry_failed()]
        return out

    # ----------------------------------------------------------------- plumbing
    def _ingest(self, kind: str, payload) -> EventOutcome:
        model = AudioSegment if kind == "audio" else VideoObservation
        try:
            event = payload if isinstance(payload, model) else model.model_validate(payload)
        except ValidationError as e:
            eid = payload.get("event_id") if isinstance(payload, dict) else None
            return EventOutcome("rejected", eid, kind, "ingest", _err(e))
        if not self.config.owns_session(event.session_id):
            return EventOutcome("rejected", event.event_id, kind, "ingest",
                                f"session {event.session_id!r} is not mapped to user {self.config.user_id!r}")
        try:
            res = (self.engine.ingest_audio if kind == "audio" else self.engine.ingest_video)(event)
        except EventConflictError as e:
            return EventOutcome("conflict", event.event_id, kind, "ingest", str(e))
        if res.status == "duplicate" and self.ledger.status(event.event_id) == "done":
            row = self.ledger.get(event.event_id)
            return EventOutcome("duplicate", event.event_id, kind, ingest_seq=row["ingest_seq"])
        rec = self.engine.get_event_record(event.event_id)
        # a duplicate whose side effects never finished (crash after the CE commit) is finished now
        return self._process(rec, recovering=res.status == "duplicate")

    def _process(self, rec, recovering: bool = False) -> EventOutcome:
        done = "recovered" if recovering else "processed"
        out = EventOutcome(done, rec.event_id, rec.kind, ingest_seq=rec.ingest_seq)
        prior = self.ledger.status(rec.event_id)
        writer = self.writers[rec.session_id]
        self._current_session = rec.session_id
        self.ledger.begin(rec.event_id, rec.session_id, rec.kind, rec.ingest_seq)
        if prior != "episode_done":
            try:
                if rec.kind == "audio":
                    out.note_id = writer.write_note(rec)["id"]
                else:
                    out.visit_ids = writer.observe_frame(rec)
            except Exception as e:                      # FalkorDB down, bad graph state, ...
                return self._fail(out, "episode", e)
            self.ledger.mark(rec.event_id, "episode_done", "episode")
        try:
            decisions = self.agent.handle(adapters.agent_observation(rec.kind, rec.event))
        except Exception as e:
            return self._fail(out, "agent", e)
        out.decisions = [d.to_dict() for d in decisions]
        if any(d.status == "failed" and "memory unavailable" in d.reason for d in decisions):
            # the agent refused to guess without memory: keep the event open for recover()
            return self._fail(out, "agent", RuntimeError("agent memory unavailable"))
        self.ledger.mark(rec.event_id, "done", None, None, [d.decision_id for d in decisions])
        return out

    def _fail(self, out: EventOutcome, stage: str, err: Exception) -> EventOutcome:
        out.status, out.stage, out.error = "failed", stage, _err(err)
        self.ledger.mark(out.event_id, "failed", stage, out.error)
        logger.warning("event %s failed at %s: %s", out.event_id, stage, type(err).__name__)
        return out

    def status(self) -> dict:
        return {"user_id": self.config.user_id, "graph": self.config.graph_name, "storage": self.config.storage,
                "sessions": list(self.config.session_ids),
                "events": {s: self.engine.store.count(s) for s in self.config.session_ids},
                "ledger": self.ledger.counts(), "open_visit_frames": sum(w.open_frames for w in self.writers.values()),
                "graph_labels": schema.label_counts(self.graph)}


def _err(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())[:500]
    return f"{type(e).__name__}: {e}"[:500]
