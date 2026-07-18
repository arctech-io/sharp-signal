# Sharp Signal

Sharp Signal is a real-time betting odds monitoring service. It ingests World Cup odds from the TxLINE API, detects statistically significant movements ("sharp signals"), tracks whether those signals were correct after match outcomes are finalised, and serves a live dashboard.

> **Built for the [TxODDS World Cup hackathon](https://earn.superteam.fun) on Superteam Earn** — a fully autonomous agent that ingests live TxLINE World Cup feeds, flags sharp odds movements, and tracks prediction accuracy with zero manual intervention.

## How It Works

1. **Poll** — A background loop fetches odds snapshots from the TxLINE API every `POLL_INTERVAL_SECONDS`.
2. **Detect** — Each odds update is run through a detection engine that computes z-scores and percent changes against a rolling window. If either threshold is breached, a signal is emitted with a direction (SHORTENING/DRIFTING), a confidence score (0–100), and a one-sentence human-readable reason.
3. **Resolve** — After matches finish, the resolver fetches final scores from TxLINE and marks each pending signal as CORRECT or INCORRECT based on whether the odds movement predicted the actual outcome.
4. **Serve** — A REST API exposes recent signals, per-signal detail, and aggregate accuracy stats. A live HTML dashboard auto-refreshes every 10 seconds.

## Quick Start

### Local Development

```bash
# Clone and set up
cd sharp-signal
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your TxLINE credentials and preferred thresholds

# Run
uvicorn app.main:app --reload
```

The app is now at `http://localhost:8000`. The dashboard is served at `/`.

### Docker

```bash
docker build -t sharp-signal .
docker run -p 8000:8000 --env-file .env sharp-signal
```

### Run Tests

```bash
pytest -v
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Live HTML dashboard (auto-refreshes every 10s) |
| `GET` | `/health` | Service health — uptime, last successful poll time |
| `GET` | `/signals?limit=50&status=pending` | Recent signals, newest first. Filter by status: `pending`, `correct`, `incorrect` |
| `GET` | `/signals/{id}` | Single signal detail |
| `GET` | `/accuracy` | Aggregate accuracy stats — total resolved, correct count, incorrect count, accuracy percentage |

## TxLINE API Feedback

### What worked well

- **Single normalised schema.** Every competition returns the same JSON shape, so one client handles all World Cup fixtures without per-league branching.
- **Snapshot endpoints are fast and consistent.** `fixtures/snapshot`, `odds/snapshot/{fixtureId}`, and `scores/snapshot/{fixtureId}` returned clean, stable payloads on every poll.
- **Auth was straightforward.** JWT `Authorization: Bearer` + `X-Api-Token` header worked first try.
- **Zero-cost access** for the hackathon made it trivial to poll every 60s against live data.

### Where I hit friction

- **Odds values are probability × 1000, not decimal odds.** A selection becoming *more likely* shows as a *higher* number (e.g. `2376` = 2.376 implied probability). This is the opposite of decimal-odds intuition, so direction (shortening/drifting) must be inverted relative to a typical bookmaker feed. Easy to normalise, but not documented.
- **Scores snapshot returns multiple entries per match.** Each match emits several `Action` records (`comment`, `coverage_update`, …); the final result must be inferred from `Action == "game_finalised"` (or `StatusId == 100` / `Period == 100`). Live/in-progress scores are intentionally ignored so signals never resolve against an unfinished match.
- **No rate-limit headers.** I added a defensive 0.5s delay between per-fixture odds requests to avoid hammering the feed.
- **Dev feed clock.** In the dev environment the World Cup fixtures were scheduled but final scores did not appear within the build window, so the resolver holds signals pending until the feed emits finalisation. The code path is verified by tests; live resolution depends on the feed emitting `game_finalised`.

### Suggested improvements

- **A combined endpoint** returning odds + scores for all fixtures in one call would remove the N+1 request pattern (one `odds/snapshot` per fixture).
- **Document the `StatusId` / `Period` / `Action` finalisation fields** explicitly, and publish the exact probability-encoding (×1000) in the quickstart.
- **A `finalised` boolean** on each score entry would let consumers skip the action-string inference.

---

## Configuration

All settings are loaded from environment variables (or a `.env` file). Copy `.env.example` to `.env` and fill in your values.

| Variable | Default | Description |
|----------|---------|-------------|
| `TXLINE_API_KEY` | `""` | TxLINE JWT bearer token |
| `TXLINE_API_TOKEN` | `""` | TxLINE API token (`X-Api-Token` header) |
| `TXLINE_BASE_URL` | `""` | TxLINE API base URL |
| `TXLINE_COMPETITION_FILTER` | `World Cup` | Only ingest fixtures whose competition contains this string (case-insensitive) |
| `SHARP_RESET_DB` | `false` | If `true`, wipe the database on startup (fresh demo slate) |
| `POLL_INTERVAL_SECONDS` | `60` | Seconds between ingestion cycles |
| `DATABASE_URL` | `sqlite:///./sharp_signal.db` | SQLAlchemy database URL |
| `Z_SCORE_THRESHOLD` | `2.0` | Z-score at which a movement is flagged as a signal |
| `PCT_CHANGE_THRESHOLD` | `5.0` | Minimum percent change between consecutive odds to flag |
| `ROLLING_WINDOW_SIZE` | `20` | Number of recent odds values used for mean/std calculation |
| `MIN_WINDOW_SIZE` | `5` | Minimum data points before detection activates for a market |
| `SIGNAL_COOLDOWN_SECONDS` | `600` | Suppress a repeated same-direction signal for the same market/selection within this window |

### Tuning the Thresholds

- **Z-score threshold** — Lower values (e.g. 1.5) produce more signals but increase false positives. Higher values (e.g. 3.0) are more conservative.
- **Percent change threshold** — Catches single-jump spikes that z-score might miss. Set to `0` to rely solely on z-score.
- **Rolling window size** — Larger windows smooth out noise but react more slowly to trends. Smaller windows are more responsive but noisier.
- **Min window size** — How many odds updates must be collected before detection starts for a given market+selection. Prevents spurious signals from a handful of data points.

## Project Structure

```
sharp-signal/
├── app/
│   ├── api/
│   │   ├── routes.py          # REST endpoints
│   │   └── dashboard.html     # Live dashboard
│   ├── config.py              # Settings from env vars
│   ├── detection/
│   │   ├── engine.py          # Detection math + DetectionEngine
│   │   └── resolver.py        # Signal resolution against outcomes
│   ├── ingestion/
│   │   └── txline_client.py   # TxLINE async client with retry
│   ├── storage/
│   │   ├── db.py              # SQLAlchemy schema + CRUD
│   │   └── models.py          # Pydantic data models
│   └── main.py                # FastAPI app + background loop
├── tests/
│   ├── test_detection.py
│   ├── test_integration.py
│   ├── test_resolver.py
│   ├── test_routes.py
│   ├── test_storage.py
│   └── test_txline_client.py
├── .env.example
├── .gitignore
├── Dockerfile
├── .dockerignore
├── requirements.txt
└── README.md
```
