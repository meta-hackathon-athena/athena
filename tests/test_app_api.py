"""The app's clarification flow over /v1: list the agent's questions, answer them, and read
the plans and reminders the answer changed. Runs the real two-day M2 demo plus one spoken
plan change, through the same runtime as `make ui-demo`."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs, urlparse

from memory.store import TZ
from vixy.api import VixyAPI
from vixy.runtime import VixyRuntime

from .test_m2 import cfg, say, two_days  # noqa: F401  (cfg is a pytest fixture)

CORRECTION = "Actually I will meet Marta on Thursday at 11am."


def _call(api: VixyAPI, method: str, path: str, body=None):
    url = urlparse(path)
    return api.handle(method, url.path, {k: v[-1] for k, v in parse_qs(url.query).items()}, body)


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _seeded(rt) -> VixyAPI:
    two_days(rt)
    out = say(rt, CORRECTION, "2026-11-03T19:20:00+08:00", "ui1")
    assert [d["action"] for d in out.decisions if d["action"] != "NO_ACTION"] == ["ASK_USER"]
    return VixyAPI(rt)


def test_questions_carry_their_context_and_answers_go_through_the_agent(cfg):
    with VixyRuntime(cfg) as rt:
        api = _seeded(rt)
        code, body = _call(api, "GET", "/v1/questions")
        assert code == 200
        qs = {q["kind"]: q for q in body["questions"]}
        fix, milk = qs["confirm_correction"], qs["complete_reminder"]
        assert fix["text"] == "Did Meet Marta change from 9 AM to 11 AM on Thu 05 Nov?"
        assert fix["heard"]["text"] == CORRECTION and fix["heard"]["speaker_role"] == "wearer"
        assert fix["about"][0]["label"] == "Commitment" and fix["about"][0]["status"] == "tentative"
        assert fix["proposal"]["op"] == "update_commitment_time"
        assert fix["proposal"]["start_ms"] == _ms("2026-11-05T11:00:00+08:00")
        assert milk["heard"]["text"] == "I bought the milk" and milk["proposal"]["op"] == "complete_reminder"
        assert _call(api, "GET", f"/v1/questions/{fix['id']}")[1]["question"]["id"] == fix["id"]

        # "no": nothing changes, the question is closed
        code, out = _call(api, "POST", f"/v1/questions/{milk['id']}/answer", body={"answer": "no"})
        assert code == 200 and [d["action"] for d in out["decisions"]] == ["NO_ACTION"]
        assert out["question"]["status"] == "answered" and out["question"]["answer"] == "no"
        assert [r["status"] for r in _call(api, "GET", "/v1/reminders")[1]["reminders"]] == ["open"]

        # "yes": the agent applies its own allow-listed write; the old time is kept
        code, out = _call(api, "POST", f"/v1/questions/{fix['id']}/answer", body={"answer": "yes"})
        assert code == 200 and out["decisions"][0]["action"] == "MEMORY_UPDATE"
        assert out["decisions"][0]["status"] == "done"
        [plan] = _call(api, "GET", "/v1/commitments")[1]["commitments"]
        assert plan["title"] == "Meet Marta" and plan["status"] == "confirmed"
        assert plan["start_ms"] == _ms("2026-11-05T11:00:00+08:00")
        assert plan["prev_start_ms"] == _ms("2026-11-05T09:00:00+08:00")
        assert _call(api, "GET", "/v1/questions")[1]["questions"] == []
        answered = _call(api, "GET", "/v1/questions?status=answered")[1]["questions"]
        assert {q["id"] for q in answered} == {fix["id"], milk["id"]}

        # answering twice, bad bodies, unknown ids and bad filters are refused
        assert _call(api, "POST", f"/v1/questions/{fix['id']}/answer", body={"answer": "yes"})[0] == 409
        assert _call(api, "POST", f"/v1/questions/{fix['id']}/answer", body={"answer": "maybe"})[0] == 400
        assert _call(api, "POST", "/v1/questions/qst_nope/answer", body={"answer": "yes"})[0] == 404
        assert _call(api, "GET", "/v1/questions?status=weird")[0] == 400
        assert _call(api, "GET", "/v1/reminders?status=weird")[0] == 400


def test_a_yes_completes_the_reminder(cfg):
    with VixyRuntime(cfg) as rt:
        api = _seeded(rt)
        open_qs = _call(api, "GET", "/v1/questions")[1]["questions"]
        milk = next(q for q in open_qs if q["kind"] == "complete_reminder")
        fix = next(q for q in open_qs if q["kind"] == "confirm_correction")
        # past its expiry (the plan's old start time) the question can't be answered
        code, err = _call(api, "POST", f"/v1/questions/{fix['id']}/answer",
                          body={"answer": "yes", "now": "2026-11-06T09:00:00+08:00"})
        assert code == 409 and err["error"]["code"] == "not_open"
        assert _call(api, "GET", "/v1/commitments")[1]["commitments"][0]["status"] == "tentative"
        code, out = _call(api, "POST", f"/v1/questions/{milk['id']}/answer",
                               body={"answer": "yes", "now": "2026-11-03T19:30:00+08:00"})
        assert code == 200 and out["decisions"][0]["action"] == "MEMORY_UPDATE"
        [done] = _call(api, "GET", "/v1/reminders?status=done")[1]["reminders"]
        assert done["status"] == "done" and done["done_ms"] == _ms("2026-11-03T19:30:00+08:00")
        assert _call(api, "GET", "/v1/reminders")[1]["reminders"] == []
        assert datetime.fromtimestamp(done["done_ms"] / 1000, TZ).hour == 19
