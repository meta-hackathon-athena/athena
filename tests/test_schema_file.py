from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[1] / "memory" / "schema.cypher"


def statements():
    body = "\n".join(l for l in SCHEMA.read_text().splitlines() if not l.strip().startswith("//"))
    return [s.strip() for s in body.split(";") if s.strip()]


def test_schema_file_runs_and_matches_store(make_store):
    from agent.memory_tools import AGENT_INDEXES
    from memory.store import INDEXES
    stmts = statements()
    s = make_store("schema_check")      # setup() already created the indexes
    results = []
    for q in stmts:
        if q.startswith("CREATE INDEX"):
            continue
        results.append(s.q(q))
    # the indexes in the file are exactly the ones the code creates
    file_idx = {q.split("(n:")[1].split(")")[0] + "." + q.split("(n.")[1].split(")")[0]
                for q in stmts if q.startswith("CREATE INDEX")}
    assert file_idx == {f"{l}.{p}" for l, p in INDEXES + AGENT_INDEXES}
    assert s.stats() == {"visits": 3, "places": 1, "facts": 2, "notes": 2}
    # the example queries return what the example graph implies
    day, here, laptop, facts, why, nxt, rem = results[-7:]
    assert len(day) == 1 and here[0][1] == 3 and laptop == [] and len(facts) == 2
    assert [r[1] for r in why] == ["2026-10-19", "2026-10-20", "2026-10-21"]
    assert rem[0][0] == "remind me to buy milk at the supermarket"
    # store.py reads the example graph correctly too
    assert "3 separate days" in s.explain("fact_freq_plc_koufu")["why"]
