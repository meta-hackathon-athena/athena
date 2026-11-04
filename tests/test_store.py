from datetime import timedelta

from tests.test_pipeline import T0, frames
from memory.segmenter import segment

DAY = timedelta(days=1)


def ingest(store, fs, user="alice"):
    vs = segment(fs, user)
    for v in vs:
        store.add_visit(v)
    return vs


def food_court(start, lat=1.3467, lon=103.68, prefix="k"):
    return frames(["food court", "plate", "noodle", "person"], 20, start=start, lat=lat, lon=lon, prefix=prefix)


def test_reingest_is_idempotent(make_store):
    s = make_store()
    fs = food_court(T0)
    ingest(s, fs)
    ingest(s, fs)
    assert s.stats() == {"visits": 1, "places": 1, "facts": 0, "notes": 0}


def test_nearby_same_type_reuses_place_but_neighbour_cafe_is_separate(make_store):
    s = make_store()
    ingest(s, food_court(T0))
    ingest(s, food_court(T0 + DAY, lat=1.3468, prefix="k2"))          # ~11 m away
    ingest(s, frames(["cafe", "coffee", "cup"], 20, start=T0 + 2 * DAY, lat=1.3469, prefix="c"))
    assert s.stats()["places"] == 2


def test_facts_need_three_distinct_days_and_explain(make_store):
    s = make_store()
    for d in range(2):
        ingest(s, food_court(T0 + d * DAY, prefix=f"k{d}"))
    now = int((T0 + 3 * DAY).timestamp() * 1000)
    assert s.consolidate(now)["active_inferred_facts"] == 0
    ingest(s, food_court(T0 + 2 * DAY, prefix="k2"))
    ingest(s, food_court(T0 + 2 * DAY + timedelta(hours=2), prefix="k3"))  # same day: not new evidence
    s.consolidate(now)
    freq = [f for f in s.facts() if f["kind"] == "frequent_place"]
    assert len(freq) == 1 and freq[0]["support_days"] == 3
    exp = s.explain(freq[0]["id"])
    assert len(exp["evidence_days"]) == 3 and "3 separate days" in exp["why"]


def test_sensitive_places_never_become_facts(make_store):
    s = make_store()
    for d in range(4):
        ingest(s, frames(["clinic", "chair"], 20, start=T0 + d * DAY, lat=1.351, lon=103.692, prefix=f"c{d}"))
    s.consolidate(int((T0 + 5 * DAY).timestamp() * 1000))
    assert s.stats()["visits"] == 4 and s.facts() == []


def test_forget_visit_deletes_and_retires_fact(make_store):
    s = make_store()
    vs = []
    for d in range(3):
        vs += ingest(s, food_court(T0 + d * DAY, prefix=f"k{d}"))
    now = int((T0 + 3 * DAY).timestamp() * 1000)
    s.consolidate(now)
    fid = s.facts()[0]["id"]
    s.forget_visit(vs[0].id)
    assert s.q("MATCH (v:Visit {id: $id}) RETURN count(v)", {"id": vs[0].id})[0][0] == 0
    s.consolidate(now)
    assert s.facts() == []
    assert [f["status"] for f in s.facts(include_inactive=True) if f["id"] == fid] == ["inactive"]


def test_notes_reminders_and_preferences(make_store):
    s = make_store()
    ingest(s, frames(["supermarket", "shopping cart", "shelf"], 20, lat=1.35, lon=103.69))
    ts = int(T0.timestamp() * 1000) + 60_000
    rem = s.add_note("Remind me to buy milk at the supermarket", ts)
    assert rem["kind"] == "reminder" and rem["trigger_place_type"] == "supermarket"
    ctx = s.context(1.35, 103.69)
    assert [r["text"] for r in ctx["reminders_here"]] == ["Remind me to buy milk at the supermarket"]
    assert s.q("MATCH (:Note)-[:DURING]->(v:Visit) RETURN count(v)")[0][0] == 1
    s.complete_reminder(rem["id"])
    assert s.open_reminders() == []

    s.add_note("I love spicy noodles", ts)
    pref = [f for f in s.facts() if f["kind"] == "preference"]
    assert pref and pref[0]["source"] == "user"
    assert s.explain(pref[0]["id"])["why"] == 'You told me: "I love spicy noodles"'


def test_users_are_isolated(make_store):
    a, b = make_store("alice"), make_store("bob")
    ingest(a, food_court(T0))
    assert a.stats()["visits"] == 1 and b.stats()["visits"] == 0


def test_viz_export(make_store, tmp_path):
    from memory.viz import write_html
    s = make_store()
    ingest(s, food_court(T0))
    s.add_note("remind me </script> to buy milk", int(T0.timestamp() * 1000))
    out = write_html(s, tmp_path / "g.html", "alice")
    html = out.read_text()
    assert "vis-network" in html and "<\\/script>" in html and html.count("</script>") == 2
