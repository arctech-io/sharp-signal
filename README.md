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

_Edit this section with your actual experience of the TxLINE API._

### What worked well

- _[e.g. "Snapshot endpoints were fast and returned consistent data."]_
- _[e.g. "Auth via Bearer + X-Api-Token was straightforward."]_

### Where I hit friction

- _[e.g. "Odds values are int32 × 1000 — easy enough to normalise but not documented."]_
- _[e.g. "Scores snapshot returns multiple entries per match; had to infer the finalised one from action=game_finalised."]_
- _[e.g. "No rate-limit headers returned, so I added a defensive 0.5s delay between fixture calls."]_

### Suggested improvements

- _[e.g. "A single endpoint that returns odds + scores for all fixtures would reduce the N+1 request pattern."]_
- _[e.g. "Document the StatusId / period / Action fields that signal a finalised score."]_

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
