// Athena web app: two screens, hash routed.
//   #/clarify   the agent's open question, answered with Yes / No (POST /v1/questions/{id}/answer)
//   #/memories  memory cards from the knowledge graph (memories, plans, reminders)

import { api, ApiError, FIXTURE_MODE } from "./api.js";
import * as f from "./format.js";

const app = document.getElementById("app");
const askCount = document.getElementById("ask-count");
const state = {
  justUpdated: null,          // {aboutId, label, answer} after an answer, highlighted on Memories
  clarify: { phase: "loading", questions: [], error: null, result: null, choice: null },
};

if (FIXTURE_MODE) document.getElementById("fixture-banner").hidden = false;

// ------------------------------------------------------------------ helpers
const icon = {
  check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.2 4.2L19 7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  calendar: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="5" width="17" height="15" rx="3" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M3.5 10h17M8 3v4M16 3v4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  bell: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 16V11a6 6 0 1 1 12 0v5l1.5 2h-15z M10 20.5a2.2 2.2 0 0 0 4 0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
  ear: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 9a4.5 4.5 0 1 1 9 0c0 3-3 3.6-3 6.5a2.5 2.5 0 0 1-4.6 1.3M10.5 9.5a2 2 0 1 1 4 0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  bookmark: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3.5h10v17l-5-3.6-5 3.6z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
};
const chip = (s) => s ? `<span class="chip ${s.tone}">${f.esc(s.label)}${s.note ? ` <span class="chip-note">${f.esc(s.note)}</span>` : ""}</span>` : "";

function setCount(n) {
  askCount.hidden = !n;
  askCount.textContent = n ? String(n) : "";
}

function errorView(err, retry) {
  const offline = err instanceof ApiError && (err.code === "backend_unreachable" || err.code === "network");
  app.innerHTML = `
    <section class="panel state-panel" role="alert">
      <h1>${offline ? "Athena's backend isn't running" : "Something went wrong"}</h1>
      <p>${offline ? "Start it from the repository root, then try again:" : f.esc(err.message)}</p>
      ${offline ? '<pre><code>make ui-demo VIXY_USER=app_demo\nmake serve VIXY_USER=app_demo</code></pre>' : ""}
      <div class="row"><button class="btn primary" id="retry">Try again</button>
      ${offline && !FIXTURE_MODE ? '<a class="btn ghost" href="?fixtures=1#/clarify">Preview with fixtures</a>' : ""}</div>
    </section>`;
  document.getElementById("retry").onclick = retry;
}

// ------------------------------------------------------------------ Clarify
async function loadClarify() {
  const c = state.clarify;
  if (c.phase !== "done") {
    c.phase = "loading";
    renderClarify();
  }
  try {
    c.questions = await api.openQuestions();
    setCount(c.questions.length);
    if (c.phase !== "done") c.phase = c.questions.length ? "ask" : "empty";
    renderClarify();
  } catch (err) {
    c.error = err;
    errorView(err, loadClarify);
  }
}

function renderClarify() {
  const c = state.clarify;
  if (c.phase === "loading") {
    app.innerHTML = `<section class="clarify" aria-busy="true">
      <div class="skeleton line short"></div><div class="skeleton line title"></div><div class="skeleton line title two"></div>
      <div class="skeleton block"></div><div class="answers"><div class="skeleton btn-s"></div><div class="skeleton btn-s"></div></div></section>`;
    return;
  }
  if (c.phase === "empty") {
    app.innerHTML = `<section class="clarify empty">
      <div class="done-icon">${icon.check}</div>
      <h1 class="question small">All clear</h1>
      <p class="lede">Athena has nothing to check with you right now.</p>
      <a class="btn primary" href="#/memories">See what Athena remembers</a></section>`;
    return;
  }
  if (c.phase === "done") return renderDone();

  const q = c.questions[0];
  const heard = f.heardLine(q.heard);
  const saved = f.savedLine(q.about?.[0]);
  const effect = f.yesEffect(q);
  const busy = c.phase === "submitting";
  const total = c.questions.length;
  app.innerHTML = `
    <section class="clarify" aria-busy="${busy}">
      <p class="eyebrow"><span class="pulse" aria-hidden="true"></span>Athena wants to check something
        ${total > 1 ? `<span class="step">1 of ${total}</span>` : ""}</p>
      <h1 class="question" id="q-text">${f.esc(q.text).replace(/(\d) (AM|PM)\b/g, "$1&nbsp;$2")}</h1>
      <div class="context">
        ${heard ? `<div class="ctx">
          <div class="ctx-icon">${icon.ear}</div>
          <div><div class="ctx-label">${f.esc(heard.who)}</div>
            <blockquote>"${f.esc(heard.text)}"</blockquote>
            ${heard.at ? `<div class="ctx-meta">${f.esc(heard.at)}</div>` : ""}</div></div>` : ""}
        ${saved ? `<div class="ctx">
          <div class="ctx-icon">${icon.bookmark}</div>
          <div><div class="ctx-label">Saved right now</div>
            <div class="ctx-value">${f.esc(saved.title)} <span class="dot">·</span> ${f.esc(saved.detail)}</div>
            <div class="ctx-chips">${chip(saved.status)}</div></div></div>` : ""}
      </div>
      ${effect ? `<p class="effect"><strong>Yes</strong> will ${f.esc(effect.charAt(0).toLowerCase() + effect.slice(1))}
        <strong>No</strong> keeps everything as it is.</p>` : ""}
      <div class="answers" role="group" aria-labelledby="q-text">
        <button class="btn answer no" data-answer="no" ${busy ? "disabled" : ""}>
          ${busy && c.choice === "no" ? '<span class="spinner"></span>Saving' : "No"}</button>
        <button class="btn answer yes" data-answer="yes" ${busy ? "disabled" : ""}>
          ${busy && c.choice === "yes" ? '<span class="spinner"></span>Saving' : "Yes"}</button>
      </div>
      ${c.error ? `<p class="inline-error" role="alert">${f.esc(c.error)}</p>` : ""}
      <p class="hint">Press <kbd>Y</kbd> or <kbd>N</kbd></p>
    </section>`;
  app.querySelectorAll("[data-answer]").forEach((b) => (b.onclick = () => submit(q, b.dataset.answer)));
}

async function submit(q, answer) {
  const c = state.clarify;
  if (c.phase !== "ask") return;
  c.phase = "submitting";
  c.choice = answer;
  c.error = null;
  renderClarify();
  try {
    const res = await api.answer(q.id, answer);
    const applied = res.decisions?.find((d) => d.action === "MEMORY_UPDATE");
    if (answer === "yes" && applied && applied.status === "failed") {
      throw new Error("Athena couldn't apply that change. Nothing was saved.");
    }
    const changed = answer === "yes" && (applied?.status === "done" || Boolean(res.fixture));
    c.result = { question: res.question ?? q, answer, fixture: Boolean(res.fixture), changed,
                 reason: res.decisions?.[0]?.reason };
    const about = res.question?.about?.[0];
    state.justUpdated = changed && about ? { aboutId: about.id, label: about.label } : null;
    c.questions = c.questions.filter((x) => x.id !== q.id);
    setCount(c.questions.length);
    c.phase = "done";
  } catch (err) {
    c.phase = "ask";
    c.error = err instanceof ApiError && err.code === "not_open"
      ? "This question was already answered or has expired. Refreshing."
      : `Your answer wasn't saved. ${err.message}`;
    if (err instanceof ApiError && err.code === "not_open") setTimeout(loadClarify, 1200);
  }
  renderClarify();
}

function renderDone() {
  const { question, answer, fixture, changed, reason } = state.clarify.result;
  // Only claim a change when the agent reports a MEMORY_UPDATE that was applied.
  const s = answer === "yes" && !changed
    ? { title: "Answer recorded", body: `Athena didn't change anything${reason ? `: ${reason}` : ""}.` }
    : f.answeredSummary(question, answer);
  const left = state.clarify.questions.length;
  app.innerHTML = `
    <section class="clarify done" aria-live="polite">
      <div class="done-icon ${changed ? "" : "muted"}">${icon.check}</div>
      <p class="eyebrow center">You answered ${answer === "yes" ? "Yes" : "No"}</p>
      <h1 class="question small">${f.esc(s.title)}</h1>
      <p class="lede">${f.esc(s.body)}</p>
      <p class="provenance">${fixture ? "Fixture preview: nothing was saved."
        : changed ? "Saved to Athena's knowledge graph." : "Your answer is stored with the question."}</p>
      <div class="row center">
        <a class="btn primary" href="#/memories">${changed ? "See it in Memories" : "Go to Memories"}</a>
        ${left ? `<button class="btn ghost" id="next">Next question (${left})</button>` : ""}
      </div>
    </section>`;
  const next = document.getElementById("next");
  if (next) next.onclick = () => { state.clarify.phase = "ask"; renderClarify(); };
}

document.addEventListener("keydown", (e) => {
  if (route() !== "clarify" || state.clarify.phase !== "ask" || e.metaKey || e.ctrlKey || e.altKey) return;
  const key = e.key.toLowerCase();
  if (key === "y" || key === "n") submit(state.clarify.questions[0], key === "y" ? "yes" : "no");
});

// ------------------------------------------------------------------ Memories
async function loadMemories() {
  app.innerHTML = `<section class="memories" aria-busy="true">
    <div class="skeleton line title"></div><div class="skeleton line short"></div>
    <div class="grid">${'<div class="skeleton card-s"></div>'.repeat(4)}</div></section>`;
  try {
    const [memories, commitments, reminders, questions] = await Promise.all([
      api.memories(), api.commitments(), api.reminders(), api.openQuestions().catch(() => null)]);
    if (questions) setCount(questions.length);
    const details = Object.fromEntries(await Promise.all(
      memories.map((m) => api.memory(m.id).then((d) => [m.id, d]).catch(() => [m.id, null]))));
    if (route() === "memories") renderMemories({ memories, commitments, reminders, details });
  } catch (err) {
    errorView(err, loadMemories);
  }
}

function card({ id, cls = "", kicker, avatar, iconSvg, status, text, sub, history, meta }) {
  const hot = state.justUpdated?.aboutId === id;
  return `<article class="card ${cls} ${hot ? "just-updated" : ""}" id="card-${f.esc(id)}">
    ${hot ? '<div class="updated-badge">Just updated from your answer</div>' : ""}
    <div class="card-top">
      ${avatar ? `<span class="avatar">${f.esc(avatar)}</span>` : `<span class="avatar icon">${iconSvg}</span>`}
      <div class="kicker">${kicker}</div>
    </div>
    <p class="card-text">${f.esc(text)}</p>
    ${sub ? `<p class="card-sub">${f.esc(sub)}</p>` : ""}
    ${history ? `<p class="card-history">${f.esc(history)}</p>` : ""}
    <div class="card-foot">${chip(status)}${meta ? `<span class="meta">${f.esc(meta)}</span>` : ""}</div>
  </article>`;
}

function renderMemories({ memories, commitments, reminders, details }) {
  const people = memories.filter((m) => m.subject && m.subject !== "user");
  const you = memories.filter((m) => !m.subject || m.subject === "user");
  const memCard = (m) => {
    const d = details[m.id];
    const prev = (d?.supersedes ?? []).map((s) => s.statement).filter(Boolean);
    const unverifiedNote = m.contested ? { label: "Conflicting reports", tone: "unverified" } : null;
    return card({
      id: m.id, cls: f.trust(m).tone === "unverified" ? "unverified" : "",
      avatar: m.subject && m.subject !== "user" ? f.initial(m.subject) : null, iconSvg: icon.bookmark,
      kicker: `<b>${f.esc(m.subject && m.subject !== "user" ? m.subject : "You")}</b> <span class="dot">·</span> ${f.esc(f.topic(m))}`,
      status: unverifiedNote ?? f.trust(m), text: m.statement,
      history: prev.length ? `Previously: ${prev.join(" ")}` : null,
      meta: [f.firstHeard(d, m), d ? f.sourceLine(d) : null].filter(Boolean).join(" · "),
    });
  };
  const planCard = (c) => card({
    id: c.id, iconSvg: icon.calendar, kicker: `<b>Plan</b>${c.people?.length ? ` <span class="dot">·</span> with ${f.esc(c.people.join(", "))}` : ""}`,
    status: f.commitmentStatus(c), text: c.title, sub: f.when(c.start_ms),
    history: f.movedLine(c), meta: c.updated_ms && c.updated_ms !== c.created_ms ? `Updated ${f.when(c.updated_ms)}` : null,
  });
  const reminderCard = (r) => card({
    id: r.id, iconSvg: icon.bell,
    kicker: `<b>Reminder</b>${r.trigger_place_type ? ` <span class="dot">·</span> at the ${f.esc(r.trigger_place_type.replace(/_/g, " "))}` : ""}`,
    status: r.status === "done" ? { label: "Done", tone: "done" } : { label: "Open", tone: "open" },
    text: f.task(r.text), meta: r.status === "done" && r.done_ms ? `Done ${f.when(r.done_ms)}` : `Set ${f.when(r.ts_ms)}`,
  });
  const section = (title, sub, items, render) => items.length ? `
    <section class="mem-section"><div class="section-head"><h2>${title}</h2><span>${sub}</span></div>
    <div class="grid">${items.map(render).join("")}</div></section>` : "";
  const total = memories.length + commitments.length + reminders.length;
  app.innerHTML = `
    <section class="memories">
      <header class="page-head">
        <h1>What Athena remembers</h1>
        <p>Short summaries of what Athena has learned, built from its knowledge graph. Anything it hasn't verified is marked.</p>
      </header>
      ${total ? "" : '<div class="panel state-panel"><h1>No memories yet</h1><p>Athena forms memories after it has heard and seen your day and consolidated it overnight.</p></div>'}
      ${section("Plans", "From what you said", commitments, planCard)}
      ${section("People", "What you've heard about others", people, memCard)}
      ${section("About you", "Your own words", you, memCard)}
      ${section("Reminders", "Triggered by places", reminders, reminderCard)}
    </section>`;
  const hot = state.justUpdated && document.getElementById(`card-${state.justUpdated.aboutId}`);
  if (hot) hot.scrollIntoView({ block: "center", behavior: "smooth" });
}

// ------------------------------------------------------------------ router
const route = () => (location.hash.replace(/^#\/?/, "") || "clarify").split("?")[0];

function go() {
  const r = route() === "memories" ? "memories" : "clarify";
  document.querySelectorAll("[data-route]").forEach((a) => a.setAttribute("aria-current", a.dataset.route === r ? "page" : "false"));
  window.scrollTo({ top: 0 });
  if (r === "memories") loadMemories();
  else {
    if (state.clarify.phase === "done") state.clarify.phase = "loading";    // coming back: next question
    loadClarify();
  }
}

window.addEventListener("hashchange", go);
go();
