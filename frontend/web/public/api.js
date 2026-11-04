// The only module that talks to the backend. Every call goes to the same-origin /v1 proxy
// (frontend/web/server.mjs), which forwards to `make serve`.
//
// Fixture mode (?fixtures=1) swaps in fixtures.js, an offline snapshot of real responses.
// It is labelled on screen and never claims to save anything.

import { FIXTURES } from "./fixtures.js";

// The hosted preview (GitHub Pages) has no backend, so it always uses the snapshot.
export const HOSTED_PREVIEW = location.hostname.endsWith("github.io");
export const FIXTURE_MODE = HOSTED_PREVIEW || new URLSearchParams(location.search).has("fixtures");

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function call(method, path, body) {
  if (FIXTURE_MODE) return fixture(method, path, body);
  let res;
  try {
    res = await fetch(`/v1${path}`, {
      method,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "network", "The web app server is not reachable.");
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* non-JSON error page */
  }
  if (!res.ok || !data || data.schema !== "vixy.api.v1") {
    const err = data?.error ?? {};
    throw new ApiError(res.status, err.code ?? "http_error", err.message ?? `HTTP ${res.status}`);
  }
  return data;
}

export const api = {
  health: () => call("GET", "/health"),
  openQuestions: () => call("GET", "/questions?status=open&limit=20").then((d) => d.questions),
  answer: (id, answer) => call("POST", `/questions/${encodeURIComponent(id)}/answer`, { answer }),
  memories: () => call("GET", "/memories?status=active,unconfirmed&limit=100").then((d) => d.memories),
  memory: (id) => call("GET", `/memories/${encodeURIComponent(id)}`).then((d) => d.memory),
  commitments: () => call("GET", "/commitments").then((d) => d.commitments),
  reminders: () => call("GET", "/reminders?status=all").then((d) => d.reminders),
};

// ------------------------------------------------------------------ fixture mode
const local = structuredClone(FIXTURES);

// Mirror what "Yes" does on the backend, only in this page's copy, so the screens stay consistent.
function applyLocally(q) {
  const p = q.proposal;
  if (!p) return;
  const now = q.heard?.ts_ms ?? q.created_ms;   // simulated time, like the backend
  if (p.op === "update_commitment_time") {
    const c = local.commitments.find((x) => x.id === p.cid);
    for (const x of [c, ...(q.about ?? []).filter((a) => a.id === p.cid)]) {
      if (x) Object.assign(x, { prev_start_ms: x.start_ms, start_ms: p.start_ms, status: "confirmed", updated_ms: now });
    }
  }
  if (p.op === "complete_reminder") {
    const r = local.reminders.find((x) => x.id === (p.note_id ?? p.id));
    if (r) Object.assign(r, { status: "done", done_ms: now });
  }
}

function fixture(method, path, body) {
  const [route, query = ""] = path.split("?");
  const parts = route.split("/").filter(Boolean).map(decodeURIComponent);
  const reply = (x) => new Promise((ok) => setTimeout(() => ok({ schema: "vixy.api.v1", ...x }), 350));
  if (parts[0] === "health") return reply({ ok: true, graph: "fixtures", fixtures: true });
  if (parts[0] === "questions" && parts.length === 1) {
    const open = new URLSearchParams(query).get("status") === "open";
    return reply({ questions: local.questions.filter((q) => !open || ["pending", "asked"].includes(q.status)) });
  }
  if (parts[0] === "questions" && parts[2] === "answer" && method === "POST") {
    const q = local.questions.find((x) => x.id === parts[1]);
    if (!q) return Promise.reject(new ApiError(404, "not_found", "no such question"));
    q.status = "answered";
    q.answer = body.answer;
    if (body.answer === "yes") applyLocally(q);
    return reply({ fixture: true, decisions: [{ action: body.answer === "yes" ? "MEMORY_UPDATE" : "NO_ACTION",
                                                status: "not_saved", reason: "fixture mode: nothing was sent" }],
                   question: q });
  }
  if (parts[0] === "memories" && parts.length === 2) return reply({ memory: local.memoryDetails[parts[1]] ?? {} });
  if (parts[0] === "memories") return reply({ memories: local.memories, patterns: [] });
  if (parts[0] === "commitments") return reply({ commitments: local.commitments });
  if (parts[0] === "reminders") return reply({ reminders: local.reminders });
  return Promise.reject(new ApiError(404, "not_found", `fixture has no ${path}`));
}
