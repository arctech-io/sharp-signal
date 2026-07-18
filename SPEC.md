# Sharp Signal — System Specification

## Overview

Sharp Signal monitors World Cup betting odds via the TxLINE API, detects statistically significant movements ("sharp signals"), tracks whether those signals correctly predicted match outcomes, and exposes everything through a REST API.

---

## 1. Ingestion

### Scenario 1.1 — Successful poll

```gherkin
Given   the TxLINE API is reachable and returns valid JSON
When    the poller runs on its scheduled interval
Then    it fetches current odds for all live and upcoming World Cup matches
And     normalizes every odds entry into the internal schema:
        match_id, market, selection, odds_value, timestamp
And     persists each normalized record to the database
And     logs the number of records fetched at INFO level
```

### Scenario 1.2 — API unreachable

```gherkin
Given   the TxLINE API is unreachable (timeout or DNS failure)
When    the poller runs
Then    it logs the failure at ERROR level with the exception details
And     schedules a retry with exponential backoff (1s, 2s, 4s, … capped at 60s)
And     does not raise an unhandled exception
And     does not corrupt or duplicate existing data in the database
```

### Scenario 1.3 — API returns an error status

```gherkin
Given   the TxLINE API returns an HTTP 4xx or 5xx status
When    the poller processes the response
Then    it logs the status code and response body at WARN level
And     retries per the backoff strategy
And     leaves previously ingested data untouched
```

### Scenario 1.4 — Empty response

```gherkin
Given   the TxLINE API returns an empty list (no live/upcoming matches)
When    the poller runs
Then    it logs that zero matches are available
And     completes without error
And     does not delete or invalidate any existing stored odds
```

---

## 2. Detection — What Counts as a Sharp Move

### Scenario 2.1 — Normal odds update, no signal

```gherkin
Given   a rolling window of the last N odds snapshots for a match/market/selection
And    the z-score cutoff is set to Z_THRESHOLD
And    the percentage-change cutoff is set to PCT_THRESHOLD
When    a new odds update arrives for that match/market/selection
And     the percentage change from the rolling average is below PCT_THRESHOLD
And     the z-score is below Z_THRESHOLD
Then    no signal is created
And     the new odds value is appended to the rolling window
```

### Scenario 2.2 — Sharp move detected (odds shortening)

```gherkin
Given   a rolling window of recent odds for match M, market "1X2", selection "Home"
When    a new odds update arrives and the price rises (TxLINE prices are probability × 1000, so a rise means the outcome is more likely)
And     the percentage change exceeds PCT_THRESHOLD
Or      the z-score exceeds Z_THRESHOLD
Then    a Signal record is created with:
          - confidence:  0–100 (derived from the magnitude of the triggering stat)
          - direction:   "shortening" (implies the selection is becoming more likely)
          - reason:      one sentence explaining which stat triggered the signal
          - status:      "pending"
And     the signal is persisted to the database
```

### Scenario 2.3 — Sharp move detected (odds drifting)

```gherkin
Given   a rolling window of recent odds for match M, market "Over/Under 2.5", selection "Over"
When    a new odds update arrives and the price falls (less likely)
And     the percentage change exceeds PCT_THRESHOLD
Or      the z-score exceeds Z_THRESHOLD
Then    a Signal record is created with:
          - confidence:  0–100
          - direction:   "drifting" (implies the selection is becoming less likely)
          - reason:      one sentence explaining which stat triggered the signal
          - status:      "pending"
And     the signal is persisted to the database
```

### Scenario 2.4 — Missing data or match not started

```gherkin
Given   a match has no odds data in the rolling window
Or      a match has not yet started (no kickoff timestamp in the past)
When    detection runs for that match
Then    the system skips that match silently
And     logs a DEBUG-level note that the match was skipped and why
And     does not raise an error
```

### Scenario 2.5 — Rolling window too small

```gherkin
Given   a match/market/selection has fewer than MIN_WINDOW_SIZE odds snapshots
When    detection runs
Then    it skips signal generation for that combination
And     logs that the window is insufficient for statistical comparison
```

---

## 3. Tracking Accuracy

### Scenario 3.1 — Signal resolved as correct

```gherkin
Given   a Signal with direction "shortening" for match M, selection "Home"
When    match M finishes and the actual outcome is a Home win
Then    the system marks the signal as "correct"
And     updates the signal record in the database
```

### Scenario 3.2 — Signal resolved as incorrect

```gherkin
Given   a Signal with direction "shortening" for match M, selection "Home"
When    match M finishes and the actual outcome is NOT a Home win
Then    the system marks the signal as "incorrect"
And     updates the signal record in the database
```

### Scenario 3.3 — Match outcome not available

```gherkin
Given   a Signal is logged for match M
When    match M finishes but TxLINE score data is unavailable or incomplete
Then    the signal remains in "pending" status
And     the system retries checking the outcome on subsequent poll cycles
```

### Scenario 3.4 — Accuracy reporting

```gherkin
Given   a set of resolved signals (correct or incorrect)
When    the system is asked for accuracy stats
Then    it returns:
          - overall accuracy: (correct / (correct + incorrect)) * 100
          - per-market breakdown: accuracy for each market type (1X2, Over/Under, Both Teams to Score, etc.)
And     accuracy is expressed as a percentage rounded to one decimal place
```

---

## 4. API / Dashboard

### Scenario 4.1 — List recent signals

```gherkin
Given   the system is running
When    a user sends GET /signals
Then    the response is a JSON array of the most recent signals, newest first
And     each signal includes: id, match_id, market, selection, odds_value, confidence, direction, reason, status, created_at
And     the response includes a total count
And     the endpoint supports query params: ?limit=N&status=pending|correct|incorrect
```

### Scenario 4.2 — View accuracy stats

```gherkin
Given   the system is running
When    a user sends GET /accuracy
Then    the response includes:
          - overall accuracy percentage
          - total signals analyzed
          - per-market accuracy breakdown
And     only resolved signals (correct + incorrect) are counted
```

### Scenario 4.3 — Health check

```gherkin
Given   the system is running
When    a user sends GET /health
Then    the response includes:
          - status: "ok"
          - uptime_seconds: time since application startup
          - last_successful_poll: ISO 8601 timestamp of the last successful TxLINE fetch
```

### Scenario 4.4 — Single signal detail

```gherkin
Given   the system is running and signals exist
When    a user sends GET /signals/{signal_id}
Then    the response is the full signal object with all fields
And     returns 404 if the signal does not exist
```

---

## 5. Non-Functional Requirements

```gherkin
Given   the application has started
When    no manual input is provided
Then    the ingestion+detection loop runs autonomously every POLL_INTERVAL_SECONDS
And     continues running until the process is terminated
And     handles transient errors without crashing

Given   the configuration file is loaded
When    the application reads threshold values
Then    z-score cutoff, percentage-change cutoff, and rolling window size
        are all read from environment variables or a config file
And     no threshold is hardcoded as a magic number in source code

Given   a signal has been detected
When    any consumer reads the signal record
Then    the "reason" field contains a single human-readable sentence
        explaining what statistical condition triggered the signal
And     the sentence is suitable for a demo video and judge review
```

---

## Configuration Reference

| Variable                  | Default  | Description                                  |
|---------------------------|----------|----------------------------------------------|
| `TXLINE_API_KEY`          | —        | API key for TxLINE                           |
| `TXLINE_BASE_URL`         | —        | Base URL for TxLINE API                      |
| `POLL_INTERVAL_SECONDS`   | `60`     | Seconds between ingestion runs               |
| `DATABASE_URL`            | `sqlite:///./sharp_signal.db` | Connection string for the DB     |
| `Z_SCORE_THRESHOLD`       | `2.0`    | Z-score above which a signal is triggered    |
| `PCT_CHANGE_THRESHOLD`    | `5.0`    | Percentage change above which a signal fires |
| `ROLLING_WINDOW_SIZE`     | `20`     | Number of recent odds snapshots to compare   |
| `MIN_WINDOW_SIZE`         | `5`      | Minimum snapshots before detection activates |
| `SIGNAL_COOLDOWN_SECONDS` | `600`    | Suppress duplicate same-direction signals within this window |
