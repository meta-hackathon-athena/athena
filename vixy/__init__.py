"""Vixy integrated runtime (Milestone 1).

Context Engine (canonical observed events) -> Episode Writer (Note / Visit) -> Active Agent,
over one FalkorDB graph per user (vixy_<user_id>, schema vixy.graph.v2) that the Dreamer
consolidates into Pattern / LongTermMemory with real evidence. See docs/ARCHITECTURE.md.
"""

from .config import VixyConfig, connect_graph
from .contracts import GRAPH_SCHEMA_VERSION, SOURCE_SCHEMA
from .memory_api import SharedMemory
from .runtime import EventOutcome, VixyRuntime

__all__ = ["EventOutcome", "GRAPH_SCHEMA_VERSION", "SOURCE_SCHEMA", "SharedMemory", "VixyConfig",
           "VixyRuntime", "connect_graph"]
