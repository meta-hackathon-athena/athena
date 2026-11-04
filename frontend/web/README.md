# Athena web app

Two screens for the demo, in white and celeste:

| Screen | URL | Backend |
|---|---|---|
| **Clarify** | `http://127.0.0.1:5173/#/clarify` | `GET /v1/questions`, `POST /v1/questions/{id}/answer` |
| **Memories** | `http://127.0.0.1:5173/#/memories` | `GET /v1/memories`, `/v1/memories/{id}`, `/v1/commitments`, `/v1/reminders` |

No npm install: `server.mjs` uses Node built-ins only (Node 18 or newer). It serves `public/` and
proxies `/v1/*` to the backend, because the backend sends no CORS headers.

## Run

From the repository root, in three terminals (or run the first line once):

```bash
make ui-demo VIXY_USER=app_demo      # two synthetic days + one spoken plan change (needs FalkorDB: make falkordb)
make serve VIXY_USER=app_demo        # API on http://127.0.0.1:8765/v1
make web                             # this app on http://127.0.0.1:5173
```

Settings (environment variables for `server.mjs`): `ATHENA_API` (default `http://127.0.0.1:8765`),
`PORT` (default `5173`), `HOST` (default `127.0.0.1`).

## What is real

Everything on both screens comes from the running backend.

- **Clarify** shows the newest open question the Active Agent asked, the utterance that raised it
  (`heard`), what is saved right now (`about`), and what "yes" will change (`proposal`, worded from
  the agent's allow-listed operation). Yes and No call `POST /v1/questions/{id}/answer`, which runs
  `ActiveAgent.answer`. "Yes" applies the write (for example `update_commitment_time`), "no" changes
  nothing. The confirmation screen is built from the updated record the API returns.
- **Memories** shows Dreamer `LongTermMemory` claims (People, About you) with their trust level
  (`epistemic_status`: someone else said it and it isn't verified, you said it, and so on), first
  heard time, evidence check and history (`supersedes`), plus the agent's plans and reminders. After
  an answer, the card it changed is highlighted as "Just updated from your answer".

What an answer does **not** do: it doesn't verify or reject a Dreamer memory such as "Alex is
allergic to peanuts". The backend has no operation for that yet, so those cards stay "Not verified".

## Demo script (about 30 seconds)

1. `#/clarify`: "Did Meet Marta change from 9 AM to 11 AM on Thu 05 Nov?" with "You said: Actually I
   will meet Marta on Thursday at 11am" and "Saved right now: Meet Marta, Thu 5 Nov, 9:00 AM, Tentative".
2. Press **Yes**. The confirmation says the plan is now 11:00 AM, confirmed, moved from 9:00 AM.
3. **See it in Memories**: the Meet Marta card is highlighted, Confirmed, "Moved from 9:00 AM".
   People cards show Alex's allergy, Lena's move from Lisbon to Berlin, Marta's presentation and
   Sam's diet, each marked "Not verified".
4. Optional: back on Clarify, the second question "Mark 'buy milk at the supermarket' as done?".

Answers are stored, so to record again, reset the data first: stop `make serve`, rerun
`make ui-demo VIXY_USER=app_demo`, then start `make serve` again.

## Fixture mode (offline fallback)

`http://127.0.0.1:5173/?fixtures=1#/clarify` runs without a backend from
[`public/fixtures.js`](public/fixtures.js), a snapshot of real API responses. An amber banner says
so on every screen, and answers are not saved anywhere. Regenerate the snapshot right after
`make ui-demo`, before answering:

```bash
node frontend/web/capture-fixtures.mjs
```

## Files

```text
server.mjs            static files + /v1 proxy
capture-fixtures.mjs  rewrites public/fixtures.js from a running backend
public/index.html     shell, top bar, navigation
public/app.js         router and the two screens (loading, submitting, confirmation, empty, error states)
public/api.js         the only module that calls the backend; fixture mode switch
public/format.js      dates (Asia/Singapore, the backend's VIXY_TZ) and readable wording
public/styles.css     white and celeste theme
public/logo.svg       wordmark icon
```
