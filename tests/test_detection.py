"""Tests for the detection engine — pure math, no I/O."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.detection.engine import (
    DetectionConfig,
    DetectionEngine,
    RollingWindow,
    WindowEntry,
    build_reason,
    check_for_signal,
    compute_confidence,
    determine_direction,
    percent_change,
    rolling_mean,
    rolling_std,
    z_score,
)
from app.storage.models import OddsUpdate, SignalDirection


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _ts(minutes: int = 0) -> datetime:
    """Return a fixed base timestamp offset by *minutes*."""
    return datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# Pure math unit tests
# ---------------------------------------------------------------------------


class TestPercentChange:
    def test_basic_increase(self):
        assert percent_change(100.0, 110.0) == pytest.approx(10.0)

    def test_basic_decrease(self):
        assert percent_change(100.0, 90.0) == pytest.approx(10.0)

    def test_returns_absolute(self):
        assert percent_change(100.0, 80.0) == percent_change(100.0, 120.0)

    def test_zero_old(self):
        assert percent_change(0.0, 50.0) == 0.0

    def test_both_zero(self):
        assert percent_change(0.0, 0.0) == 0.0

    def test_small_change(self):
        assert percent_change(2.0, 2.04) == pytest.approx(2.0)


class TestRollingMean:
    def test_basic(self):
        assert rolling_mean([1.0, 2.0, 3.0]) == pytest.approx(2.0)

    def test_empty(self):
        assert rolling_mean([]) == 0.0

    def test_single(self):
        assert rolling_mean([5.0]) == 5.0


class TestRollingStd:
    def test_known_values(self):
        # [10, 12, 23, 23, 16, 23, 21, 16] — population std ≈ 4.899
        values = [10.0, 12.0, 23.0, 23.0, 16.0, 23.0, 21.0, 16.0]
        assert rolling_std(values) == pytest.approx(4.899, abs=0.01)

    def test_identical_values(self):
        assert rolling_std([5.0, 5.0, 5.0]) == 0.0

    def test_empty(self):
        assert rolling_std([]) == 0.0

    def test_single(self):
        assert rolling_std([3.0]) == 0.0


class TestZScore:
    def test_above_mean(self):
        assert z_score(15.0, 10.0, 2.5) == pytest.approx(2.0)

    def test_below_mean(self):
        assert z_score(5.0, 10.0, 2.5) == pytest.approx(-2.0)

    def test_zero_std(self):
        assert z_score(10.0, 10.0, 0.0) == 0.0


class TestDetermineDirection:
    def test_shortening(self):
        # TxLINE prices are probability×1000: a rise means more likely (shortening)
        assert determine_direction(2.5, 2.1) == SignalDirection.DRIFTING

    def test_drifting(self):
        assert determine_direction(2.5, 3.0) == SignalDirection.SHORTENING

    def test_unchanged(self):
        # Same value → not shortening, defaults to DRIFTING
        assert determine_direction(2.5, 2.5) == SignalDirection.DRIFTING


class TestComputeConfidence:
    def test_at_threshold(self):
        # Exactly at threshold → ~50
        score = compute_confidence(5.0, 2.0, 5.0, 2.0)
        assert score == pytest.approx(50.0)

    def test_well_above(self):
        # 2× threshold on pct → 50 + 2×25 = 100
        score = compute_confidence(15.0, 2.0, 5.0, 2.0)
        assert score == pytest.approx(100.0)

    def test_below_threshold(self):
        # Below threshold → clamped to 0 excess → 50
        score = compute_confidence(3.0, 1.0, 5.0, 2.0)
        assert score == pytest.approx(50.0)

    def test_z_drives_higher(self):
        # High z but low pct
        score = compute_confidence(2.0, 6.0, 5.0, 2.0)
        assert score > 75.0


class TestBuildReason:
    def test_shortening(self):
        r = build_reason(18.0, 2.4, SignalDirection.SHORTENING, 12)
        assert "18.0%" in r
        assert "2.4 standard deviations" in r
        assert "shortening" in r
        assert "12 observations" in r

    def test_drifting(self):
        r = build_reason(12.0, 3.0, SignalDirection.DRIFTING, 8)
        assert "drifting" in r
        assert "8 observations" in r

    def test_seconds(self):
        r = build_reason(10.0, 2.1, SignalDirection.SHORTENING, 3)
        assert "3 observations" in r


# ---------------------------------------------------------------------------
# RollingWindow tests
# ---------------------------------------------------------------------------


class TestRollingWindow:
    def test_push_and_len(self):
        w = RollingWindow(max_size=3)
        w.push(WindowEntry(1.0, _ts(0)))
        w.push(WindowEntry(2.0, _ts(1)))
        assert len(w) == 2
        w.push(WindowEntry(3.0, _ts(2)))
        assert len(w) == 3

    def test_evicts_oldest(self):
        w = RollingWindow(max_size=3)
        for i in range(5):
            w.push(WindowEntry(float(i), _ts(i)))
        assert len(w) == 3
        assert w.values == [2.0, 3.0, 4.0]

    def test_last_value(self):
        w = RollingWindow(max_size=5)
        assert w.last_value() is None
        w.push(WindowEntry(1.5, _ts(0)))
        assert w.last_value() == 1.5
        w.push(WindowEntry(2.5, _ts(1)))
        assert w.last_value() == 2.5

    def test_timestamps(self):
        w = RollingWindow(max_size=5)
        w.push(WindowEntry(1.0, _ts(0)))
        w.push(WindowEntry(2.0, _ts(5)))
        assert w.timestamps == [_ts(0), _ts(5)]


# ---------------------------------------------------------------------------
# check_for_signal — hand-crafted scenarios
# ---------------------------------------------------------------------------


class TestCheckForSignal:
    """Hand-crafted number sequences proving detection behaviour."""

    def _cfg(self, **overrides) -> DetectionConfig:
        defaults = dict(
            z_score_threshold=2.0,
            pct_change_threshold=5.0,
            rolling_window_size=20,
            min_window_size=5,
        )
        defaults.update(overrides)
        return DetectionConfig(**defaults)

    def _window(self, values: list[float], minutes_start: int = 0) -> RollingWindow:
        """Build a RollingWindow pre-filled with values spaced 1 minute apart."""
        w = RollingWindow(max_size=20)
        for i, v in enumerate(values):
            w.push(WindowEntry(odds_value=v, timestamp=_ts(minutes_start + i)))
        return w

    def test_big_jump_triggers_signal(self):
        """Price rise 2.00 → 2.50 (25%) = more likely → SHORTENING."""
        cfg = self._cfg(pct_change_threshold=5.0, z_score_threshold=2.0)
        # Window of stable odds at 2.00, then a big jump
        window = self._window([2.00, 2.01, 1.99, 2.00, 2.01, 2.00, 1.99, 2.00])
        signal = check_for_signal(
            window=window,
            new_odds=2.50,
            new_timestamp=_ts(8),
            match_id="M1",
            market="1X2",
            selection="Home",
            config=cfg,
        )
        assert signal is not None
        assert signal.direction == SignalDirection.SHORTENING
        assert signal.confidence > 50.0
        assert "25.0%" in signal.reason
        assert "sharp shortening" in signal.reason

    def test_big_drop_triggers_signal(self):
        """Price fall 3.00 → 2.40 (20%) = less likely → DRIFTING."""
        cfg = self._cfg(pct_change_threshold=5.0, z_score_threshold=2.0)
        window = self._window([3.00, 3.01, 2.99, 3.00, 3.01, 3.00])
        signal = check_for_signal(
            window=window,
            new_odds=2.40,
            new_timestamp=_ts(6),
            match_id="M2",
            market="Over/Under 2.5",
            selection="Over",
            config=cfg,
        )
        assert signal is not None
        assert signal.direction == SignalDirection.DRIFTING
        assert "20.0%" in signal.reason
        assert "drifting" in signal.reason

    def test_normal_noise_does_not_trigger(self):
        """Small random fluctuations should NOT fire."""
        cfg = self._cfg(pct_change_threshold=5.0, z_score_threshold=2.0)
        # Wider spread keeps z-score low even with small moves
        window = self._window([2.00, 2.08, 1.92, 2.05, 1.95, 2.03, 1.97])
        signal = check_for_signal(
            window=window,
            new_odds=2.02,
            new_timestamp=_ts(7),
            match_id="M3",
            market="1X2",
            selection="Draw",
            config=cfg,
        )
        assert signal is None

    def test_zscore_triggers_even_if_pct_below_threshold(self):
        """High z-score alone can trigger even if pct is borderline."""
        cfg = self._cfg(pct_change_threshold=10.0, z_score_threshold=1.5)
        # Tight cluster, then a value 2 std above
        window = self._window([1.50, 1.51, 1.49, 1.50, 1.51, 1.50, 1.49, 1.50])
        signal = check_for_signal(
            window=window,
            new_odds=1.70,
            new_timestamp=_ts(8),
            match_id="M4",
            market="1X2",
            selection="Home",
            config=cfg,
        )
        assert signal is not None

    def test_pct_triggers_even_if_zscore_below_threshold(self):
        """High pct change alone can trigger even if z-score is borderline."""
        cfg = self._cfg(pct_change_threshold=5.0, z_score_threshold=5.0)
        # Noisy window with high std, so z-score stays low
        window = self._window([1.0, 3.0, 1.0, 3.0, 1.0, 3.0, 1.0, 3.0])
        signal = check_for_signal(
            window=window,
            new_odds=3.50,
            new_timestamp=_ts(8),
            match_id="M5",
            market="1X2",
            selection="Away",
            config=cfg,
        )
        assert signal is not None

    def test_below_min_window_no_signal(self):
        """Window too small → no signal, even with a huge jump."""
        cfg = self._cfg(min_window_size=5)
        window = self._window([1.0, 1.1])  # only 2 entries
        signal = check_for_signal(
            window=window,
            new_odds=5.0,
            new_timestamp=_ts(2),
            match_id="M6",
            market="1X2",
            selection="Home",
            config=cfg,
        )
        assert signal is None

    def test_reason_string_matches_numbers(self):
        """The reason string accurately reflects the computed stats."""
        cfg = self._cfg(pct_change_threshold=5.0, z_score_threshold=2.0)
        window = self._window([2.00, 2.01, 1.99, 2.00, 2.01, 2.00])
        signal = check_for_signal(
            window=window,
            new_odds=2.20,
            new_timestamp=_ts(6),
            match_id="M7",
            market="1X2",
            selection="Home",
            config=cfg,
        )
        assert signal is not None
        # 10% move
        assert "10.0%" in signal.reason
        # Direction: price up = more likely = shortening
        assert "shortening" in signal.reason

    def test_confidence_scales_with_magnitude(self):
        """A bigger move should produce higher confidence."""
        cfg = self._cfg(pct_change_threshold=5.0, z_score_threshold=2.0)
        window_small = self._window([2.00] * 8)
        window_big = self._window([2.00] * 8)

        sig_small = check_for_signal(
            window=window_small, new_odds=2.10, new_timestamp=_ts(8),
            match_id="M", market="1X2", selection="Home", config=cfg,
        )
        sig_big = check_for_signal(
            window=window_big, new_odds=2.50, new_timestamp=_ts(8),
            match_id="M", market="1X2", selection="Home", config=cfg,
        )
        assert sig_small is not None and sig_big is not None
        assert sig_big.confidence > sig_small.confidence


# ---------------------------------------------------------------------------
# DetectionEngine (stateful wrapper) tests
# ---------------------------------------------------------------------------


class TestDetectionEngine:
    def test_returns_none_for_noise(self):
        engine = DetectionEngine(DetectionConfig(
            pct_change_threshold=5.0, z_score_threshold=2.0, min_window_size=3,
        ))
        for i in range(10):
            update = OddsUpdate(
                match_id="M1", market="1X2", selection="Home",
                odds_value=2.00 + (i % 2) * 0.01,
                timestamp=_ts(i),
            )
            assert engine.process(update) is None

    def test_fires_on_big_move(self):
        engine = DetectionEngine(DetectionConfig(
            pct_change_threshold=5.0, z_score_threshold=2.0, min_window_size=3,
        ))
        # Build stable window
        for i in range(5):
            engine.process(OddsUpdate(
                match_id="M1", market="1X2", selection="Home",
                odds_value=2.00, timestamp=_ts(i),
            ))
        # Big jump (price up = more likely = shortening)
        signal = engine.process(OddsUpdate(
            match_id="M1", market="1X2", selection="Home",
            odds_value=2.50, timestamp=_ts(5),
        ))
        assert signal is not None
        assert signal.direction == SignalDirection.SHORTENING

    def test_separate_windows_per_selection(self):
        engine = DetectionEngine(DetectionConfig(
            pct_change_threshold=5.0, z_score_threshold=2.0, min_window_size=3,
        ))
        # Home selection stable
        for i in range(5):
            engine.process(OddsUpdate(
                match_id="M1", market="1X2", selection="Home",
                odds_value=2.00, timestamp=_ts(i),
            ))
        # Away selection — different window, also stable
        for i in range(5):
            engine.process(OddsUpdate(
                match_id="M1", market="1X2", selection="Away",
                odds_value=3.50, timestamp=_ts(i),
            ))
        # Now spike Home
        signal = engine.process(OddsUpdate(
            match_id="M1", market="1X2", selection="Home",
            odds_value=2.50, timestamp=_ts(5),
        ))
        assert signal is not None
        # Away should still be fine
        signal_away = engine.process(OddsUpdate(
            match_id="M1", market="1X2", selection="Away",
            odds_value=3.51, timestamp=_ts(5),
        ))
        assert signal_away is None

    def test_multi_match_interleaved(self):
        """Updates from different matches interleaved — each gets its own window."""
        engine = DetectionEngine(DetectionConfig(
            pct_change_threshold=5.0, z_score_threshold=2.0, min_window_size=3,
        ))
        # Interleave M1 and M2
        for i in range(5):
            engine.process(OddsUpdate(
                match_id="M1", market="1X2", selection="Home",
                odds_value=2.00, timestamp=_ts(i),
            ))
            engine.process(OddsUpdate(
                match_id="M2", market="1X2", selection="Home",
                odds_value=3.00, timestamp=_ts(i),
            ))
        # Spike M1 only
        signal_m1 = engine.process(OddsUpdate(
            match_id="M1", market="1X2", selection="Home",
            odds_value=2.50, timestamp=_ts(5),
        ))
        assert signal_m1 is not None
        # M2 should be unaffected
        signal_m2 = engine.process(OddsUpdate(
            match_id="M2", market="1X2", selection="Home",
            odds_value=3.01, timestamp=_ts(5),
        ))
        assert signal_m2 is None

    def test_full_window_rotation(self):
        """After 20+ updates, old values are evicted and detection still works."""
        engine = DetectionEngine(DetectionConfig(
            pct_change_threshold=5.0, z_score_threshold=2.0,
            rolling_window_size=10, min_window_size=5,
        ))
        # Fill window with 15 stable values
        for i in range(15):
            engine.process(OddsUpdate(
                match_id="M1", market="1X2", selection="Home",
                odds_value=2.00, timestamp=_ts(i),
            ))
        # Now spike — should trigger because window only holds last 10
        signal = engine.process(OddsUpdate(
            match_id="M1", market="1X2", selection="Home",
            odds_value=2.50, timestamp=_ts(15),
        ))
        assert signal is not None

    def test_all_same_values_std_zero(self):
        """All identical odds → std=0, z_score=0, only pct triggers."""
        engine = DetectionEngine(DetectionConfig(
            pct_change_threshold=5.0, z_score_threshold=2.0, min_window_size=3,
        ))
        for i in range(6):
            engine.process(OddsUpdate(
                match_id="M1", market="1X2", selection="Home",
                odds_value=2.00, timestamp=_ts(i),
            ))
        # 6% move — pct triggers, z-score stays 0
        signal = engine.process(OddsUpdate(
            match_id="M1", market="1X2", selection="Home",
            odds_value=2.12, timestamp=_ts(6),
        ))
        assert signal is not None
        assert "6.0%" in signal.reason

    def test_first_update_never_triggers(self):
        """The very first update to a window never has a previous value to compare."""
        engine = DetectionEngine(DetectionConfig(
            pct_change_threshold=1.0, z_score_threshold=0.1, min_window_size=1,
        ))
        signal = engine.process(OddsUpdate(
            match_id="M1", market="1X2", selection="Home",
            odds_value=100.0, timestamp=_ts(0),
        ))
        assert signal is None

    def test_signal_cooldown_suppresses_duplicates(self):
        """Repeated same-direction moves within the cooldown window emit once."""
        engine = DetectionEngine(DetectionConfig(
            pct_change_threshold=5.0, z_score_threshold=2.0, min_window_size=3,
            signal_cooldown_seconds=600,
        ))
        for i in range(5):
            engine.process(OddsUpdate(
                match_id="M1", market="1X2", selection="Home",
                odds_value=2.00, timestamp=_ts(i),
            ))
        first = engine.process(OddsUpdate(
            match_id="M1", market="1X2", selection="Home",
            odds_value=2.50, timestamp=_ts(5),
        ))
        assert first is not None
        # Same direction (still rising against baseline) shortly after → suppressed
        second = engine.process(OddsUpdate(
            match_id="M1", market="1X2", selection="Home",
            odds_value=2.60, timestamp=_ts(6),
        ))
        assert second is None

    def test_signal_cooldown_allows_opposite_direction(self):
        """A direction flip is a new signal even within the cooldown."""
        engine = DetectionEngine(DetectionConfig(
            pct_change_threshold=5.0, z_score_threshold=2.0, min_window_size=3,
            signal_cooldown_seconds=600,
        ))
        for i in range(5):
            engine.process(OddsUpdate(
                match_id="M1", market="1X2", selection="Home",
                odds_value=2.00, timestamp=_ts(i),
            ))
        drift = engine.process(OddsUpdate(
            match_id="M1", market="1X2", selection="Home",
            odds_value=2.50, timestamp=_ts(5),
        ))
        assert drift is not None and drift.direction == SignalDirection.SHORTENING
        # Price falls back → drifting is a new direction → should fire
        shorten = engine.process(OddsUpdate(
            match_id="M1", market="1X2", selection="Home",
            odds_value=1.95, timestamp=_ts(6),
        ))
        assert shorten is not None and shorten.direction == SignalDirection.DRIFTING
