"""Report rendering from already-collected data (no graph needed)."""

from __future__ import annotations

import json
from pathlib import Path

from prod_dreamer_model.report import render_markdown, render_text

DATA = {
    "generated_at": "2026-11-04T00:00:00Z",
    "graph": "vixy_dreamer",
    "session_id": "wearer-01",
    "coverage": {
        "labels": {"Frame": 10}, "relationships": {}, "frames": 10, "days": 2,
        "notes": 1, "classes": 4, "patterns": 1, "memories": 1,
        "first_ms": 1_793_000_000_000, "last_ms": 1_793_086_400_000, "gps_frames": 10,
    },
    "coverage_first": "Mon 26 Oct 2026 08:00",
    "coverage_last": "Tue 27 Oct 2026 08:00",
    "patterns": [
        {
            "pattern_id": "morning-bus-commute",
            "statement": "Takes the bus to campus around 08:12 on weekdays.",
            "kind": "commute", "confidence": 0.71, "status": "active",
            "time_window": "08:00-08:30", "days": [0, 1, 2, 3, 4],
            "about": ["bus interior"], "evidence": [{"label": "Day", "day_id": "2026-10-26"}],
        }
    ],
    "long_term_memories": [
        {
            "memory_id": "ltm-morning-bus-commute",
            "statement": "Takes the bus to campus around 08:12 on weekdays.",
            "kind": "routine", "confidence": 0.71, "status": "active",
            "about": [], "from_patterns": ["morning-bus-commute"], "evidence": [],
        }
    ],
    "notes": [{"kind": "preference", "text": "I really like the noodles."}],
    "remember": ["Takes the bus to campus around 08:12 on weekdays."],
}


def test_render_text_contains_the_conclusions():
    text = render_text(DATA)
    assert "VIXY DREAMER" in text
    assert "Takes the bus to campus around 08:12 on weekdays." in text
    assert "ltm-morning-bus-commute" in text
    assert "Mon, Tue, Wed, Thu, Fri" in text
    assert "PLAIN ANSWER" in text


def test_render_markdown_is_valid_and_complete():
    md = render_markdown(DATA)
    assert md.startswith("# Vixy dreamer report")
    assert "**Takes the bus to campus around 08:12 on weekdays.**" in md
    assert "## Patterns" in md
    assert "## Spoken notes" in md


def test_empty_graph_still_renders():
    empty = {**DATA, "patterns": [], "long_term_memories": [], "remember": [], "notes": []}
    text = render_text(empty)
    assert "none yet" in text
    assert "_none yet_" in render_markdown(empty)


def test_json_round_trips():
    payload = json.dumps(DATA)
    assert json.loads(payload)["graph"] == "vixy_dreamer"
    assert Path("/tmp").is_dir()
