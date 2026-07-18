# Sharp Signal — Technical Document

## Core Idea

Sharp Signal is a real-time monitoring service that ingests World Cup betting odds from the TxLINE API, detects statistically significant price movements ("sharp signals") by combining z-score analysis with percent-change thresholds, tracks prediction correctness against final match outcomes, and exposes everything through a REST API and live dashboard.

## Architecture

The pipeline is a four-stage loop that runs every `POLL_INTERVAL_SECONDS` (default 60s):

```
TxLINE API ──► Ingestion ──► Detection ──► Storage ──► API / Dashboard
                   │                             │
                   ▼                             ▼
            rate-limited                signal + accuracy
            retry logic                 tracking (resolution)
```

### 1. Ingestion (`app/ingestion/txline_client.py`)

- Authenticates with a JWT bearer token + `X-Api-Token` header.
- `GET /api/fixtures/snapshot` → returns all live/upcoming fixtures.
- Fixtures are filtered client-side by competition (`TXLINE_COMPETITION_FILTER`, default `World Cup`) so only World Cup matches are ingested — the TxLINE World Cup feed also includes warm-up friendlies.
- For each fixture, `GET /api/odds/snapshot/{fixtureId}` → returns OddsPayload[].
- Raw payloads are normalised into flat `OddsUpdate` records (one per market/selection).
- Retry: up to 3 attempts with exponential backoff (1s → 2s → 4s, capped at 60s). Retries on network errors and 5xx; fails fast on 4xx.
- Rate-limit: 0.5s delay between successive fixture odds requests.

### 2. Detection (`app/detection/engine.py`)

- Maintains a per-(match, market, selection) rolling window of recent odds values.
- On each new odds update:
  - Computes the **z-score** of the new value against the window mean + std.
  - Computes the **absolute percent change** from the previous value.
  - If either exceeds its threshold, a `Signal` is emitted with:
    - Direction: `SHORTENING` (odds decreased → outcome became more likely) or `DRIFTING` (odds increased → outcome became less likely).
    - Confidence: 0–100 score derived from how far past the thresholds the move extended.
    - Reason: one-sentence human-readable explanation (e.g. _"Odds moved 12.3% in 8 minutes, 3.1 standard deviations from the recent average — sharp shortening."_).
- Pure math functions, no I/O, fully testable in isolation.

### 3. Tracking / Resolution (`app/detection/resolver.py`)

- After a match finishes, the resolver fetches `GET /api/scores/snapshot/{fixtureId}` from TxLINE.
- Parses the final score (looks for `action=game_finalised` or `statusId=100`).
- For each pending signal on that match, determines correctness:
  - **SHORTENING signal + selection won** → CORRECT.
  - **SHORTENING signal + selection lost** → INCORRECT.
  - **DRIFTING signal + selection lost** → CORRECT.
  - **DRIFTING signal + selection won** → INCORRECT.
- Updates accuracy stats: total resolved, correct count, incorrect count, accuracy percentage.

### 4. API (`app/api/routes.py`)

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Service health — uptime seconds, last successful poll |
| `GET /signals` | Recent signals, filterable by `status` and `limit` |
| `GET /signals/{id}` | Single signal detail |
| `GET /accuracy` | Aggregate accuracy stats |
| `GET /` | Live HTML dashboard (10s auto-refresh) |

## TxLINE Endpoints Used

| Endpoint | Method | Purpose | Rate Limit Consideration |
|----------|--------|---------|------------------------|
| `/api/fixtures/snapshot` | GET | Fetch all live/upcoming fixtures | Called once per poll cycle |
| `/api/odds/snapshot/{fixtureId}` | GET | Fetch odds snapshot for one fixture | Called per fixture, 0.5s delay between |
| `/api/scores/snapshot/{fixtureId}` | GET | Fetch match outcome for resolution | Called once per resolved match |

## Key Design Decisions

### Why z-score + percent change?

**Z-score** detects moves that are statistically unusual relative to the recent window. It catches sustained trends where the odds creep incrementally — each individual tick might be small, but the cumulative deviation becomes significant.

**Percent change** catches sudden, large jumps in a single interval that might not yet yield a high z-score (e.g., if the window is noisy or the first update after a fixture opens). Running both in parallel ("if either threshold is crossed") ensures we catch both gradual shifts and instant spikes.

This dual-threshold approach is common in financial anomaly detection (e.g., trading signal systems) and maps naturally to sports betting where odds can move either steadily or abruptly.

### Why SQLite?

- Zero-config: no separate database server to provision. The entire state lives in a single file (`sharp_signal.db`).
- Perfect for a demo/assessment where persistence across restarts matters but sharding, concurrent writes, and high availability don't.
- SQLAlchemy on top means swapping to PostgreSQL (for production) is a one-line `DATABASE_URL` change.

### Why this threshold approach?

All four thresholds (`Z_SCORE_THRESHOLD`, `PCT_CHANGE_THRESHOLD`, `ROLLING_WINDOW_SIZE`, `MIN_WINDOW_SIZE`) are exposed as environment variables with sensible defaults:

- **Z_SCORE_THRESHOLD = 2.0**: ~95th percentile in a normal distribution — a 1-in-20 event. High confidence, low noise.
- **PCT_CHANGE_THRESHOLD = 5.0**: Catches non-noise moves in liquid markets while ignoring micro-fluctuations.
- **ROLLING_WINDOW_SIZE = 20**: Smooths out short-term variance without being so large that the window becomes stale.
- **MIN_WINDOW_SIZE = 5**: Prevents spurious signals from tiny datasets (first 4 odds values are "warm-up").

Tuning is straightforward: lower thresholds for more signals (but more false positives), raise them for higher precision (but potentially miss moves).

## Project Structure

```
sharp-signal/
├── app/
│   ├── api/
│   │   ├── routes.py          # REST endpoints
│   │   └── dashboard.html     # Live HTML dashboard
│   ├── config.py              # Settings from env vars
│   ├── detection/
│   │   ├── engine.py          # Pure-math detection logic
│   │   └── resolver.py        # Signal resolution against outcomes
│   ├── ingestion/
│   │   └── txline_client.py   # TxLINE async client with retry
│   ├── storage/
│   │   ├── db.py              # SQLAlchemy schema + CRUD
│   │   └── models.py          # Pydantic data models
│   └── main.py                # FastAPI app + background loop
├── tests/
│   ├── test_detection.py      # 44 pure-math + engine tests
│   ├── test_resolver.py       # 28 resolver logic tests
│   ├── test_storage.py        # 19 CRUD tests
│   ├── test_txline_client.py  # 20 client + edge case tests
│   ├── test_routes.py         # 11 API endpoint tests
│   └── test_integration.py    # 7 full pipeline tests
├── Dockerfile
├── Procfile
├── .env.example
└── README.md
```

129 tests total, all passing.
