"""Regression: offline-fallback memories used to end up with zero evidence.

The fallback wrote through the graph directly, so the tool-usage counters
stayed at zero and evidence propagation never ran (audit §5, probe D5).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from prod_dreamer_model.bridge import EventBridge
from prod_dreamer_model.config import DreamerConfig
from prod_dreamer_model.dreamer import Dreamer
from prod_dreamer_model.graph import open_graph
from prod_dreamer_model.mock_data import synthetic_days
from prod_dreamer_model.tools import GraphToolBox

pytest.importorskip("redislite.falkordb_client")
pytest.importorskip("context_engine")


def test_fallback_memories_get_recorded_evidence_or_are_unconfirmed(tmp_path):
    config = DreamerConfig(graph_name="evidence_test", embedded_path=str(tmp_path / "g.rdb"),
                           session_id="tester")
    days = synthetic_days("2026-10-19", days=5, seed=7)
    with EventBridge(str(tmp_path / "e.sqlite3"), "tester", config.tz) as bridge:
        for d in days:
            bridge.ingest_mock_day(d)
        bundles = bridge.bundles_for_range(days[0].date, days[-1].date + timedelta(days=1))
    graph = open_graph(config)
    graph.bootstrap()
    graph.load_bundles(bundles)

    result = Dreamer(GraphToolBox(graph, config), config, client=None, log=lambda *_: None).dream()

    assert result.used_fallback and result.evidence["propagated"] > 0
    assert result.evidence["inferred"] == 0                     # window backfill is opt-in
    memories = graph.fetch_memories()
    assert memories
    for m in memories:
        assert m["evidence_count"] > 0 or m["status"] == "unconfirmed", m
    graph.g.delete()
