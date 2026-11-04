// Turning API records into short, readable sentences. Pure functions, no I/O.
//
// Times are simulated event times in the backend's timezone (VIXY_TZ, default
// Asia/Singapore), so they are shown in that zone, not the viewer's.

export const TZ = "Asia/Singapore";

const dayFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, weekday: "short", day: "numeric", month: "short" });
const timeFmt = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit" });
const keyFmt = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });

export const day = (ms) => dayFmt.format(new Date(ms)).replace(",", "");
export const time = (ms) => timeFmt.format(new Date(ms)).replace(/\s(AM|PM)$/, "\u00a0$1");   // keep "9:00 AM" together
export const when = (ms) => `${day(ms)}, ${time(ms)}`;
const sameDay = (a, b) => keyFmt.format(new Date(a)) === keyFmt.format(new Date(b));

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

export const task = (text) =>
  String(text ?? "").replace(/^(remind me to|remind me|don't forget to|dont forget to)\s+/i, "").replace(/[ .!]+$/, "")
    .replace(/^./, (c) => c.toUpperCase());

// ------------------------------------------------------------------ clarifications
export function heardLine(heard) {
  if (!heard) return null;
  const who = { wearer: "You said", other: "Someone near you said" }[heard.speaker_role] ?? "Athena heard";
  return { who, text: heard.text, at: heard.ts_ms ? when(heard.ts_ms) : null };
}

export function savedLine(about) {
  if (!about) return null;
  if (about.label === "Commitment") {
    return { title: about.title, detail: when(about.start_ms), status: commitmentStatus(about) };
  }
  if (about.label === "Note") {
    return { title: task(about.text), detail: "Reminder", status: about.status === "done"
      ? { label: "Done", tone: "done" } : { label: "Open", tone: "open" } };
  }
  return null;
}

// What a "yes" does, worded from the agent's allow-listed operation (agent/memory_tools.py).
export function yesEffect(q) {
  const p = q.proposal, about = q.about?.[0];
  if (!p) return null;
  if (p.op === "update_commitment_time") {
    return `Move ${about?.title ?? "the plan"} to ${when(p.start_ms)} and mark it confirmed.`;
  }
  if (p.op === "record_commitment") return `Save ${p.title} on ${when(p.start_ms)} as a confirmed plan.`;
  if (p.op === "complete_reminder") return `Mark the reminder "${task(about?.text) || "this reminder"}" as done.`;
  return null;
}

export function answeredSummary(q, answer) {
  const about = q.about?.[0];
  if (answer === "no") return { title: "Got it. Nothing was changed.", body: "Athena keeps what it had saved." };
  if (about?.label === "Commitment") {
    const moved = about.prev_start_ms && about.prev_start_ms !== about.start_ms
      ? ` Moved from ${sameDay(about.prev_start_ms, about.start_ms) ? time(about.prev_start_ms) : when(about.prev_start_ms)}.` : "";
    return { title: "Plan updated", body: `${about.title} is now ${when(about.start_ms)}, confirmed by you.${moved}` };
  }
  if (about?.label === "Note") {
    return { title: "Reminder done", body: `"${task(about.text)}" is marked as done.` };
  }
  return { title: "Answer saved", body: "Athena recorded your answer." };
}

// ------------------------------------------------------------------ memory cards
const TOPIC = {
  allergic_to: "Allergy", diet: "Diet", avoids: "Avoids", likes: "Likes", dislikes: "Dislikes",
  relationship: "Relationship", lives_in: "Home city", works_at: "Work", studies_at: "Studies",
  has_event: "Upcoming event", states: "Note",
};

export function topic(m) {
  if (TOPIC[m.predicate]) return TOPIC[m.predicate];
  return { preference: "Preference", routine: "Routine", schedule: "Schedule" }[m.kind] ?? "Fact";
}

// epistemic_status per docs/GRAPH_SCHEMA.md; "reported" and "hypothesis" are unverified
export function trust(m) {
  const by = m.reported_by ?? [];
  switch (m.epistemic_status) {
    case "self_reported": return { label: "You said this", tone: "you" };
    case "reported":
      return by.includes("other") ? { label: "Heard from someone else", note: "Not verified", tone: "unverified" }
                                  : { label: "Speaker unknown", note: "Not verified", tone: "unverified" };
    case "hypothesis": return { label: "Said with doubt", note: "Not verified", tone: "unverified" };
    case "inferred": return { label: "Learned from your days", tone: "inferred" };
    case "observed": return { label: "Seen by the camera", tone: "inferred" };
    default: return { label: "Remembered", tone: "inferred" };
  }
}

export function sourceLine(detail) {
  const ev = detail?.evidence ?? [];
  if (!ev.length) return "No source records left";
  const checked = ev.every((e) => e.original?.matches === true);
  const n = ev.length === 1 ? "1 source" : `${ev.length} sources`;
  return checked ? `${n}, original recording checked` : `${n}, can't be verified`;
}

export function firstHeard(detail, m) {
  const ms = detail?.first_stated_ms ?? detail?.evidence?.[0]?.ts_ms ?? null;
  if (!ms) return null;
  return `${m.subject === "user" ? "Noted" : "First heard"} ${when(ms)}`;
}

export function commitmentStatus(c) {
  if (c.status === "confirmed") return { label: "Confirmed", tone: "done" };
  if (c.status === "cancelled") return { label: "Cancelled", tone: "muted" };
  return { label: "Tentative", note: "Not confirmed yet", tone: "unverified" };
}

export function movedLine(c) {
  if (!c.prev_start_ms || c.prev_start_ms === c.start_ms) return null;
  return `Moved from ${sameDay(c.prev_start_ms, c.start_ms) ? time(c.prev_start_ms) : when(c.prev_start_ms)}`;
}

export const initial = (name) => (String(name ?? "?").trim()[0] ?? "?").toUpperCase();
