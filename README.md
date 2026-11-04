# Athena

**A wearable that sees what you see, remembers what matters, and only speaks up when it should.**

A small camera clips onto you. Athena turns what it sees and hears into a memory graph of your days,
checks that graph in real time to decide whether to say anything, and every night "dreams": it
replays the day against everything before it and turns repeated evidence into beliefs. It runs on
small local models, and most decisions need no language model at all.

![The supermarket moment: what Athena sees, what it decides, and the one notification it sends](docs/assets/athena-moment.jpg)

## What it does

- **Sees and hears.** YOLO11n (1 frame/s) and Whisper run locally, not in the cloud.
- **Remembers with evidence.** Every utterance, visit and object lands in a FalkorDB graph, one per
  user. Every belief links back to the original recording it came from.
- **Knows who said it.** If someone else says "I have class at 6", it is *their* plan, not yours.
  Things you only heard about are kept as **not verified**.
- **Speaks up rarely.** The Active Agent is rules first: in a simulated 9-hour day, 30,420 frames
  led to 7 notifications and 2 language-model calls.
- **Dreams at night.** The Dreamer consolidates days into patterns and long-term memories. A
  routine needs several separate days, a one-off stays an anomaly, and a stated change supersedes
  the old fact while keeping its history.
- **Asks before it changes things.** If you correct a plan, Athena asks "did this change?" and only
  writes to memory after you answer.

![From a glance to a decision and a belief](docs/assets/athena-architecture.jpg)

```text
camera + mic ─► perception ─► Context Engine ─► memory graph (FalkorDB)
 (wearable)     YOLO11n         validate,            │        ▲
                Whisper         dedupe, align         ▼        │
                                              Active Agent   Dreamer
                                              (day: speak?)  (night: believe?)
                                                     │        │
                                                     └─► /v1 API ─► Athena app
```

## The app

Two screens, both reading the live backend: **Clarify** (Athena's yes/no question, the words that
raised it, and what "Yes" will change) and **Memories** (what Athena believes, and how much it
trusts it).

![Clarify, the answer, and Memories](docs/assets/athena-app.jpg)

## Run it (about 5 minutes)

You need [`uv`](https://docs.astral.sh/uv/getting-started/installation/) (it installs Python 3.12
for you), Node 18+ and `make`. Docker is optional.

```bash
git clone https://github.com/meta-hackathon-athena/athena.git && cd athena
./scripts/bootstrap.sh --no-docker              # or `make install` if you have Docker

make ui-demo VIXY_USER=app_demo FALKORDB_URL=   # two synthetic days, the Dreamer, one plan change
make serve   VIXY_USER=app_demo FALKORDB_URL=   # API on http://127.0.0.1:8765/v1   (terminal 1)
make web                                        # app on http://127.0.0.1:5173      (terminal 2)
```

Open <http://127.0.0.1:5173> in a phone-sized window, answer the question, then open Memories.
With Docker, run `make falkordb` first and drop `FALKORDB_URL=`. The code and CLI still use the
working name `vixy`.

Ask it things from the terminal:

```bash
uv run python -m vixy --user app_demo --state-dir data/app_demo ask "What do you remember about Alex?"
uv run python -m vixy --user app_demo --state-dir data/app_demo decisions --limit 5
```

`make ui-demo` also prints 18 end-to-end checks. Two of them: on Tuesday the agent recalls that
"someone mentioned Alex is allergic to peanuts" when you plan to buy peanuts for Alex (attributed,
not certain), and it refuses to invent a memory it doesn't have.

## Local first, Muse for the hackathon

Athena is meant to run on hardware you own. Your day is the most private data there is, so the
parts that watch and react in real time (perception and the Active Agent) are small, rules-first
and built to run on a pocket board (target: RUBIK Pi 3, Qualcomm QCS6490) with nothing leaving it.

For this hackathon the reasoning-heavy steps are built on **Meta Muse**: the Dreamer's nightly
focus rounds (read-only graph queries plus validated write tools) and natural-language recall for
`/ask`. Add `MODEL_API_KEY` to `.env`, use the FalkorDB server (`make falkordb`, so the Dreamer
shares the graph) and dream online:

```bash
uv run python -m prod_dreamer_model.cli --layout unified --user app_demo dream --online \
    --history-db data/app_demo/vixy_events.sqlite3
```

Without a key, everything above still runs offline with rule-based fallbacks.

## Repository

```text
vixy/          runtime, CLI, /v1 API, perception and replay
agent/         Active Agent (rules, recall, questions)
memory/        graph memory and schema
components/    context-engine/, dreamer-model/
frontend/web/  the Athena app (no build step, Node built-ins only)
demo/          MP4 simulator
docs/          architecture, API, graph schema, reports
```

More detail: [architecture](docs/ARCHITECTURE.md) · [graph schema](docs/GRAPH_SCHEMA.md) ·
[API](docs/API.md) · [app integration](docs/FRONTEND_INTEGRATION.md) · [simulator](demo/README.md).

The API has no authentication and binds to `127.0.0.1`, so keep it local.
