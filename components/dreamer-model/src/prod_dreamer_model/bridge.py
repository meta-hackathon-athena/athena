"""context-engine bridge.

This is the *only* door between perception-shaped data and the dreamer:

    VisionFrame / note dict
        -> context-engine v1 events (validated, deduped, durable in SQLite)
        -> get_context / export_events
        -> vixy.dreamer_event.v1 records
        -> DayBundle for the graph loader

The dreamer never reads raw frames or transcripts straight off disk; it sees
what context-engine committed. Re-running an ingest is a no-op because
``event_id`` is stable and context-engine deduplicates on it.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, time as Time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .config import REPO_ROOT
from .frames import VisionFrame

if TYPE_CHECKING:  # annotations only; context-engine is imported lazily at runtime
    from context_engine import ContextEngine
    from context_engine.schemas import DreamerEventRecord


def _import_context_engine():
    """Import context-engine, or explain exactly how to provide it.

    Order: an installed ``vixy-context-engine`` package, then ``$CONTEXT_ENGINE_SRC``,
    then a sibling checkout (monorepo dev). This package is independently
    installable, so context-engine is not assumed to sit next to it.
    """
    try:  # installed (recommended)
        import context_engine  # type: ignore

        return context_engine
    except ImportError:
        pass

    candidates = []
    env_src = os.getenv("CONTEXT_ENGINE_SRC")
    if env_src:
        candidates.append(Path(env_src).expanduser())
    candidates += [
        REPO_ROOT / "context-engine" / "src",           # sibling in a monorepo
        REPO_ROOT.parent / "context-engine" / "src",    # nested one level down
    ]
    for candidate in candidates:
        if candidate.is_dir():
            sys.path.insert(0, str(candidate))
            import context_engine  # type: ignore

            return context_engine

    raise ImportError(
        "context-engine is required to ingest perception data but was not found.\n"
        "Install it, or point at a checkout:\n"
        "  uv pip install vixy-context-engine        # if published\n"
        "  uv pip install -e ../context-engine      # from a sibling checkout\n"
        "  export CONTEXT_ENGINE_SRC=/path/to/context-engine/src\n"
        "Commands that only read the graph (dream, report) do not need it."
    )


def _load_context_engine():
    """Resolve the two context-engine names this module needs, lazily.

    Deferred so that ``dream`` and ``report`` keep working without
    context-engine installed — only ingestion requires it.
    """
    _import_context_engine()
    from context_engine import ContextEngine as _ContextEngine
    from context_engine.schemas import DreamerEventRecord as _DreamerEventRecord

    return _ContextEngine, _DreamerEventRecord


# ---------------------------------------------------------------------------
# Graph-ready node shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassNode:
    name: str
    count: int
    confidence: float


@dataclass(frozen=True)
class FrameNode:
    frame_id: str
    ts_ms: int
    timestamp: str
    description: str
    classes: tuple[ClassNode, ...] = ()
    lat: float | None = None
    lon: float | None = None
    accuracy_m: float | None = None


@dataclass(frozen=True)
class NoteNode:
    note_id: str
    ts_ms: int
    timestamp: str
    text: str
    kind: str = "note"  # note | reminder | preference


@dataclass
class DayBundle:
    day_id: str
    date: str
    session_id: str
    frames: list[FrameNode] = field(default_factory=list)
    notes: list[NoteNode] = field(default_factory=list)

    @property
    def first_ms(self) -> int:
        stamps = [f.ts_ms for f in self.frames] + [n.ts_ms for n in self.notes]
        return min(stamps) if stamps else 0

    @property
    def last_ms(self) -> int:
        stamps = [f.ts_ms for f in self.frames] + [n.ts_ms for n in self.notes]
        return max(stamps) if stamps else 0


@dataclass
class IngestStats:
    accepted: int = 0
    duplicates: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)

    def add(self, status: str, kind: str) -> None:
        if status == "accepted":
            self.accepted += 1
        else:
            self.duplicates += 1
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1


def classify_note(text: str) -> str:
    """A deterministic hint only; the dreamer still reads the full text."""
    low = text.lower()
    if low.startswith("remind me") or "remind me" in low:
        return "reminder"
    if any(w in low for w in ("i like", "i love", "i hate", "i really like", "my favourite")):
        return "preference"
    return "note"


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class EventBridge:
    """Ingests perception-shaped data and exports the dreamer feed."""

    def __init__(
        self,
        db_path: str = "data/prod_dreamer_events.sqlite3",
        session_id: str = "wearer-01",
        tz_name: str = "Asia/Singapore",
        engine: ContextEngine | None = None,
    ) -> None:
        self.tz = ZoneInfo(tz_name)
        self.session_id = session_id
        self.db_path = db_path
        if engine is not None:
            self.engine = engine
        else:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            context_engine_cls, _ = _load_context_engine()
            self.engine = context_engine_cls(db_path)

    def close(self) -> None:
        self.engine.close()

    def __enter__(self) -> "EventBridge":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- ingest ------------------------------------------------------------

    def ingest_frames(self, frames: list[VisionFrame]) -> IngestStats:
        stats = IngestStats()
        for frame in frames:
            result = self.engine.ingest_video(_observation(frame, self.session_id))
            stats.add(result.status, "video")
        return stats

    def ingest_notes(self, notes: list[dict]) -> IngestStats:
        stats = IngestStats()
        for note in notes:
            result = self.engine.ingest_audio({**note, "session_id": self.session_id})
            stats.add(result.status, "audio")
        return stats

    def ingest_mock_day(self, mock_day) -> IngestStats:
        stats = self.ingest_frames(mock_day.frames)
        other = self.ingest_notes(mock_day.notes)
        stats.accepted += other.accepted
        stats.duplicates += other.duplicates
        for k, v in other.by_kind.items():
            stats.by_kind[k] = stats.by_kind.get(k, 0) + v
        return stats

    # -- export ------------------------------------------------------------

    def day_bounds_utc(self, day: Date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, Time(0, 0), tzinfo=self.tz).astimezone(timezone.utc)
        return start, start + timedelta(days=1)

    def export_day(self, day: Date) -> list[DreamerEventRecord]:
        start, end = self.day_bounds_utc(day)
        return self.engine.export_events(self.session_id, start_time=start, end_time=end)

    def export_range(self, start: Date, end: Date) -> list[DreamerEventRecord]:
        start_dt = datetime.combine(start, Time(0, 0), tzinfo=self.tz).astimezone(timezone.utc)
        end_dt = datetime.combine(end, Time(0, 0), tzinfo=self.tz).astimezone(timezone.utc)
        return self.engine.export_events(self.session_id, start_time=start_dt, end_time=end_dt)

    def export_jsonl(self, destination: str | Path, start: Date, end: Date) -> int:
        return self.engine.export_jsonl(
            self.session_id,
            destination,
            start_time=datetime.combine(start, Time(0, 0), tzinfo=self.tz).astimezone(timezone.utc),
            end_time=datetime.combine(end, Time(0, 0), tzinfo=self.tz).astimezone(timezone.utc),
        )

    # -- bundles -----------------------------------------------------------

    def bundles_for_range(self, start: Date, end: Date) -> list[DayBundle]:
        return bundles_from_records(self.export_range(start, end), self.tz, self.session_id)

    def bundle_for_day(self, day: Date) -> DayBundle | None:
        bundles = self.bundles_for_range(day, day + timedelta(days=1))
        return bundles[0] if bundles else None


# ---------------------------------------------------------------------------
# Reading context-engine history without re-ingesting it
# ---------------------------------------------------------------------------


def load_export_jsonl(path: str | Path) -> list[DreamerEventRecord]:
    """Read a context-engine ``vixy.dreamer_event.v1`` JSONL export (validated)."""
    _, record_cls = _load_context_engine()
    records = []
    with open(path, encoding="utf-8") as fp:
        for number, line in enumerate(fp, 1):
            if not line.strip():
                continue
            try:
                records.append(record_cls.model_validate(json.loads(line)))
            except Exception as exc:  # pydantic.ValidationError / JSONDecodeError
                raise ValueError(f"{path}:{number}: not a vixy.dreamer_event.v1 record: {exc}") from exc
    return records


def bundles_from_records(
    records: list[DreamerEventRecord], tz: ZoneInfo, session_id: str | None = None
) -> list[DayBundle]:
    """Group export records into per-local-day bundles (legacy graph loader input)."""
    by_day: dict[str, DayBundle] = {}
    for record in records:
        if session_id is not None and record.session_id != session_id:
            continue
        local_day = record.event_time.astimezone(tz).date().isoformat()
        bundle = by_day.setdefault(
            local_day,
            DayBundle(day_id=local_day, date=local_day, session_id=record.session_id),
        )
        if record.kind == "video":
            bundle.frames.append(_frame_node(record))
        else:
            bundle.notes.append(_note_node(record))
    for bundle in by_day.values():
        bundle.frames.sort(key=lambda f: f.ts_ms)
        bundle.notes.sort(key=lambda n: n.ts_ms)
    return [by_day[d] for d in sorted(by_day)]


class EventHistory:
    """Read-only lookup of original observations by ``event_id``.

    The unified dreamer uses it to check that a graph Note really is the
    context-engine event it cites before consolidating it, and to read the
    original text. Backed by the shared context-engine (in-process engine or its
    SQLite file — never a private copy) or by a JSONL export.
    """

    def __init__(self, lookup, source: str, closer=None) -> None:
        self._lookup = lookup
        self.source = source
        self._closer = closer

    @classmethod
    def from_engine(cls, engine: ContextEngine) -> "EventHistory":
        return cls(engine.get_event_record, "context-engine (shared engine)")

    @classmethod
    def from_sqlite(cls, db_path: str | Path) -> "EventHistory":
        if not Path(db_path).exists():
            raise FileNotFoundError(f"context-engine event store not found: {db_path}")
        engine_cls, _ = _load_context_engine()
        engine = engine_cls(db_path)
        return cls(engine.get_event_record, f"context-engine sqlite {db_path}", engine.close)

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "EventHistory":
        by_id = {r.event_id: r for r in load_export_jsonl(path)}
        return cls(by_id.get, f"context-engine export {path}")

    def get(self, event_id: str):
        return self._lookup(event_id) if event_id else None

    def close(self) -> None:
        if self._closer:
            self._closer()


# ---------------------------------------------------------------------------
# Record -> node mapping
# ---------------------------------------------------------------------------


def _observation(frame: VisionFrame, session_id: str) -> dict:
    from .frames import to_video_observation

    return to_video_observation(frame, session_id)


def _frame_node(record: DreamerEventRecord) -> FrameNode:
    event = record.event
    source = event.get("source") or {}
    counts = source.get("counts") or {}
    classes = tuple(
        ClassNode(
            name=str(det["label"]),
            count=int(counts.get(det["label"], 1)),
            confidence=float(det.get("confidence", 0.0)),
        )
        for det in event.get("detections") or []
    )
    location = event.get("location") or {}
    frame_id = str(source.get("frame_id") or event.get("event_id") or record.event_id)
    return FrameNode(
        frame_id=frame_id,
        ts_ms=int(record.event_time.timestamp() * 1000),
        timestamp=record.event_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        description=str(event.get("description") or ""),
        classes=classes,
        lat=location.get("latitude"),
        lon=location.get("longitude"),
        accuracy_m=location.get("accuracy_m"),
    )


def _note_node(record: DreamerEventRecord) -> NoteNode:
    event = record.event
    text = str(event.get("text") or "")
    return NoteNode(
        note_id=record.event_id,
        ts_ms=int(record.event_time.timestamp() * 1000),
        timestamp=record.event_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        text=text,
        kind=classify_note(text),
    )
