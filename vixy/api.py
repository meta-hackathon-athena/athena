"""Versioned JSON API for the future Vixy app and the simulator (``/v1``, stdlib only).

    GET  /v1/health                         liveness + graph/schema
    GET  /v1/status                         runtime status, agent/recall/model usage
    POST /v1/ask        {question, conversation_id?, now?}          -> vixy.ask.v1
    GET  /v1/memories?about=&status=&limit=                          consolidated memories
    GET  /v1/memories/{id}                  memory + derivation + relations + evidence,
                                            each evidence record re-checked against context-engine
    GET  /v1/people/{id}                    a Person, its possible duplicates and memories
    GET  /v1/timeline?date=YYYY-MM-DD | ?from=ISO&to=ISO             visits + notes
    GET  /v1/decisions?action=&since=&limit=&delivered=1              agent decisions (AgentLog)
    GET  /v1/decisions/{id}                 one decision with its supporting memories + evidence
    POST /v1/events/audio | /v1/events/video  a CE v1 event, or {"asr": {...}} / {"perception": {...}}
    POST /v1/dream                          run the Dreamer (offline unless the runtime has a model)
    GET  /v1/questions?status=open|answered|all&limit=   the agent's clarification questions
    GET  /v1/questions/{id}                 one question: what it is about, what "yes" would change
    POST /v1/questions/{id}/answer  {answer: "yes"|"no", now?}       answer it in the app
                                            (ActiveAgent.answer: "yes" applies the allow-listed write)
    GET  /v1/commitments                    the user's plans (tentative / confirmed, with prior times)
    GET  /v1/reminders?status=open|done|all  reminders and whether they are done

Every response is JSON with a ``schema`` key; errors are ``vixy.error.v1``. Reads use the
read-only memory interfaces; the only writes are event ingestion (through the normal
runtime path), answers to the agent's own questions (through ActiveAgent.answer, which
applies only its allow-listed operations) and the Dreamer job. No authentication: local development only
(binding is 127.0.0.1 by default).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from memory.store import TZ

from .contracts import API_VERSION, GRAPH_SCHEMA_VERSION

API_SCHEMA = "vixy.api.v1"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _ms(value: str, name: str) -> int:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise ApiError(400, "bad_request", f"{name} must be an ISO-8601 time") from e
    if dt.tzinfo is None:
        raise ApiError(400, "bad_request", f"{name} needs an explicit UTC offset")
    return int(dt.timestamp() * 1000)


def _int(q: dict, name: str, default: int, lo: int = 1, hi: int = 500) -> int:
    try:
        return max(lo, min(hi, int(q.get(name, default))))
    except (TypeError, ValueError) as e:
        raise ApiError(400, "bad_request", f"{name} must be an integer") from e


class VixyAPI:
    """Transport-independent request handling (the HTTP server and tests both use it)."""

    def __init__(self, runtime):
        self.rt = runtime
        self.lock = threading.Lock()           # the runtime (and embedded FalkorDB) is single-threaded

    def handle(self, method: str, path: str, query: dict | None = None, body: Any = None) -> tuple[int, dict]:
        query = query or {}
        parts = [unquote(p) for p in path.strip("/").split("/") if p]
        try:
            if len(parts) < 1 or parts[0] != API_VERSION:
                raise ApiError(404, "not_found", f"unknown path {path}; this API is /{API_VERSION}/...")
            with self.lock:
                return 200, {"schema": API_SCHEMA, **self._route(method.upper(), parts[1:], query, body)}
        except ApiError as e:
            return e.status, {"schema": "vixy.error.v1", "error": {"code": e.code, "message": e.message}}
        except Exception as e:                  # never leak internals (or transcript text) in errors
            return 500, {"schema": "vixy.error.v1", "error": {"code": "internal", "message": type(e).__name__}}

    def _route(self, method: str, parts: list[str], q: dict, body: Any) -> dict:
        head = parts[0] if parts else ""
        if method == "GET" and head == "health":
            return {"ok": True, "graph": self.rt.config.graph_name, "graph_schema": GRAPH_SCHEMA_VERSION}
        if method == "GET" and head == "status":
            st = self.rt.status()
            st["agent"] = self.rt.agent.summary()
            st["ask_model_usage"] = self.rt.asker.model.usage.as_dict() if self.rt.asker.model else None
            return {"status": st}
        if method == "POST" and head == "ask":
            if not isinstance(body, dict) or not isinstance(body.get("question"), str):
                raise ApiError(400, "bad_request", "body must be {\"question\": \"...\"}")
            now = _ms(body["now"], "now") if body.get("now") else None
            conv = body.get("conversation_id")
            if conv is not None and (not isinstance(conv, str) or len(conv) > 64):
                raise ApiError(400, "bad_request", "conversation_id must be a short string")
            return {"result": self.rt.ask(body["question"], conversation_id=conv, now_ms=now)}
        if method == "GET" and head == "memories":
            if len(parts) == 2:
                return {"memory": self._memory_detail(parts[1])}
            status = tuple(s for s in str(q.get("status", "active")).split(",") if s)
            return {"memories": self.rt.memory.long_term_memories(about=q.get("about"), status=status,
                                                                  limit=_int(q, "limit", 50)),
                    "patterns": self.rt.memory.patterns(status=status) if not q.get("about") else []}
        if method == "GET" and head == "people" and len(parts) == 2:
            p = self.rt.memory.person(parts[1])
            if not p:
                raise ApiError(404, "not_found", f"no person {parts[1]}")
            return {"person": p}
        if method == "GET" and head == "timeline":
            if q.get("date"):
                try:
                    day = datetime.fromisoformat(q["date"]).date()
                except ValueError as e:
                    raise ApiError(400, "bad_request", "date must be YYYY-MM-DD") from e
                start = datetime.combine(day, datetime.min.time(), TZ)
                lo, hi = int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)
            elif q.get("from") and q.get("to"):
                lo, hi = _ms(q["from"], "from"), _ms(q["to"], "to")
                if hi <= lo or hi - lo > 31 * 86_400_000:
                    raise ApiError(400, "bad_request", "need from < to, at most 31 days apart")
            else:
                raise ApiError(400, "bad_request", "give date=YYYY-MM-DD or from=&to=")
            return {"from": datetime.fromtimestamp(lo / 1000, TZ).isoformat(),
                    "to": datetime.fromtimestamp(hi / 1000, TZ).isoformat(),
                    "items": self.rt.memory.timeline(lo, hi, limit=_int(q, "limit", 500, hi=2000))}
        if method == "GET" and head == "decisions":
            if len(parts) == 2:
                d = self.rt.memory.decision(parts[1])
                if not d:
                    raise ApiError(404, "not_found", f"no decision {parts[1]}")
                return {"decision": d}
            since = _ms(q["since"], "since") if q.get("since") else 0
            return {"decisions": self.rt.memory.decisions(action=q.get("action"), since_ms=since,
                                                          limit=_int(q, "limit", 50),
                                                          delivered_only=q.get("delivered") in ("1", "true"))}
        if method == "POST" and head == "events" and len(parts) == 2 and parts[1] in ("audio", "video"):
            if not isinstance(body, dict):
                raise ApiError(400, "bad_request", "body must be a JSON object")
            kind = parts[1]
            if kind == "audio" and isinstance(body.get("asr"), dict):
                out = self.rt.ingest_transcription(body["asr"])
            elif kind == "video" and isinstance(body.get("perception"), dict):
                out = self.rt.ingest_perception_frame(body["perception"])
            else:
                out = (self.rt.ingest_audio if kind == "audio" else self.rt.ingest_video)(body)
            return {"outcome": out.to_dict()}
        if method == "POST" and head == "dream":
            return {"dream": self.rt.dream()}
        if head == "questions":
            return self._questions(method, parts, q, body)
        if method == "GET" and head == "commitments" and len(parts) == 1:
            return {"commitments": self.rt.memory.commitments()}
        if method == "GET" and head == "reminders" and len(parts) == 1:
            status = {"open": "open", "done": "done", "all": None}.get(q.get("status", "open"), "?")
            if status == "?":
                raise ApiError(400, "bad_request", "status must be open, done or all")
            return {"reminders": self.rt.memory.reminders(status=status)}
        raise ApiError(404, "not_found", f"no route {method} /{API_VERSION}/{'/'.join(parts)}")

    QUESTION_STATUS = {"open": ("pending", "asked"), "answered": ("answered",),
                       "all": ("pending", "asked", "answered", "expired")}

    def _questions(self, method: str, parts: list[str], q: dict, body: Any) -> dict:
        if method == "GET" and len(parts) == 1:
            status = self.QUESTION_STATUS.get(q.get("status", "open"))
            if status is None:
                raise ApiError(400, "bad_request", "status must be open, answered or all")
            return {"questions": self.rt.memory.questions(status=status, limit=_int(q, "limit", 20))}
        found = self.rt.memory.questions(question_id=parts[1]) if len(parts) >= 2 else []
        if len(parts) >= 2 and not found:
            raise ApiError(404, "not_found", f"no question {parts[1]}")
        if method == "GET" and len(parts) == 2:
            return {"question": found[0]}
        if method == "POST" and len(parts) == 3 and parts[2] == "answer":
            question = found[0]
            if not isinstance(body, dict) or body.get("answer") not in ("yes", "no"):
                raise ApiError(400, "bad_request", "body must be {\"answer\": \"yes\" | \"no\"}")
            if question["status"] not in ("pending", "asked"):
                raise ApiError(409, "not_open", f"question is {question['status']}")
            # Simulated data: default to the agent's own latest decision time, never before
            # the question was asked, so a replayed day is answered in its own time.
            asked = question.get("asked_ms") or question.get("created_ms") or 0
            now = _ms(body["now"], "now") if body.get("now") else max(
                self.rt.agent_memory.latest_log_ms() or 0, asked)
            if question.get("expires_ms") and question["expires_ms"] <= now:
                raise ApiError(409, "not_open", "question expired")
            decisions = self.rt.agent.answer(question["id"], body["answer"], now)
            return {"decisions": [d.to_dict() for d in decisions],
                    "question": self.rt.memory.questions(question_id=question["id"])[0]}
        raise ApiError(404, "not_found", f"no route {method} /{API_VERSION}/{'/'.join(['questions', *parts[1:]])}")

    def _memory_detail(self, memory_id: str) -> dict:
        m = self.rt.memory.explain(memory_id)
        if not m:
            raise ApiError(404, "not_found", f"no memory {memory_id}")
        for e in m.get("evidence") or []:
            eid = e.get("source_event_id") or e.get("first_event_id")
            rec = self.rt.engine.get_event_record(eid) if eid else None
            e["original"] = None if rec is None else {
                "event_id": rec.event_id, "kind": rec.kind, "ingest_seq": rec.ingest_seq,
                "payload_sha256": rec.payload_sha256,
                "matches": (rec.payload_sha256 == e.get("payload_sha256")) if e.get("payload_sha256") else None,
                "text": rec.event.get("text") if rec.kind == "audio" else None}
        return m


def serve(runtime, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Start the HTTP server (returns it; call serve_forever() or shutdown())."""
    api = VixyAPI(runtime)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, default=str, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            body = None
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n > 1_000_000:
                    return self._send(413, {"schema": "vixy.error.v1", "error": {"code": "too_large",
                                                                                 "message": "body over 1 MB"}})
                try:
                    body = json.loads(self.rfile.read(n) or b"null")
                except ValueError:
                    return self._send(400, {"schema": "vixy.error.v1", "error": {"code": "bad_json",
                                                                                 "message": "body is not JSON"}})
            self._send(*api.handle(method, url.path, query, body))

        def do_GET(self):  # noqa: N802
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def log_message(self, fmt, *args):     # no request logging (paths can carry ids only)
            pass

    return ThreadingHTTPServer((host, port), Handler)
