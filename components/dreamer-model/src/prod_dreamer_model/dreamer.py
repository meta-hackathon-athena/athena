"""The Muse dream loop.

Muse (Meta Responses API) is the reasoning engine. Each ``dream()`` call runs a
sequence of focus rounds — temporal routines, places and movement, activities
and notes, then consolidation — and in every round the model freely queries the
FalkorDB graph through the read-only ``run_cypher`` tool before writing beliefs
with ``write_pattern`` / ``write_memory``. Graph state accumulates across
rounds, so later rounds refine rather than repeat.

If the model is unavailable, or makes no tool calls at all, the deterministic
fallback still runs (``heuristic_dream`` on the legacy graph). On the shared Vixy
graph ``unified_dream`` (routines, facts, structured semantic claims, optional
model-assisted extraction through ``extractor``) runs on every dream: it is the
authoritative, idempotent consolidation, and the Muse rounds may only add to it. Whoever wrote the beliefs, ``finalize_evidence``
then runs: memories inherit their patterns' recorded evidence, and anything
still without evidence is marked "unconfirmed" rather than presented as known.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import requests

from .config import DreamerConfig
from .consolidate import unified_dream
from .heuristic import heuristic_dream
from .tools import TOOL_SPECS, GraphToolBox

ROUND_FOCUS: list[tuple[str, str]] = [
    (
        "Temporal routines",
        "Work out WHEN this person does things. Use run_cypher to look at "
        "local_hour, local_min and dow for the classes that recur, and compare "
        "days with each other. Store one :Pattern per genuine routine you can "
        "support with evidence. Do not store things seen on only one day as a "
        "routine.",
    ),
    (
        "Places and movement",
        "Work out WHERE this person goes and HOW they move between places. "
        "Cluster GPS frames (lat/lon) into places; look at which classes appear "
        "at each; identify commute legs by bus/train and their times. Store "
        ":Pattern nodes of kind commute / location, with time_window and days.",
    ),
    (
        "Activities, objects and what was said",
        "Work out WHAT this person does and cares about. Look at co-occurring "
        "classes, and read every :Note. A stated preference outranks an inferred "
        "one. Store :Pattern nodes of kind activity / preference / social, and "
        "note any one-off or sensitive event as kind anomaly instead of a habit.",
    ),
    (
        "Consolidate and remember",
        "Re-read all :Pattern nodes and the evidence behind them. Merge "
        "duplicates, correct or retract weak ones, and promote stable, useful "
        "conclusions into :LongTermMemory with from_patterns links. Then produce "
        "the final list of durable things Vixy should remember about this user.",
    ),
]

# The same four rounds over the shared graph, where episodes replace frames.
ROUND_FOCUS_UNIFIED: list[tuple[str, str]] = [
    (
        "Temporal routines",
        "Work out WHEN this person does things. Compare :Visit start_min / dow / "
        "place_type across days. Store one :Pattern per routine that at least 3 "
        "distinct days support, citing those Visit ids.",
    ),
    (
        "Places and movement",
        "Work out WHERE this person goes: :Visit-[:AT]->:Place, transit visits, and "
        "what usually comes next (:NEXT). Store :Pattern nodes of kind location / "
        "commute with their supporting Visit ids.",
    ),
    (
        "People, facts and what was said",
        "Read every :Note. Store durable facts about named people (pass people=[name]) "
        "and the user's stated preferences, citing the Note ids. A statement by an "
        "unknown speaker is not the user's own. Commitments and reminders belong to "
        "the agent: do not restate them as memories.",
    ),
    (
        "Consolidate and remember",
        "Re-read :Pattern and :LongTermMemory nodes with their evidence. Merge "
        "duplicates (reuse ids), retract weak ones, and promote stable patterns into "
        ":LongTermMemory with from_patterns links.",
    ),
]

INSTRUCTIONS = """\
You are the Vixy dreamer: a memory-consolidation agent that studies one \
wearable user's day and turns raw observations into durable, evidence-linked \
beliefs about them.

You are reasoning over a FalkorDB graph. You may call `run_cypher` as many \
times as you like; it is READ-ONLY. You store conclusions only through \
`write_pattern` and `write_memory`, which always link back to the frames, \
notes or days that support them.

Method:
1. Query first, but be economical: 3-6 well-chosen queries are usually enough \
for a round. After that, WRITE. Do not spend the whole round exploring.
2. Never conclude from the briefing alone, and never write a routine that only \
one day supports: require 2-3 distinct days, and say which days. A single \
one-off (including medical or religious visits) is at most an `anomaly`, \
never a habit. Never store sensitive health or worship details as routine.
3. Prefer few, true, well-supported beliefs over many weak ones.
4. Statements are about the user, in the third person or imperative, concrete \
and testable: "Takes the bus to university around 08:12 on weekdays."
5. Update existing patterns by reusing their id instead of duplicating.
6. Store each conclusion the moment you reach it; do not batch everything to \
the end of the round.
7. Every write must carry evidence: pass the {evidence_ids} values \
you actually saw in the graph. A belief you cannot support is not worth storing.
8. ALWAYS end the round by calling `finish`. A round is not complete until you \
do, and you will be told to stop exploring if you take too long.
"""


class MuseClient(Protocol):
    def create(self, input_items: list[dict], previous_response_id: str | None = None) -> dict:
        ...


class ResponsesMuseClient:
    """Thin transport for the Meta Responses API tool loop."""

    def __init__(self, config: DreamerConfig) -> None:
        if not config.model_api_key:
            raise ValueError("MODEL_API_KEY is not set")
        config.check_model_policy()
        self.config = config

    def create(self, input_items: list[dict], previous_response_id: str | None = None) -> dict:
        payload: dict[str, Any] = {
            "model": self.config.muse_model,
            "input": input_items,
            "tools": TOOL_SPECS,
            "tool_choice": "auto",
            # previous_response_id chaining only works when the response is stored.
            "store": self.config.store_responses,
            "max_output_tokens": self.config.max_output_tokens,
            "reasoning": {"effort": self.config.reasoning_effort},
        }
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        response = requests.post(
            self.config.muse_url,
            headers={
                "Authorization": f"Bearer {self.config.model_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.config.request_timeout_s,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Muse API {response.status_code}: {response.text[:300]}"
            )
        return response.json()


@dataclass
class DreamRunResult:
    rounds_run: int = 0
    turns: int = 0
    used_fallback: bool = False
    errors: list[str] = field(default_factory=list)
    round_summaries: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    fallback: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rounds_run": self.rounds_run,
            "turns": self.turns,
            "used_fallback": self.used_fallback,
            "errors": self.errors,
            "round_summaries": self.round_summaries,
            "usage": self.usage,
            "evidence": self.evidence,
            "fallback": self.fallback,
        }


def parse_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _brief(outcome: dict[str, Any]) -> str:
    """Compact one-line tool result for the run log."""
    if not outcome.get("ok"):
        return f"error: {str(outcome.get('error'))[:120]}"
    if "statement" in outcome:
        return f"stored {outcome.get('pattern_id') or outcome.get('memory_id')}: " \
               f"{str(outcome['statement'])[:90]}"
    if "summary" in outcome:
        return f"finish: {str(outcome['summary'])[:110]}"
    rows = outcome.get("rows")
    if rows is not None:
        return f"{outcome.get('row_count', 0)} row(s){' (truncated)' if outcome.get('truncated') else ''}"
    return json.dumps(outcome, default=str)[:120]


class Dreamer:
    """Runs focus rounds against a toolbox, with a deterministic fallback."""

    def __init__(
        self,
        toolbox: GraphToolBox,
        config: DreamerConfig,
        client: MuseClient | None = None,
        log: Callable[[str], None] = print,
        history: Any = None,
        extractor: Any = None,
    ) -> None:
        self.toolbox = toolbox
        self.config = config
        self.client = client
        self.log = log
        # unified layout: lookup of original context-engine events by event_id
        # (bridge.EventHistory), used to verify notes before consolidating them
        self.history = history
        # unified layout: optional model_extract.ModelExtractor (budgeted, validated, cached)
        self.extractor = extractor

    @property
    def round_focus(self) -> list[tuple[str, str]]:
        return ROUND_FOCUS_UNIFIED if self.config.unified else ROUND_FOCUS

    # -- public ------------------------------------------------------------

    def dream(self, rounds: int | None = None, allow_fallback: bool = True) -> DreamRunResult:
        result = DreamRunResult()
        planned = list(range(min(rounds or self.config.max_rounds, len(self.round_focus))))

        if self.client is None:
            result.errors.append("no model client configured")
        else:
            for index in planned:
                title, focus = self.round_focus[index]
                self.log(f"  round {index + 1}/{len(planned)}: {title}")
                try:
                    summary, turns = self._run_round(index, title, focus)
                    result.rounds_run += 1
                    result.turns += turns
                    result.round_summaries.append({"round": index + 1, "title": title, **summary})
                    self.log(f"    round done in {turns} turn(s): "
                             f"{str(summary.get('summary', ''))[:140]}")
                except Exception as exc:  # network / API error
                    message = f"round {index + 1} failed: {type(exc).__name__}: {exc}"
                    self.log(f"    ! {message}")
                    result.errors.append(message)
                    break

        before = (self.toolbox.usage.patterns_written, self.toolbox.usage.memories_written)
        if allow_fallback and (before == (0, 0) or self.config.unified):
            if before == (0, 0):
                self.log("  no beliefs stored by the model — running the deterministic consolidation")
            if self.config.unified:
                written = unified_dream(self.toolbox, self.history, self.extractor)
            else:
                written = heuristic_dream(self.toolbox)
            result.used_fallback = before == (0, 0)
            result.fallback = written
            # The fallback writes through the graph directly, not through the
            # tool dispatcher; count its writes so evidence finalization runs
            # (previously skipped, leaving fallback memories without evidence).
            self.toolbox.usage.patterns_written += int(written.get("patterns", 0))
            self.toolbox.usage.memories_written += int(written.get("memories", 0))
            self.log(f"  fallback wrote {written['patterns']} patterns / "
                     f"{written['memories']} memories")

        after = (self.toolbox.usage.patterns_written, self.toolbox.usage.memories_written)
        if after != (0, 0):
            result.evidence = self.toolbox.finalize_evidence()
            if result.evidence.get("propagated") or result.evidence.get("inferred"):
                self.log(f"  evidence: {result.evidence}")

        result.usage = self.toolbox.usage.as_dict()
        return result

    # -- internals ---------------------------------------------------------

    def _run_round(self, index: int, title: str, focus: str) -> tuple[dict[str, Any], int]:
        brief = self.toolbox.briefing(focus=f"{title} — {focus}")
        evidence_ids = ("Visit.id / Note.id / Note.note_id" if self.config.unified
                        else "frame_id / note_id / day_id")
        user_text = (
            f"{INSTRUCTIONS.format(evidence_ids=evidence_ids)}\n\n{self.toolbox.schema_brief}\n\n"
            f"THIS ROUND — {title}\n{focus}\n\n"
            f"GRAPH BRIEFING\n{brief}\n\n"
            "Begin by querying the graph."
        )
        input_items: list[dict] = [
            {"role": "user", "content": [{"type": "input_text", "text": user_text}]}
        ]
        previous_id: str | None = None
        nudges = 0
        turns = 0
        writes_before = (
            self.toolbox.usage.patterns_written + self.toolbox.usage.memories_written
        )

        for _turn in range(self.config.max_turns_per_round):
            turns += 1
            data = self.client.create(input_items, previous_id)  # type: ignore[union-attr]
            previous_id = data.get("id")
            calls = [o for o in data.get("output", []) if o.get("type") == "function_call"]

            if not calls:
                if nudges == 0:
                    nudges += 1
                    input_items = [{
                        "role": "user",
                        "content": [{
                            "type": "input_text",
                            "text": "Continue. Call run_cypher to investigate, or "
                                    "write_pattern / write_memory, or finish when done.",
                        }],
                    }]
                    continue
                break

            outputs: list[dict] = []
            finished = False
            summary: dict[str, Any] = {}
            for call in calls:
                name = call.get("name", "")
                args = parse_tool_args(call.get("arguments"))
                outcome, is_finish = self.toolbox.dispatch(name, args)
                self.log(f"      · {name} -> {_brief(outcome)}")
                if is_finish:
                    summary = outcome
                    finished = True
                outputs.append({
                    "type": "function_call_output",
                    "call_id": call.get("call_id"),
                    "output": json.dumps(outcome, default=str),
                })

            if finished:
                return summary, turns

            input_items = outputs
            nudge = self._budget_nudge(_turn, writes_before)
            if nudge:
                self.log(f"      · nudge -> {nudge[:70]}")
                input_items = outputs + [{
                    "role": "user",
                    "content": [{"type": "input_text", "text": nudge}],
                }]

        writes_done = (
            self.toolbox.usage.patterns_written + self.toolbox.usage.memories_written
            - writes_before
        )
        return {
            "summary": f"(turn budget reached after storing {writes_done} belief(s); "
                       "no finish call)"
        }, turns

    def _budget_nudge(self, turn: int, writes_before: int) -> str | None:
        """Keep the model from burning the whole round on exploration."""
        writes_now = (
            self.toolbox.usage.patterns_written + self.toolbox.usage.memories_written
        )
        remaining = self.config.max_turns_per_round - turn - 1
        if writes_now == writes_before and turn >= 3:
            return (
                "You have explored enough. Stop querying: store what you have "
                "found now with write_pattern, then continue to the next finding "
                "or call finish."
            )
        if remaining <= 1:
            return (
                "Final turn. Store anything still missing, then call finish now "
                "with a summary."
            )
        if remaining <= 3 and writes_now > writes_before:
            return "Wrap up: store any remaining memory with write_memory, then call finish."
        return None
