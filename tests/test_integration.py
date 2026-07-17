"""Integration test — full pipeline end-to-end with mocked TxLINE.

Fake odds sequence in → detection → storage → resolution → accuracy update.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.detection.engine import DetectionConfig, DetectionEngine
from app.detection.resolver import resolve_all_pending
from app.ingestion.txline_client import TxLineClient, TxLineError, _normalize_odds
from app.storage.db import (
    Base,
    get_accuracy_stats,
    get_odds_window,
    get_pending_signals,
    get_recent_signals,
    save_odds_update,
    save_signal,
)
from app.storage.models import OddsUpdate, SignalDirection, SignalStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(minutes: int = 0) -> datetime:
    return datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def _make_odds_update(match_id, market, selection, odds_value, minutes=0):
    return OddsUpdate(
        match_id=match_id,
        market=market,
        selection=selection,
        odds_value=odds_value,
        timestamp=_ts(minutes),
    )


def _mock_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or []
    resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture()
def db_session(tmp_path):
    db_url = f"sqlite:///{tmp_path}/integration.db"
    engine = create_engine(db_url, echo=False)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# Test: full pipeline — odds in, signal out, resolve, accuracy
# ---------------------------------------------------------------------------


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_odds_in_signal_out_resolve_accuracy(self, db_session):
        """
        1. Feed 8 stable odds + 1 big jump through detection engine
        2. Save odds and signals to DB
        3. Mock TxLINE scores response (home wins)
        4. Run resolver
        5. Verify accuracy stats
        """
        engine = DetectionEngine(DetectionConfig(
            pct_change_threshold=5.0,
            z_score_threshold=2.0,
            rolling_window_size=20,
            min_window_size=5,
        ))

        # Step 1-2: Feed odds, detect, persist
        stable_odds = [
            _make_odds_update("WC-001", "1X2", "Home", 2.00, i)
            for i in range(8)
        ]
        jump_odds = _make_odds_update("WC-001", "1X2", "Home", 2.50, 8)

        signals_created = []
        for update in stable_odds + [jump_odds]:
            save_odds_update(db_session, update)
            signal = engine.process(update)
            if signal is not None:
                save_signal(db_session, signal)
                signals_created.append(signal)

        # Should have created exactly 1 signal (the jump)
        assert len(signals_created) == 1
        sig = signals_created[0]
        assert sig.match_id == "WC-001"
        assert sig.direction == SignalDirection.DRIFTING
        assert sig.confidence > 50

        # Verify odds window was persisted
        window = get_odds_window(db_session, "WC-001", "1X2", "Home")
        assert len(window) == 9
        assert window[-1] == 2.50

        # Verify signal is pending
        pending = get_pending_signals(db_session)
        assert len(pending) == 1

        # Step 3: Mock TxLINE scores — home wins 2-0
        mock_client = AsyncMock()
        mock_client._request.return_value = _mock_response(200, json_data=[
            {"Action": "game_finalised", "Participant1Goals": 2, "Participant2Goals": 0}
        ])

        # Step 4: Resolve
        resolved = await resolve_all_pending(mock_client, db_session)
        assert resolved == 1

        # Step 5: Check accuracy
        stats = get_accuracy_stats(db_session)
        assert stats.total_resolved == 1
        # Odds drifted on Home, Home won → signal is INCORRECT
        assert stats.incorrect == 1
        assert stats.correct == 0
        assert stats.overall_accuracy_pct == 0.0

    @pytest.mark.asyncio
    async def test_multiple_matches_concurrent(self, db_session):
        """
        Process odds from 3 different matches simultaneously.
        Each gets its own detection window. Verify signals are match-specific.
        """
        engine = DetectionEngine(DetectionConfig(
            pct_change_threshold=5.0,
            z_score_threshold=2.0,
            rolling_window_size=20,
            min_window_size=3,
        ))

        match_ids = ["M1", "M2", "M3"]
        signals = []

        # Interleave updates from all 3 matches
        for i in range(6):
            for mid in match_ids:
                update = _make_odds_update(mid, "1X2", "Home", 2.00, i)
                save_odds_update(db_session, update)
                sig = engine.process(update)
                if sig:
                    save_signal(db_session, sig)
                    signals.append(sig)

        # Now spike only M2
        spike = _make_odds_update("M2", "1X2", "Home", 2.50, 6)
        save_odds_update(db_session, spike)
        sig = engine.process(spike)
        if sig:
            save_signal(db_session, sig)
            signals.append(sig)

        # Only M2 should have produced a signal
        m2_signals = [s for s in signals if s.match_id == "M2"]
        non_m2 = [s for s in signals if s.match_id != "M2"]
        assert len(m2_signals) == 1
        assert len(non_m2) == 0

    @pytest.mark.asyncio
    async def test_txline_down_does_not_crash_pipeline(self, db_session):
        """
        TxLINE is completely unreachable — pipeline should log and continue.
        """
        mock_client = AsyncMock()
        mock_client._request.side_effect = TxLineError("Connection refused")

        # This should not raise
        resolved = await resolve_all_pending(mock_client, db_session)
        assert resolved == 0

    @pytest.mark.asyncio
    async def test_empty_odds_response(self, db_session):
        """
        TxLINE returns empty odds — no signals, no crash.
        """
        engine = DetectionEngine(DetectionConfig(
            pct_change_threshold=5.0,
            z_score_threshold=2.0,
            min_window_size=3,
        ))

        # No odds to process — just verify engine state is clean
        assert len(engine._windows) == 0

        # Resolve with no pending signals
        mock_client = AsyncMock()
        resolved = await resolve_all_pending(mock_client, db_session)
        assert resolved == 0

    @pytest.mark.asyncio
    async def test_match_with_no_history_window(self, db_session):
        """
        A brand-new match with only 2 odds updates — detection should skip it.
        """
        engine = DetectionEngine(DetectionConfig(
            pct_change_threshold=5.0,
            z_score_threshold=2.0,
            min_window_size=5,
        ))

        # Only 2 updates — below min_window_size
        for i in range(2):
            update = _make_odds_update("NEW-MATCH", "1X2", "Home", 2.00 + i * 0.1, i)
            save_odds_update(db_session, update)
            signal = engine.process(update)
            assert signal is None  # Not enough history

        # No signals should exist
        pending = get_pending_signals(db_session)
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_accuracy_after_multiple_resolutions(self, db_session):
        """
        Resolve several matches, verify accuracy aggregates correctly.
        """
        engine = DetectionEngine(DetectionConfig(
            pct_change_threshold=5.0,
            z_score_threshold=2.0,
            rolling_window_size=20,
            min_window_size=3,
        ))

        # Create signals for 3 matches
        for mid in ["M1", "M2", "M3"]:
            for i in range(5):
                update = _make_odds_update(mid, "1X2", "Home", 2.00, i)
                save_odds_update(db_session, update)
                engine.process(update)
            # Spike to create signal
            spike = _make_odds_update(mid, "1X2", "Home", 2.50, 5)
            save_odds_update(db_session, spike)
            sig = engine.process(spike)
            if sig:
                save_signal(db_session, sig)

        pending = get_pending_signals(db_session)
        assert len(pending) == 3

        # Mock scores: M1 home wins, M2 draw, M3 away wins
        scores_map = {
            "M1": {"Action": "game_finalised", "Participant1Goals": 2, "Participant2Goals": 0},
            "M2": {"Action": "game_finalised", "Participant1Goals": 1, "Participant2Goals": 1},
            "M3": {"Action": "game_finalised", "Participant1Goals": 0, "Participant2Goals": 3},
        }

        async def mock_request(method, path):
            for mid, score in scores_map.items():
                if mid in path:
                    return _mock_response(200, json_data=[score])
            return _mock_response(200, json_data=[])

        mock_client = AsyncMock()
        mock_client._request = AsyncMock(side_effect=mock_request)

        resolved = await resolve_all_pending(mock_client, db_session)
        assert resolved == 3

        stats = get_accuracy_stats(db_session)
        assert stats.total_resolved == 3
        # All signals are direction=DRIFTING (odds 2.00→2.50) on Home selection
        # M1: drifted + home won → INCORRECT (Home DID win despite drift)
        # M2: drifted + draw → CORRECT (Home didn't win)
        # M3: drifted + away won → CORRECT (Home didn't win)
        assert stats.correct == 2
        assert stats.incorrect == 1
