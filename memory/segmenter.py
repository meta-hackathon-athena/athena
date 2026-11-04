"""Group a stream of frames into visits (episodes).

Thirty frames of the same food court become ONE visit, not thirty.
A visit ends when:
  - there is a time gap (camera off / not worn), or
  - GPS moves away from the visit's centroid, or
  - the scene stops matching for several consecutive frames (hysteresis,
    so a single odd frame doesn't split a visit).
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass, field

from .frames import Frame
from .rules import classify_activity, classify_place


@dataclass
class SegmenterConfig:
    gap_s: float = 300            # no frame for 5 min -> close the visit
    move_m: float = 120           # GPS this far from centroid -> new visit
    max_gps_accuracy_m: float = 60  # ignore worse fixes for movement decisions
    min_similarity: float = 0.2   # Jaccard(frame, visit profile) below this = mismatch
    mismatch_frames: int = 3      # consecutive mismatches needed to split
    min_frames: int = 3           # shorter blips are dropped
    min_duration_s: float = 30
    tag_min_ratio: float = 0.2    # class must be in >=20% of frames ...
    tag_min_frames: int = 2       # ... and in >=2 frames to count as a visit tag
    min_confidence: float = 0.5   # per-frame class confidence threshold


@dataclass
class Visit:
    id: str
    start_ms: int
    end_ms: int
    frames: int
    tags: dict[str, float]        # class -> fraction of frames
    tag_conf: dict[str, float]    # class -> mean confidence
    place_type: str
    activity: str
    lat: float | None = None
    lon: float | None = None
    first_frame_id: str = ""
    tag_max_count: dict[str, int] = field(default_factory=dict)  # class -> most instances in one frame
    last_frame_id: str = ""
    frame_ids: list[str] = field(default_factory=list)          # every frame in the visit, in order

    @property
    def kind(self) -> str:
        return "transit" if self.place_type == "transit" else "stay"


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class _Open:
    frames: list[Frame] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    pending: list[Frame] = field(default_factory=list)  # mismatching frames awaiting decision

    def add(self, f: Frame, min_conf: float) -> None:
        self.frames.append(f)
        self.counts.update(c for c, p in f.classes.items() if p >= min_conf)

    def profile(self) -> set[str]:
        n = len(self.frames)
        if n < 3:
            return set(self.counts)
        return {c for c, k in self.counts.items() if k / n >= 0.3}

    def centroid(self, max_acc: float) -> tuple[float, float] | None:
        pts = [(f.lat, f.lon) for f in self.frames
               if f.has_gps and (f.accuracy_m is None or f.accuracy_m <= max_acc)]
        if not pts:
            return None
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


class Segmenter:
    def __init__(self, user_id: str, config: SegmenterConfig | None = None):
        self.user_id = user_id
        self.cfg = config or SegmenterConfig()
        self._open: _Open | None = None

    def _classes(self, f: Frame) -> set[str]:
        return {c for c, p in f.classes.items() if p >= self.cfg.min_confidence}

    def add(self, f: Frame) -> list[Visit]:
        """Feed one frame (in time order). Returns visits that just closed."""
        cfg, closed = self.cfg, []
        cur = self._open
        if cur is None:
            self._open = _Open()
            self._open.add(f, cfg.min_confidence)
            return closed

        last = (cur.pending or cur.frames)[-1]
        moved = False
        # on a bus/train the position keeps changing; split transit on scene change only
        in_transit = classify_place({c: 1.0 for c in cur.profile()}) == "transit"
        if not in_transit and f.has_gps and (f.accuracy_m is None or f.accuracy_m <= cfg.max_gps_accuracy_m):
            c = cur.centroid(cfg.max_gps_accuracy_m)
            moved = c is not None and haversine_m(c[0], c[1], f.lat, f.lon) > cfg.move_m

        if (f.ts_ms - last.ts_ms) / 1000 > cfg.gap_s or moved:
            closed += self._close()
            self._open = _Open()
            self._open.add(f, cfg.min_confidence)
            return closed

        prof, cls = cur.profile(), self._classes(f)
        union = prof | cls
        sim = len(prof & cls) / len(union) if union else 1.0
        if sim >= cfg.min_similarity:
            for p in cur.pending:      # the odd frames were noise; keep them
                cur.add(p, cfg.min_confidence)
            cur.pending.clear()
            cur.add(f, cfg.min_confidence)
            return closed

        cur.pending.append(f)
        if len(cur.pending) >= cfg.mismatch_frames:
            pending = cur.pending
            cur.pending = []
            closed += self._close()
            self._open = _Open()
            for p in pending:
                self._open.add(p, cfg.min_confidence)
        return closed

    def open_frames(self) -> list[Frame]:
        """Frames of the visit still being built (not yet returned by add/flush)."""
        if self._open is None:
            return []
        return self._open.frames + self._open.pending

    def flush(self) -> list[Visit]:
        if self._open is None:
            return []
        cur = self._open
        for p in cur.pending:
            cur.add(p, self.cfg.min_confidence)
        cur.pending = []
        return self._close()

    def _close(self) -> list[Visit]:
        cur, self._open = self._open, None
        cfg = self.cfg
        if cur is None or not cur.frames:
            return []
        n = len(cur.frames)
        start, end = cur.frames[0].ts_ms, cur.frames[-1].ts_ms
        if n < cfg.min_frames or (end - start) / 1000 < cfg.min_duration_s:
            return []
        tags = {c: k / n for c, k in cur.counts.items()
                if k >= cfg.tag_min_frames and k / n >= cfg.tag_min_ratio}
        conf = {}
        for c in tags:
            vals = [f.classes[c] for f in cur.frames if c in f.classes]
            conf[c] = sum(vals) / len(vals)
        max_count = {c: max((f.counts.get(c, 0) for f in cur.frames), default=0) for c in tags}
        max_count = {c: k for c, k in max_count.items() if k > 0}
        place_type = classify_place(tags)
        centroid = cur.centroid(cfg.max_gps_accuracy_m)
        vid = hashlib.sha1(f"{self.user_id}:{cur.frames[0].frame_id}:{start}".encode()).hexdigest()[:16]
        return [Visit(
            id=f"vis_{vid}", start_ms=start, end_ms=end, frames=n, tags=tags, tag_conf=conf,
            tag_max_count=max_count,
            place_type=place_type, activity=classify_activity(tags, place_type),
            lat=centroid[0] if centroid else None, lon=centroid[1] if centroid else None,
            first_frame_id=cur.frames[0].frame_id,
            last_frame_id=cur.frames[-1].frame_id, frame_ids=[f.frame_id for f in cur.frames],
        )]


def segment(frames: list[Frame], user_id: str, config: SegmenterConfig | None = None) -> list[Visit]:
    seg = Segmenter(user_id, config)
    out: list[Visit] = []
    for f in sorted(frames, key=lambda x: x.ts_ms):
        out += seg.add(f)
    return out + seg.flush()
