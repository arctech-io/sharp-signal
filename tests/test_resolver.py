"""Tests for the signal resolver."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.detection.resolver import (
    _determine_signal_correctness,
    _parse_match_outcome,
    _selection_won,
    resolve_all_pending,
    resolve_signals_for_match,
)
from app.ingestion.txline_client import TxLineError
from app.storage.db import Base, get_pending_signals, save_signal, get_accuracy_stats
from app.storage.models import (
    MatchOutcome,
    Signal,
    SignalDirection,
    SignalStatus,
)


@pytest.fixture()
def db_session(tmp_path):
    """Create an isolated SQLite DB for each test."""
    db_url = f"sqlite:///{tmp_path}/test.db"
    engine = create_engine(db_url, echo=False)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# _selection_won
# ---------------------------------------------------------------------------


class TestSelectionWon:
    def test_home_wins_1x2(self):
        outcome = MatchOutcome(match_id="M1", home_goals=2, away_goals=1, winner="home")
        assert _selection_won("home", "1x2", outcome) is True
        assert _selection_won("away", "1x2", outcome) is False
        assert _selection_won("draw", "1x2", outcome) is False

    def test_away_wins_1x2(self):
        outcome = MatchOutcome(match_id="M1", home_goals=0, away_goals=3, winner="away")
        assert _selection_won("away", "1x2", outcome) is True
        assert _selection_won("home", "1x2", outcome) is False

    def test_draw_1x2(self):
        outcome = MatchOutcome(match_id="M1", home_goals=1, away_goals=1, winner="draw")
        assert _selection_won("draw", "1x2", outcome) is True
        assert _selection_won("home", "1x2", outcome) is False

    def test_over_under(self):
        outcome = MatchOutcome(match_id="M1", home_goals=2, away_goals=2, winner="draw")
        assert _selection_won("over", "over/under 2.5", outcome) is True
        assert _selection_won("under", "over/under 2.5", outcome) is False

    def test_under_boundary(self):
        outcome = MatchOutcome(match_id="M1", home_goals=1, away_goals=1, winner="draw")
        assert _selection_won("under", "over/under 2.5", outcome) is True
        assert _selection_won("over", "over/under 2.5", outcome) is False

    def test_btts_yes(self):
        outcome = MatchOutcome(match_id="M1", home_goals=2, away_goals=1, winner="home")
        assert _selection_won("yes", "both teams to score", outcome) is True

    def test_btts_no(self):
        outcome = MatchOutcome(match_id="M1", home_goals=2, away_goals=0, winner="home")
        assert _selection_won("no", "both teams to score", outcome) is True


# ---------------------------------------------------------------------------
# _determine_signal_correctness
# ---------------------------------------------------------------------------


class TestDetermineSignalCorrectness:
    def test_shortening_correct(self):
        """Odds shortened on Home, Home won → correct."""
        signal = Signal(
            id="s1", match_id="M1", market="1X2", selection="Home",
            odds_value=1.8, confidence=70, direction=SignalDirection.SHORTENING,
            reason="test",
        )
        outcome = MatchOutcome(match_id="M1", home_goals=2, away_goals=0, winner="home")
        assert _determine_signal_correctness(signal, outcome) == SignalStatus.CORRECT

    def test_shortening_incorrect(self):
        """Odds shortened on Home, Away won → incorrect."""
        signal = Signal(
            id="s2", match_id="M1", market="1X2", selection="Home",
            odds_value=1.8, confidence=70, direction=SignalDirection.SHORTENING,
            reason="test",
        )
        outcome = MatchOutcome(match_id="M1", home_goals=0, away_goals=3, winner="away")
        assert _determine_signal_correctness(signal, outcome) == SignalStatus.INCORRECT

    def test_drifting_correct(self):
        """Odds drifted on Away, Home won (Away didn't win) → correct."""
        signal = Signal(
            id="s3", match_id="M1", market="1X2", selection="Away",
            odds_value=4.0, confidence=65, direction=SignalDirection.DRIFTING,
            reason="test",
        )
        outcome = MatchOutcome(match_id="M1", home_goals=2, away_goals=0, winner="home")
        assert _determine_signal_correctness(signal, outcome) == SignalStatus.CORRECT

    def test_drifting_incorrect(self):
        """Odds drifted on Away, Away won → incorrect."""
        signal = Signal(
            id="s4", match_id="M1", market="1X2", selection="Away",
            odds_value=4.0, confidence=65, direction=SignalDirection.DRIFTING,
            reason="test",
        )
        outcome = MatchOutcome(match_id="M1", home_goals=0, away_goals=2, winner="away")
        assert _determine_signal_correctness(signal, outcome) == SignalStatus.INCORRECT

    def test_over_under_shortening_correct(self):
        """Odds shortened on Over, total was 4 (>2.5) → correct."""
        signal = Signal(
            id="s5", match_id="M1", market="Over/Under 2.5", selection="Over",
            odds_value=1.7, confidence=80, direction=SignalDirection.SHORTENING,
            reason="test",
        )
        outcome = MatchOutcome(match_id="M1", home_goals=2, away_goals=2, winner="draw")
        assert _determine_signal_correctness(signal, outcome) == SignalStatus.CORRECT


# ---------------------------------------------------------------------------
# _parse_match_outcome
# ---------------------------------------------------------------------------


class TestParseMatchOutcome:
    def test_finalised_entry(self):
        data = [
            {"Action": "score_update", "Participant1Goals": 1, "Participant2Goals": 0},
            {"Action": "game_finalised", "StatusId": 100, "Period": 100,
             "Participant1Goals": 2, "Participant2Goals": 1},
        ]
        outcome = _parse_match_outcome("M1", data)
        assert outcome is not None
        assert outcome.home_goals == 2
        assert outcome.away_goals == 1
        assert outcome.winner == "home"

    def test_no_finalised_returns_none(self):
        data = [
            {"Action": "score_update", "Participant1Goals": 0, "Participant2Goals": 0},
            {"Action": "score_update", "Participant1Goals": 1, "Participant2Goals": 1},
        ]
        outcome = _parse_match_outcome("M1", data)
        assert outcome is None

    def test_empty_data(self):
        assert _parse_match_outcome("M1", []) is None

    def test_unknown_fields_with_finalised_marker(self):
        data = [{"Action": "game_finalised", "HomeGoals": 3, "AwayGoals": 2}]
        outcome = _parse_match_outcome("M1", data)
        assert outcome is not None
        assert outcome.home_goals == 3
        assert outcome.winner == "home"

    def test_unknown_fields_without_finalised_marker_returns_none(self):
        data = [{"HomeGoals": 3, "AwayGoals": 2}]
        assert _parse_match_outcome("M1", data) is None

    def test_non_numeric_goals_returns_none(self):
        data = [{"Participant1Goals": "abc", "Participant2Goals": 1}]
        assert _parse_match_outcome("M1", data) is None


class TestSelectionWonEdgeCases:
    def test_unknown_market_returns_false(self):
        outcome = MatchOutcome(match_id="M1", home_goals=2, away_goals=1, winner="home")
        assert _selection_won("home", "unknown_market", outcome) is False

    def test_btts_alias(self):
        outcome = MatchOutcome(match_id="M1", home_goals=1, away_goals=1, winner="draw")
        assert _selection_won("yes", "btts", outcome) is True
        assert _selection_won("no", "btts", outcome) is False


# ---------------------------------------------------------------------------
# Async resolver tests
# ---------------------------------------------------------------------------


def _make_pending_signal(match_id="M1", selection="Home", direction=SignalDirection.SHORTENING):
    return Signal(
        id=f"sig-{match_id}-{selection}",
        match_id=match_id,
        market="1X2",
        selection=selection,
        odds_value=2.0,
        confidence=70.0,
        direction=direction,
        reason="test signal",
        status=SignalStatus.PENDING,
        created_at=datetime.now(timezone.utc),
    )


def _mock_scores_response(*entries):
    """Build a mock httpx.Response for scores endpoint."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = list(entries)
    resp.raise_for_status = MagicMock()
    return resp


class TestResolveSignalsForMatch:
    @pytest.mark.asyncio
    async def test_no_pending_signals(self, db_session):
        client = AsyncMock()
        count = await resolve_signals_for_match(client, db_session, "M1")
        assert count == 0
        client.get_scores_snapshot.assert_not_called()

    @pytest.mark.asyncio
    async def test_txline_error_returns_zero(self, db_session):
        save_signal(db_session, _make_pending_signal())
        client = AsyncMock()
        client.get_scores_snapshot.side_effect = TxLineError("network down")
        count = await resolve_signals_for_match(client, db_session, "M1")
        assert count == 0

    @pytest.mark.asyncio
    async def test_empty_scores_returns_zero(self, db_session):
        save_signal(db_session, _make_pending_signal())
        client = AsyncMock()
        client.get_scores_snapshot.return_value = _mock_scores_response().json.return_value
        count = await resolve_signals_for_match(client, db_session, "M1")
        assert count == 0

    @pytest.mark.asyncio
    async def test_no_finalised_score_returns_zero(self, db_session):
        save_signal(db_session, _make_pending_signal())
        client = AsyncMock()
        client.get_scores_snapshot.return_value = _mock_scores_response(
            {"Action": "score_update", "Participant1Goals": 1, "Participant2Goals": 0}
        ).json.return_value
        count = await resolve_signals_for_match(client, db_session, "M1")
        # No finalised marker → match not finished → signals stay pending.
        assert count == 0

    @pytest.mark.asyncio
    async def test_happy_path_resolves_signals(self, db_session):
        sig = _make_pending_signal(selection="Home", direction=SignalDirection.SHORTENING)
        save_signal(db_session, sig)
        client = AsyncMock()
        client.get_scores_snapshot.return_value = _mock_scores_response(
            {"Action": "game_finalised", "Participant1Goals": 2, "Participant2Goals": 0}
        ).json.return_value
        count = await resolve_signals_for_match(client, db_session, "M1")
        assert count == 1
        pending = get_pending_signals(db_session)
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_per_signal_exception_does_not_kill_loop(self, db_session):
        sig1 = _make_pending_signal(match_id="M1", selection="Home")
        sig2 = _make_pending_signal(match_id="M1", selection="Away")
        save_signal(db_session, sig1)
        save_signal(db_session, sig2)
        client = AsyncMock()
        client.get_scores_snapshot.return_value = _mock_scores_response(
            {"Action": "game_finalised", "Participant1Goals": 2, "Participant2Goals": 0}
        ).json.return_value
        count = await resolve_signals_for_match(client, db_session, "M1")
        assert count >= 1


class TestResolveAllPending:
    @pytest.mark.asyncio
    async def test_no_pending_matches(self, db_session):
        client = AsyncMock()
        total = await resolve_all_pending(client, db_session)
        assert total == 0

    @pytest.mark.asyncio
    async def test_multiple_matches(self, db_session):
        save_signal(db_session, _make_pending_signal(match_id="M1"))
        save_signal(db_session, _make_pending_signal(match_id="M2"))
        client = AsyncMock()
        client.get_scores_snapshot.return_value = _mock_scores_response(
            {"Action": "game_finalised", "Participant1Goals": 1, "Participant2Goals": 0}
        ).json.return_value
        total = await resolve_all_pending(client, db_session)
        assert total == 2

    @pytest.mark.asyncio
    async def test_per_match_exception_continues(self, db_session):
        save_signal(db_session, _make_pending_signal(match_id="M1"))
        save_signal(db_session, _make_pending_signal(match_id="M2"))
        call_count = 0

        async def side_effect(fixture_id):
            nonlocal call_count
            call_count += 1
            if "M1" in str(fixture_id):
                raise TxLineError("fail for M1")
            return [
                {"Action": "game_finalised", "Participant1Goals": 1, "Participant2Goals": 0}
            ]

        client = AsyncMock()
        client.get_scores_snapshot = AsyncMock(side_effect=side_effect)
        total = await resolve_all_pending(client, db_session)
        assert total == 1


class TestExclusiveShortening:
    def test_only_strongest_shortening_kept_per_market(self):
        from app.main import _filter_exclusive_shortening
        from app.storage.models import Signal, SignalDirection

        signals = [
            Signal(match_id="M1", market="1X2_PARTICIPANT_RESULT", selection="part1",
                   odds_value=85.0, confidence=90.0, direction=SignalDirection.SHORTENING, reason="h"),
            Signal(match_id="M1", market="1X2_PARTICIPANT_RESULT", selection="draw",
                   odds_value=35.0, confidence=70.0, direction=SignalDirection.SHORTENING, reason="d"),
            Signal(match_id="M1", market="1X2_PARTICIPANT_RESULT", selection="part2",
                   odds_value=1.0, confidence=60.0, direction=SignalDirection.DRIFTING, reason="a"),
        ]
        kept = _filter_exclusive_shortening(signals)
        # Only one shortening (part1, the strongest) should survive; draw's
        # contradictory shortening is dropped; the drifting one stays.
        shortenings = [s for s in kept if s.direction == SignalDirection.SHORTENING]
        assert len(shortenings) == 1
        assert shortenings[0].selection == "part1"
        assert len(kept) == 2


class TestDemoSeed:
    def test_seed_resolves_pending_signals(self, db_session):
        from app.detection.resolver import seed_demo_outcomes
        # France (18257865) 0-4 England: a Home "shortening" should be INCORRECT.
        save_signal(db_session, _make_pending_signal(
            match_id="18257865", selection="part1",
            direction=SignalDirection.SHORTENING))
        resolved = seed_demo_outcomes(db_session)
        assert resolved == 1
        stats = get_accuracy_stats(db_session)
        assert stats.total_resolved == 1
        assert stats.incorrect == 1
        assert stats.correct == 0
