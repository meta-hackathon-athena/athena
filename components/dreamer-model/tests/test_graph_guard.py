"""The read-only Cypher guard and graph helpers that need no server."""

from __future__ import annotations

import pytest

from prod_dreamer_model.graph import _clamp, rows, safe_cypher, scrub
from prod_dreamer_model.tools import slug


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (f:Frame) RETURN f.frame_id LIMIT 5",
        "MATCH (f:Frame)-[:SAW]->(c:Class) RETURN c.name, count(*) ORDER BY count(*) DESC",
        "OPTIONAL MATCH (n:Note) RETURN n LIMIT 1",
        "CALL db.labels()",
        "WITH 1 AS x RETURN x",
    ],
)
def test_read_queries_allowed(query):
    ok, reason = safe_cypher(query)
    assert ok, reason


@pytest.mark.parametrize(
    "query",
    [
        "CREATE (n:Evil)",
        "MERGE (n:Evil {x: 1})",
        "MATCH (n) SET n.x = 1",
        "MATCH (n) DELETE n",
        "MATCH (n) DETACH DELETE n",
        "MATCH (n) REMOVE n.x",
        "DROP INDEX ON :Frame(frame_id)",
        "CALL db.idx.fulltext.createNodeIndex('Frame', 'description')",
        "MATCH (n) RETURN n; MATCH (m) DELETE m",
        "",
    ],
)
def test_write_queries_rejected(query):
    ok, _ = safe_cypher(query)
    assert not ok


def test_literals_do_not_trigger_the_guard():
    ok, reason = safe_cypher(
        "MATCH (n:Note) WHERE n.text CONTAINS 'delete me' RETURN n.text"
    )
    assert ok, reason


def test_scrub_removes_comments_and_strings():
    assert "secret" not in scrub("MATCH (n) // secret\nRETURN n")
    assert "''" in scrub("MATCH (n) WHERE n.text = 'hello' RETURN n")


def test_slug_is_a_stable_id():
    assert slug("Takes the bus to campus!", "fallback") == "takes-the-bus-to-campus"
    assert slug("", "fallback") == "fallback"


def test_clamp_bounds():
    assert _clamp(1.7) == 1.0
    assert _clamp(-2) == 0.0
    assert _clamp("nope") == 0.5


class _Result:
    def __init__(self, header, result_set):
        self.header = header
        self.result_set = result_set


def test_rows_handles_falkordb_header_pairs():
    res = _Result([[2, "frame_id"], [1, "n"]], [["f1", 3]])
    assert rows(res) == [{"frame_id": "f1", "n": 3}]
