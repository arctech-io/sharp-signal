# Demo Video Script — Sharp Signal

**Target:** Under 5 minutes (aim for ~4:00–4:30)
**Tone:** Concise, technical, confident

---

## 0:00 – 0:30 — The Problem

> _Camera shares screen, terminal or browser visible._

- "Betting odds move for a reason — injuries, weather, market sentiment."
- "Most punters see the move but don't know if it's statistically significant."
- "Sharp Signal ingests live World Cup odds from the TxLINE API, detects sharp moves with a dual-threshold engine, tracks whether each prediction was correct, and serves a live dashboard."

---

## 0:30 – 1:15 — Start the service & show the dashboard

- "Clone the repo, `cp .env.example .env`, fill in credentials."
- Start the app: `uvicorn app.main:app` _(or `docker build -t sharp-signal . && docker run -p 8000:8000 --env-file .env sharp-signal`)_

_Navigate to `localhost:8000`._

- "Here's the dashboard — dark themed, auto-refreshes every 10 seconds."
- "No signals yet because polling just started. Give it a moment…"

---

## 1:15 – 2:30 — Live signal appears

> _Wait for a signal card to appear on dashboard, or fast-forward a video clip._

- "A signal just fired. Let's look at the detail."
- Point to the card: match ID, market (1X2), selection (Home), direction (DRIFTING).
- "Confidence is 82.1 — well past the thresholds."
- Read the reason: _"Odds moved 12.3% in 8 minutes, 3.1 standard deviations from the recent average — sharp drifting."_
- "That's not a hunch — that's math."

_Optional: switch to terminal and show `GET /signals` JSON response._

- "The same data is available via the REST API if you want to pipe it elsewhere."

---

## 2:30 – 3:00 — Accuracy tracker

> _Switch to a resolved match or fast-forward to a state where matches have finished._

- "Once a match ends, the resolver fetches the final score from TxLINE."
- "If odds shortened on a team and they won → correct signal. If they drifted → incorrect."
- "The accuracy tracker updates automatically: `GET /accuracy`."
- "This turns signals from interesting trivia into a measurable track record."

---

## 3:00 – 3:30 — 30-second code walkthrough

> _Open `app/detection/engine.py` in an editor._

- "The core detection logic lives here. Pure functions, no I/O — easy to test."
- Point to `check_for_signal`:
  - "Grab the rolling window, compute mean & std, z-score the new value."
  - "If z-score OR percent change crosses the threshold → emit a Signal."
- "Every threshold is configurable via env var. Tune for sensitivity vs precision."
- "No magic numbers, no black-box ML — deterministic and auditable."

---

## 3:30 – 4:00 — Testing & deployment

- "129 tests pass. Every pure math function has edge-case coverage."
- "Deployed via Docker to Railway with one build command."
- "Graceful shutdown, rate-limit respect, no secrets in code."

---

## 4:00 – 4:15 — Wrap

- "Sharp Signal: real-time odds monitoring with statistical rigour."
- "Repo at github.com/arctech-io/sharp-signal — MIT, open for feedback."
