"""Cheap, per-frame context tracking. Runs on every frame, touches no database,
calls no model. Its job is to turn ~1 frame/s of noisy detections into a handful
of meaningful triggers per hour ("arrived at a supermarket", "waiting at a bus stop").
"""

from __future__ import annotations

import math
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from memory.rules import classify_place

from .models import VisionObservation


@dataclass
class TrackerConfig:
    min_confidence: float = 0.5      # ignore detections below this
    window_frames: int = 8           # sliding window used for "stable" classes
    window_ms: int = 60_000
    stable_ratio: float = 0.6        # class must be in >=60% of window frames
    settle_frames: int = 5           # place type must persist this many frames before it counts
    leave_frames: int = 20           # this many 'unknown' frames reset the current place
    move_m: float = 60               # GPS movement that warrants a new place lookup
    stale_ms: int = 120_000          # frames older than this (vs newest seen) are ignored
    tick_ms: int = 60_000            # time-based checks at most once per minute
    bus_wait_ms: int = 45_000        # at a bus stop this long before transit info is considered
    seen_cache: int = 10_000


@dataclass
class Trigger:
    kind: str                        # place_entered | location_changed | bus_stop_wait | boarded | tick
    ts_ms: int
    data: dict = field(default_factory=dict)


def haversine_m(a_lat, a_lon, b_lat, b_lon) -> float:
    r = 6_371_000
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


class LRUSet:
    def __init__(self, size: int):
        self.size, self.d = size, OrderedDict()

    def seen(self, key) -> bool:
        """True if already present; otherwise remembers it."""
        if key in self.d:
            self.d.move_to_end(key)
            return True
        self.d[key] = None
        if len(self.d) > self.size:
            self.d.popitem(last=False)
        return False


class ContextTracker:
    def __init__(self, cfg: TrackerConfig | None = None):
        self.cfg = cfg or TrackerConfig()
        self.frames = LRUSet(self.cfg.seen_cache)
        self.window: deque[tuple[int, set[str]]] = deque()
        self.clock_ms = 0
        self.place_type = "unknown"          # settled place type
        self._cand, self._cand_n, self._unknown_n = "unknown", 0, 0
        self.lat = self.lon = None
        self._lookup_at: tuple[float, float] | None = None
        self._bus_since: int | None = None
        self._bus_fired = False
        self.boarded_ms: int | None = None
        self._last_tick = 0
        self.stats = {"frames": 0, "duplicate": 0, "stale": 0, "triggers": 0}

    def stable_classes(self) -> dict[str, float]:
        n = len(self.window)
        if not n:
            return {}
        counts: dict[str, int] = {}
        for _, cs in self.window:
            for c in cs:
                counts[c] = counts.get(c, 0) + 1
        return {c: k / n for c, k in counts.items() if k / n >= self.cfg.stable_ratio}

    def observe(self, f: VisionObservation) -> list[Trigger]:
        cfg = self.cfg
        self.stats["frames"] += 1
        if self.frames.seen(f.frame_id):
            self.stats["duplicate"] += 1
            return []
        if f.ts_ms < self.clock_ms - cfg.stale_ms:
            self.stats["stale"] += 1             # arrived too late to describe "now"
            return []
        self.clock_ms = max(self.clock_ms, f.ts_ms)
        out: list[Trigger] = []

        classes = {c for c, conf in f.classes.items() if conf >= cfg.min_confidence}
        self.window.append((f.ts_ms, classes))
        while len(self.window) > cfg.window_frames or (self.window and self.window[0][0] < self.clock_ms - cfg.window_ms):
            self.window.popleft()
        stable = self.stable_classes()

        # place type with hysteresis: noise in one frame never changes where we are
        if len(self.window) >= min(4, cfg.window_frames):
            pt = classify_place(stable)
            if pt == self._cand:
                self._cand_n += 1
            else:
                self._cand, self._cand_n = pt, 1
            if pt == "unknown":
                self._unknown_n += 1
                if self._unknown_n >= cfg.leave_frames:
                    self.place_type = "unknown"
            else:
                self._unknown_n = 0
                if self._cand_n >= cfg.settle_frames and pt != self.place_type:
                    self.place_type = pt
                    out.append(Trigger("place_entered", self.clock_ms, {"place_type": pt, "classes": sorted(stable)}))

        if f.has_gps:
            self.lat, self.lon = f.lat, f.lon
            if self._lookup_at is None or haversine_m(*self._lookup_at, f.lat, f.lon) > cfg.move_m:
                self._lookup_at = (f.lat, f.lon)
                out.append(Trigger("location_changed", self.clock_ms, {"lat": f.lat, "lon": f.lon}))

        # bus stop: waiting (not just walking past) vs already on the bus
        on_bus = any(c in stable for c in ("bus interior", "bus window"))
        if on_bus:
            if self.boarded_ms is None:
                self.boarded_ms = self.clock_ms
                out.append(Trigger("boarded", self.clock_ms))
            self._bus_since, self._bus_fired = None, False
        elif "bus stop" in stable:
            self.boarded_ms = None
            self._bus_since = self._bus_since or self.clock_ms
            if not self._bus_fired and self.clock_ms - self._bus_since >= cfg.bus_wait_ms:
                self._bus_fired = True
                out.append(Trigger("bus_stop_wait", self.clock_ms,
                                   {"lat": self.lat, "lon": self.lon, "waited_s": (self.clock_ms - self._bus_since) // 1000}))
        else:
            self._bus_since, self._bus_fired = None, False
            if self.boarded_ms and self.place_type != "transit":
                self.boarded_ms = None

        if self.clock_ms - self._last_tick >= cfg.tick_ms:
            self._last_tick = self.clock_ms
            out.append(Trigger("tick", self.clock_ms))
        self.stats["triggers"] += sum(t.kind != "tick" for t in out)
        return out

    def advance(self, ts_ms: int) -> list[Trigger]:
        """Audio and other inputs move the clock too, so time-based checks still run."""
        if ts_ms < self.clock_ms - self.cfg.stale_ms:
            return []
        self.clock_ms = max(self.clock_ms, ts_ms)
        if self.clock_ms - self._last_tick >= self.cfg.tick_ms:
            self._last_tick = self.clock_ms
            return [Trigger("tick", self.clock_ms)]
        return []

    def snapshot(self) -> dict:
        return {"place_type": self.place_type, "classes": sorted(self.stable_classes()),
                "lat": self.lat, "lon": self.lon, "on_bus": self.boarded_ms is not None}
