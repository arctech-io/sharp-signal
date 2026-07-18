"""Detection engine — pure math that evaluates odds windows for sharp moves.

All functions are pure / side-effect-free.  They take numbers in and return
a Signal or None.  No I/O, no database access, no logging.
"""

from __future__ import annotations

import math
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.storage.models import OddsUpdate, Signal, SignalDirection


# ---------------------------------------------------------------------------
# Configuration dataclass — passed explicitly, never imported from config.py
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DetectionConfig:
    """Thresholds and window settings for the detection engine."""

    z_score_threshold: float = 2.0
    pct_change_threshold: float = 5.0
    rolling_window_size: int = 20
    min_window_size: int = 5
    signal_cooldown_seconds: int = 600


# ---------------------------------------------------------------------------
# Pure math helpers
# ---------------------------------------------------------------------------


def percent_change(old: float, new: float) -> float:
    """Compute the absolute percentage change from *old* to *new*.

    Returns 0.0 when old is zero (avoids division-by-zero).
    """
    if old == 0.0:
        return 0.0
    return abs((new - old) / old) * 100.0


def rolling_mean(values: list[float]) -> float:
    """Arithmetic mean of a list of floats.  Returns 0.0 for empty lists."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def rolling_std(values: list[float]) -> float:
    """Population standard deviation.  Returns 0.0 for empty or single-element."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(variance)


def z_score(value: float, mean: float, std: float) -> float:
    """Z-score of *value* given *mean* and *std*.

    When the window is perfectly flat (std == 0) a deviation from the mean
    is, by definition, an extreme outlier. Rather than returning 0.0 — which
    would wrongly suppress detection — we report a large finite z-score
    proportional to the relative deviation, so a jump off a stable baseline
    is flagged as highly unusual.
    """
    if std == 0.0:
        if value == mean:
            return 0.0
        # Flat baseline + any deviation → extreme outlier.
        denom = abs(mean) if mean != 0.0 else 1.0
        return abs((value - mean) / denom) * 10.0
    return (value - mean) / std


def determine_direction(old: float, new: float) -> SignalDirection:
    """Return SHORTENING if the outcome became MORE likely, DRIFTING if less.

    TxLINE prices are probability × 1000 (higher = more likely), so a rise in
    price means the selection shortened (became more likely).
    """
    if new > old:
        return SignalDirection.SHORTENING
    return SignalDirection.DRIFTING


def compute_confidence(
    pct: float,
    z: float,
    pct_threshold: float,
    z_threshold: float,
) -> float:
    """Derive a 0–100 confidence score from how far past the thresholds the move is.

    The score is the maximum of the two normalised excesses, scaled to 0–100.
    A value right at the threshold yields ~50; well above yields closer to 100.
    """
    pct_excess = max(0.0, pct - pct_threshold) / pct_threshold if pct_threshold else 0.0
    z_excess = max(0.0, z - z_threshold) / z_threshold if z_threshold else 0.0
    raw = min(2.0, max(pct_excess, z_excess))
    # Map: 0× excess → 50, 1× excess → 75, 2× excess → 100 (capped).
    score = 50.0 + raw * 25.0
    return min(100.0, max(0.0, score))


def build_reason(
    pct: float,
    z: float,
    direction: SignalDirection,
    window_points: int,
) -> str:
    """Build a one-sentence human-readable explanation of the signal.

    Args:
        pct: Absolute percentage change.
        z: Z-score vs the rolling window.
        direction: Whether odds shortened or drifted.
        window_points: Number of recent odds observations in the window.
    """
    direction_word = "shortening" if direction == SignalDirection.SHORTENING else "drifting"
    return (
        f"Odds moved {pct:.1f}% over {window_points} observations, "
        f"{z:.1f} standard deviations from the recent average — "
        f"sharp {direction_word}."
    )


# ---------------------------------------------------------------------------
# Rolling window state
# ---------------------------------------------------------------------------


@dataclass
class WindowEntry:
    """A single odds snapshot inside the rolling window."""

    odds_value: float
    timestamp: datetime


@dataclass
class RollingWindow:
    """Fixed-size deque of odds snapshots for one (match, market, selection)."""

    max_size: int = 20
    entries: deque[WindowEntry] = field(default_factory=deque)

    def push(self, entry: WindowEntry) -> None:
        """Append a new entry, evicting the oldest if at capacity."""
        self.entries.append(entry)
        while len(self.entries) > self.max_size:
            self.entries.popleft()

    @property
    def values(self) -> list[float]:
        return [e.odds_value for e in self.entries]

    @property
    def timestamps(self) -> list[datetime]:
        return [e.timestamp for e in self.entries]

    def __len__(self) -> int:
        return len(self.entries)

    def last_value(self) -> float | None:
        """Return the most recent odds value, or None if empty."""
        if not self.entries:
            return None
        return self.entries[-1].odds_value


# ---------------------------------------------------------------------------
# Core detection function (pure)
# ---------------------------------------------------------------------------


def check_for_signal(
    window: RollingWindow,
    new_odds: float,
    new_timestamp: datetime,
    match_id: str,
    market: str,
    selection: str,
    config: DetectionConfig,
) -> Signal | None:
    """Evaluate one new odds value against the rolling window.

    Returns a Signal if a sharp move is detected, None otherwise.

    This function is pure: it reads the window, computes stats, and returns
    a result.  It mutates nothing (the caller is responsible for pushing
    the new entry into the window after this call).
    """
    values = window.values
    n = len(values)

    # Not enough history to compare
    if n < config.min_window_size:
        return None

    # Previous value is the one before the new arrival
    prev = values[-1]
    mean = rolling_mean(values)
    std = rolling_std(values)

    pct = percent_change(prev, new_odds)
    z = abs(z_score(new_odds, mean, std))

    pct_crossed = pct >= config.pct_change_threshold
    z_crossed = z >= config.z_score_threshold

    if not (pct_crossed or z_crossed):
        return None

    direction = determine_direction(prev, new_odds)
    confidence = compute_confidence(pct, z, config.pct_change_threshold, config.z_score_threshold)

    reason = build_reason(pct, z, direction, n)

    return Signal(
        id=str(uuid.uuid4()),
        match_id=match_id,
        market=market,
        selection=selection,
        odds_value=new_odds,
        confidence=round(confidence, 1),
        direction=direction,
        reason=reason,
        created_at=new_timestamp,
    )


# ---------------------------------------------------------------------------
# Stateful wrapper — manages windows per (match, market, selection)
# ---------------------------------------------------------------------------


class DetectionEngine:
    """Stateful wrapper that tracks rolling windows and runs detection.

    Usage::

        engine = DetectionEngine(config)
        signal = engine.process(odds_update)
        if signal:
            # persist signal ...
    """

    def __init__(self, config: DetectionConfig | None = None) -> None:
        self.config = config or DetectionConfig()
        self._windows: dict[tuple[str, str, str], RollingWindow] = {}
        # (match, market, selection) -> (direction, last_signal_timestamp)
        self._last_signal: dict[tuple[str, str, str], tuple[str, datetime]] = {}

    def _key(self, match_id: str, market: str, selection: str) -> tuple[str, str, str]:
        return (match_id, market, selection)

    def _get_window(self, key: tuple[str, str, str]) -> RollingWindow:
        if key not in self._windows:
            self._windows[key] = RollingWindow(max_size=self.config.rolling_window_size)
        return self._windows[key]

    def process(self, update: OddsUpdate) -> Signal | None:
        """Process one OddsUpdate and return a Signal if a sharp move is detected.

        The window is mutated (the new entry is pushed) regardless of whether
        a signal fires. To avoid flooding the dashboard, a repeated signal for
        the same (match, market, selection, direction) is suppressed until
        ``signal_cooldown_seconds`` has elapsed since the last one.
        """
        key = self._key(update.match_id, update.market, update.selection)
        window = self._get_window(key)

        signal = check_for_signal(
            window=window,
            new_odds=update.odds_value,
            new_timestamp=update.timestamp,
            match_id=update.match_id,
            market=update.market,
            selection=update.selection,
            config=self.config,
        )

        # Always push the new value into the window
        window.push(WindowEntry(odds_value=update.odds_value, timestamp=update.timestamp))

        if signal is None:
            return None

        # Cooldown: skip duplicate-direction signals for the same key.
        prev = self._last_signal.get(key)
        if prev is not None:
            prev_dir, prev_ts = prev
            elapsed = (signal.created_at - prev_ts).total_seconds()
            if prev_dir == signal.direction.value and elapsed < self.config.signal_cooldown_seconds:
                return None

        self._last_signal[key] = (signal.direction.value, signal.created_at)
        return signal
