"""Parse vision-model output (and optional GPS) into Frame objects."""

from __future__ import annotations

import json
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable


@dataclass
class Frame:
    frame_id: str
    ts_ms: int
    # class name -> confidence (1.0 when the vision output has no scores)
    classes: dict[str, float]
    lat: float | None = None
    lon: float | None = None
    accuracy_m: float | None = None
    # class name -> number of instances in the frame (when the vision output has "count")
    counts: dict[str, int] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @property
    def has_gps(self) -> bool:
        return self.lat is not None and self.lon is not None


def parse_ts(value: str | int | float) -> int:
    """ISO-8601 string with an explicit offset, or epoch (s or ms) -> epoch milliseconds.

    A naive ISO string ("2026-10-26T09:00:00") is rejected, as in context-engine:
    reading it as machine-local time silently shifted events by the host's offset.
    """
    if isinstance(value, (int, float)):
        return int(value if value > 1e12 else value * 1000)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive timestamp (no UTC offset) is not accepted: {value!r}")
    return int(dt.timestamp() * 1000)


def _parse_classes(raw: dict) -> tuple[dict[str, float], dict[str, int]]:
    """Returns (class -> confidence, class -> instance count). Accepts any of:
    - "classes": [{"class": "person", "count": 2, "confidence": .94}]   (current vision output)
    - "classes": ["person", "cup"]                                    (older output, no scores)
    - "classes": ["person"], "confidences": {"person": .9}
    - "classes": [{"label": "person", "confidence": .9}]
    """
    classes = raw.get("classes") or []
    scores = raw.get("confidences") or raw.get("scores") or {}
    conf_out: dict[str, float] = {}
    count_out: dict[str, int] = {}
    for c in classes:
        count = None
        if isinstance(c, dict):
            name = c.get("class") or c.get("label") or c.get("name")
            conf = c.get("confidence", c.get("score", 1.0))
            count = c.get("count")
        else:
            name, conf = c, scores.get(c, 1.0)
        if not name:
            continue
        name = str(name).strip().lower()
        conf_out[name] = max(float(conf), conf_out.get(name, 0.0))
        if count is not None:
            count_out[name] = count_out.get(name, 0) + int(count)
    return conf_out, count_out


def parse_frame(raw: dict) -> Frame:
    loc = raw.get("location") or raw.get("gps") or {}
    lat = raw.get("lat", loc.get("lat", loc.get("latitude")))
    lon = raw.get("lon", loc.get("lon", loc.get("longitude")))
    acc = raw.get("accuracy_m", loc.get("accuracy_m"))
    known = {"frame_id", "timestamp", "classes", "confidences", "scores", "count",
             "location", "gps", "lat", "lon", "accuracy_m", "image"}
    classes, counts = _parse_classes(raw)
    return Frame(
        frame_id=str(raw.get("frame_id") or raw["timestamp"]),
        ts_ms=parse_ts(raw["timestamp"]),
        classes=classes,
        counts=counts,
        lat=float(lat) if lat is not None else None,
        lon=float(lon) if lon is not None else None,
        accuracy_m=float(acc) if acc is not None else None,
        # the image path is deliberately dropped: memory keeps classes, not pictures
        extra={k: v for k, v in raw.items() if k not in known},
    )


def load_json_records(path: str | Path) -> list[dict]:
    """Reads a JSON array, a single JSON object, or JSON Lines."""
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text[0] == "[":
        return json.loads(text)
    if text[0] == "{" and "\n" not in text:
        return [json.loads(text)]
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def attach_gps(frames: list[Frame], gps_points: Iterable[dict], max_gap_s: float = 30) -> None:
    """Fill in lat/lon on frames lacking them, from the nearest GPS fix in time."""
    pts = sorted(
        (parse_ts(p["timestamp"]), float(p.get("lat", p.get("latitude"))),
         float(p.get("lon", p.get("longitude"))), p.get("accuracy_m"))
        for p in gps_points
    )
    if not pts:
        return
    times = [p[0] for p in pts]
    for f in frames:
        if f.has_gps:
            continue
        i = bisect_left(times, f.ts_ms)
        best = min((pts[j] for j in (i - 1, i) if 0 <= j < len(pts)),
                   key=lambda p: abs(p[0] - f.ts_ms))
        if abs(best[0] - f.ts_ms) <= max_gap_s * 1000:
            f.lat, f.lon = best[1], best[2]
            f.accuracy_m = float(best[3]) if best[3] is not None else None
