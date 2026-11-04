"""Consolidation job: the Dreamer on the runtime's graph and event history.

  1. memory.store.consolidate()      Samu's deterministic episodic -> Fact rules
                                     (frequent place, usual activity); evidence = Visits
  2. prod_dreamer_model.Dreamer      Ara's dream loop in the unified layout. It always runs
                                     consolidate.unified_dream (routines -> Pattern/LTM, Fact ->
                                     LTM, structured semantic claims -> LTM + Person, with
                                     conflicts / supersession and conservative entity resolution),
                                     verifying every Note against the original context-engine event.
                                     Optional: ``extractor`` (budgeted Meta-model claim extraction,
                                     validated and cached) and ``online`` (the Muse tool loop).

Same FalkorDB graph (vixy_<user_id>) and the same context-engine store as the runtime: the
Dreamer creates no event store, no Frame nodes and never resets. Re-running is idempotent.
"""

from __future__ import annotations

from typing import Any, Callable

from memory.store import MemoryStore
from prod_dreamer_model.bridge import EventHistory
from prod_dreamer_model.config import DreamerConfig, is_contributor_model
from prod_dreamer_model.dreamer import Dreamer, ResponsesMuseClient
from prod_dreamer_model.graph import ProdGraph
from prod_dreamer_model.tools import GraphToolBox

from .config import VixyConfig


def dreamer_config(config: VixyConfig, **overrides: Any) -> DreamerConfig:
    """The Dreamer's view of the same deployment (same graph, user, session, store, tz)."""
    base = DreamerConfig.from_env()
    return base.with_overrides(graph_name=config.graph_name, graph_layout="unified", user_id=config.user_id,
                               session_id=config.default_session, tz=config.tz,
                               falkordb_url=config.falkordb_url, embedded_path=config.db_path,
                               events_db=config.events_db, **overrides)


def run_dream(config: VixyConfig, graph, engine=None, *, online: bool = False, now_ms: int | None = None,
              log: Callable[[str], None] = lambda *_: None, extractor=None) -> dict:
    dcfg = dreamer_config(config)
    if extractor and is_contributor_model(extractor.model.model_id):
        raise ValueError("contributor extraction is forbidden for the integrated wearable runtime")
    # 1. deterministic Facts from episodes; "now" = the newest episode, so replayed
    #    (simulated-time) days consolidate the same way whenever the job runs
    store = MemoryStore(graph)
    if now_ms is None:
        rows = store.q("MATCH (v:Visit) RETURN max(v.end_ms)")
        now_ms = rows[0][0] if rows and rows[0][0] is not None else None
    facts = store.consolidate(now_ms) if now_ms is not None else {"active_inferred_facts": 0, "retired": 0}

    # 2. the Dreamer on the same graph, reading the same event history
    history = EventHistory.from_engine(engine) if engine is not None else EventHistory.from_sqlite(config.events_db)
    pgraph = ProdGraph(graph, dcfg)
    pgraph.bootstrap()
    client = None
    if online:
        if not dcfg.has_model:
            raise RuntimeError("online dreaming needs MODEL_API_KEY")
        client = ResponsesMuseClient(dcfg)      # refuses contributor models unless opted in
    try:
        result = Dreamer(GraphToolBox(pgraph, dcfg), dcfg, client=client, log=log, history=history,
                         extractor=extractor).dream()
    finally:
        history.close()
    out = {"graph": dcfg.graph_name, "history": history.source, "facts": facts, **result.as_dict()}
    if extractor is not None:
        out["model_usage"] = extractor.model.usage.as_dict()
    return out
