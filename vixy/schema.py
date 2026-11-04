"""The unified per-user graph ``vixy_<user_id>``, schema ``vixy.graph.v2``.

Samu's episodic/operational layer (memory/store.py, agent/memory_tools.py) and Ara's
belief layer (prod_dreamer_model/graph.py) in ONE graph. Full reference with every
property: docs/GRAPH_SCHEMA.md. This module owns versioning and the index set;
each component keeps writing its own labels through its own code.
"""

from __future__ import annotations

import time

from agent.memory_tools import AGENT_INDEXES
from memory.store import INDEXES as EPISODIC_INDEXES
from prod_dreamer_model.graph import UNIFIED_INDEXES as BELIEF_INDEXES

from .contracts import GRAPH_SCHEMA_VERSION, MIGRATABLE_FROM

# Integration-specific lookups (provenance and per-day reads)
INTEGRATION_INDEXES = [
    ("Note", "source_event_id"), ("Note", "note_id"), ("Note", "local_date"),
    ("Day", "day_id"), ("VixyMeta", "key"),
    # v2: decision inspection by trigger
    ("AgentLog", "trigger_event_id"), ("AgentLog", "ts_ms"),
]
INDEXES = list(dict.fromkeys(EPISODIC_INDEXES + AGENT_INDEXES + BELIEF_INDEXES + INTEGRATION_INDEXES))

# Who may create/modify what. Enforced by construction (each writer only has code
# for its own labels) and checked by tests/test_integration.py.
WRITE_OWNERS = {
    "episode_writer": {"Day", "Visit", "Note", "Place", "Tag", "Fact(kind=preference, source=user)"},
    "active_agent": {"Commitment", "Question", "AgentLog", "Note.status (reminder completion)"},
    "dreamer": {"Pattern", "LongTermMemory", "Person", "DreamRun", "Extraction", "Fact(source=inferred)"},
    "runtime": {"VixyMeta"},
}
LAYERS = {
    "observed": {"Note", "Visit", "Day"},
    "entity": {"Place", "Tag", "Person"},
    "operational": {"Commitment", "Question", "AgentLog"},
    "inferred": {"Fact", "Pattern", "LongTermMemory"},
    "meta": {"VixyMeta", "DreamRun", "Extraction"},
}


class SchemaMismatch(RuntimeError):
    pass


def bootstrap(graph, user_id: str) -> dict:
    """Create indexes and the version marker. Idempotent; refuses a different schema version."""
    for label, prop in INDEXES:
        try:
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception as e:
            if "already indexed" not in str(e).lower():
                raise
    rows = graph.query("MATCH (m:VixyMeta {key: 'graph'}) RETURN m.schema_version, m.user_id").result_set
    if rows:
        version, owner = rows[0]
        if version != GRAPH_SCHEMA_VERSION:
            hint = " (run `python -m vixy migrate`)" if version in MIGRATABLE_FROM else ""
            raise SchemaMismatch(f"graph has schema {version!r}, runtime expects {GRAPH_SCHEMA_VERSION!r}; "
                                 f"migrate explicitly (no silent migration){hint}")
        if owner != user_id:
            raise SchemaMismatch(f"graph belongs to user {owner!r}, not {user_id!r}")
        return {"schema_version": version, "created": False}
    graph.query("""
        MERGE (m:VixyMeta {key: 'graph'})
        ON CREATE SET m.schema_version = $v, m.user_id = $u, m.created_ms = $now
    """, {"v": GRAPH_SCHEMA_VERSION, "u": user_id, "now": int(time.time() * 1000)})
    return {"schema_version": GRAPH_SCHEMA_VERSION, "created": True}


def migrate(graph, user_id: str) -> dict:
    """Explicit, additive upgrade of a ``vixy.graph.v1`` graph to v2. Idempotent.

    v2 only adds: indexes, the ``superseded`` belief status, structured-claim properties on
    LongTermMemory, Person resolution properties and edges, ``Extraction`` cache nodes and
    decision-explanation properties on AgentLog. Every one of them is optional, so existing
    nodes stay valid as they are: nothing is rewritten or deleted. Milestone 1 memories are
    re-consolidated (and, where a structured claim now covers them, retired with a
    SUPERSEDES edge) the next time the Dreamer runs, not here.
    """
    rows = graph.query("MATCH (m:VixyMeta {key: 'graph'}) RETURN m.schema_version, m.user_id").result_set
    if not rows:
        return {"migrated": False, **bootstrap(graph, user_id)}
    version, owner = rows[0]
    if owner != user_id:
        raise SchemaMismatch(f"graph belongs to user {owner!r}, not {user_id!r}")
    if version == GRAPH_SCHEMA_VERSION:
        return {"migrated": False, "schema_version": version}
    if version not in MIGRATABLE_FROM:
        raise SchemaMismatch(f"no migration from {version!r} to {GRAPH_SCHEMA_VERSION!r}")
    for label, prop in INDEXES:
        try:
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception as e:
            if "already indexed" not in str(e).lower():
                raise
    graph.query("""
        MATCH (m:VixyMeta {key: 'graph'})
        SET m.schema_version = $v, m.migrated_from = $old, m.migrated_ms = $now
    """, {"v": GRAPH_SCHEMA_VERSION, "old": version, "now": int(time.time() * 1000)})
    return {"migrated": True, "from": version, "schema_version": GRAPH_SCHEMA_VERSION}


def label_counts(graph) -> dict[str, int]:
    rows = graph.query("MATCH (n) RETURN labels(n)[0], count(n)").result_set
    return {label: n for label, n in rows}
