"""prod_dreamer_model — the production Vixy dreamer.

Pipeline (all local except the Muse reasoning call):

    mock wearable frames / audio notes
        -> context-engine  (typed v1 events, SQLite, alignment, JSONL export)
        -> vixy.dreamer_event.v1 export
        -> FalkorDB graph   (:Day / :Frame / :Class / :Note — observed)
        -> Muse multi-step dream loop (read-only Cypher tools + memory writers)
        -> :Pattern / :LongTermMemory      (what the user is like, with evidence)

See README.md for the graph contract and the CLI.
"""

from __future__ import annotations

from .config import DreamerConfig
from .dreamer import Dreamer, DreamRunResult
from .graph import ProdGraph, connect
from .report import render_text
from .tools import GraphToolBox

__all__ = [
    "DreamRunResult",
    "Dreamer",
    "DreamerConfig",
    "GraphToolBox",
    "ProdGraph",
    "connect",
    "render_text",
]
