"""Tests for the storage/data layer."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.storage.db import (
    Base,
    get_accuracy_stats,
    get_match_outcome,
    get_odds_window,
    get_pending_match_ids,
    get_pending_signals,
    get_recent_signals,
    mark_signal_resolved,
    save_match_outcome,
    save_odds_update,
    save_signal,
)
from app.storage.models import (
    MatchOutcome,
    OddsUpdate,
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


def _ts(minutes: int = 0) -> datetime:
    return datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def _make_signal(match_id="M1", market="1X2", selection="Home", status=SignalStatus.PENDING) -> Signal:
    return Signal(
        id=str(uuid.uuid4()),
        match_id=match_id,
        market=market,
        selection=selection,
        odds_value=2.0,
        confidence=70.0,
        direction=SignalDirection.SHORTENING,
        reason="test",
        status=status,
        created_at=_ts(0),
    )


# ---------------------------------------------------------------------------
# save_signal / read back
# ---------------------------------------------------------------------------


def test_insert_and_read_signal(db_session):
    signal = _make_signal(match_id="WC-001")
    save_signal(db_session, signal)
    from app.storage.db import SignalRow
    row = db_session.query(SignalRow).filter_by(id=signal.id).one()
    assert row.match_id == "WC-001"
    assert row.direction == "shortening"


def test_save_signal_none_created_at_uses_now(db_session):
    signal = _make_signal()
    signal.created_at = None
    save_signal(db_session, signal)
    from app.storage.db import SignalRow
    row = db_session.query(SignalRow).filter_by(id=signal.id).one()
    assert row.created_at is not None


# ---------------------------------------------------------------------------
# mark_signal_resolved
# ---------------------------------------------------------------------------


def test_mark_signal_resolved(db_session):
    signal = _make_signal()
    save_signal(db_session, signal)
    mark_signal_resolved(db_session, signal.id, SignalStatus.CORRECT)
    from app.storage.db import SignalRow
    row = db_session.query(SignalRow).filter_by(id=signal.id).one()
    assert row.status == "correct"
    assert row.resolved_at is not None


def test_mark_nonexistent_signal_raises(db_session):
    with pytest.raises(ValueError, match="not found"):
        mark_signal_resolved(db_session, "nonexistent-id", SignalStatus.CORRECT)


# ---------------------------------------------------------------------------
# save_odds_update / get_odds_window
# ---------------------------------------------------------------------------


def test_save_and_read_odds_window(db_session):
    for i in range(5):
        save_odds_update(db_session, OddsUpdate(
            match_id="M1", market="1X2", selection="Home",
            odds_value=2.0 + i * 0.01, timestamp=_ts(i),
        ))
    window = get_odds_window(db_session, "M1", "1X2", "Home")
    assert window == [2.0, 2.01, 2.02, 2.03, 2.04]


def test_get_odds_window_empty(db_session):
    assert get_odds_window(db_session, "M1", "1X2", "Home") == []


def test_get_odds_window_with_limit(db_session):
    for i in range(10):
        save_odds_update(db_session, OddsUpdate(
            match_id="M1", market="1X2", selection="Home",
            odds_value=float(i), timestamp=_ts(i),
        ))
    window = get_odds_window(db_session, "M1", "1X2", "Home", limit=3)
    assert window == [7.0, 8.0, 9.0]


def test_get_odds_window_isolation(db_session):
    save_odds_update(db_session, OddsUpdate(
        match_id="M1", market="1X2", selection="Home",
        odds_value=2.0, timestamp=_ts(0),
    ))
    save_odds_update(db_session, OddsUpdate(
        match_id="M2", market="1X2", selection="Home",
        odds_value=3.0, timestamp=_ts(0),
    ))
    assert get_odds_window(db_session, "M1", "1X2", "Home") == [2.0]
    assert get_odds_window(db_session, "M2", "1X2", "Home") == [3.0]


# ---------------------------------------------------------------------------
# get_recent_signals
# ---------------------------------------------------------------------------


def test_get_recent_signals_empty(db_session):
    assert get_recent_signals(db_session) == []


def test_get_recent_signals_newest_first(db_session):
    for i in range(3):
        s = _make_signal()
        s.created_at = _ts(i)
        save_signal(db_session, s)
    signals = get_recent_signals(db_session)
    assert len(signals) == 3
    assert signals[0].created_at >= signals[1].created_at


def test_get_recent_signals_with_status_filter(db_session):
    s1 = _make_signal(status=SignalStatus.PENDING)
    save_signal(db_session, s1)
    s2 = _make_signal(status=SignalStatus.CORRECT)
    save_signal(db_session, s2)
    pending = get_recent_signals(db_session, status="pending")
    assert len(pending) == 1
    assert pending[0].status == SignalStatus.PENDING


def test_get_recent_signals_with_limit(db_session):
    for i in range(5):
        save_signal(db_session, _make_signal())
    assert len(get_recent_signals(db_session, limit=2)) == 2


# ---------------------------------------------------------------------------
# get_pending_signals / get_pending_match_ids
# ---------------------------------------------------------------------------


def test_get_pending_signals(db_session):
    save_signal(db_session, _make_signal(match_id="M1", status=SignalStatus.PENDING))
    save_signal(db_session, _make_signal(match_id="M2", status=SignalStatus.CORRECT))
    pending = get_pending_signals(db_session)
    assert len(pending) == 1
    assert pending[0].match_id == "M1"


def test_get_pending_match_ids(db_session):
    save_signal(db_session, _make_signal(match_id="M1", status=SignalStatus.PENDING))
    save_signal(db_session, _make_signal(match_id="M1", status=SignalStatus.PENDING))
    save_signal(db_session, _make_signal(match_id="M2", status=SignalStatus.PENDING))
    save_signal(db_session, _make_signal(match_id="M3", status=SignalStatus.CORRECT))
    ids = get_pending_match_ids(db_session)
    assert sorted(ids) == ["M1", "M2"]


def test_get_pending_match_ids_empty(db_session):
    assert get_pending_match_ids(db_session) == []


# ---------------------------------------------------------------------------
# save_match_outcome / get_match_outcome
# ---------------------------------------------------------------------------


def test_save_and_get_match_outcome(db_session):
    outcome = MatchOutcome(match_id="M1", home_goals=2, away_goals=1, winner="home")
    save_match_outcome(db_session, outcome)
    result = get_match_outcome(db_session, "M1")
    assert result is not None
    assert result.home_goals == 2
    assert result.winner == "home"


def test_get_match_outcome_not_found(db_session):
    assert get_match_outcome(db_session, "nonexistent") is None


def test_save_match_outcome_upsert(db_session):
    save_match_outcome(db_session, MatchOutcome(match_id="M1", home_goals=1, away_goals=0, winner="home"))
    save_match_outcome(db_session, MatchOutcome(match_id="M1", home_goals=2, away_goals=1, winner="home"))
    result = get_match_outcome(db_session, "M1")
    assert result.home_goals == 2
    assert result.away_goals == 1


def test_save_match_outcome_none_recorded_at_uses_now(db_session):
    outcome = MatchOutcome(match_id="M1", home_goals=0, away_goals=0, winner="draw")
    outcome.recorded_at = None
    save_match_outcome(db_session, outcome)
    result = get_match_outcome(db_session, "M1")
    assert result.recorded_at is not None


# ---------------------------------------------------------------------------
# get_accuracy_stats edge cases
# ---------------------------------------------------------------------------


def test_accuracy_stats_no_resolved(db_session):
    save_signal(db_session, _make_signal(status=SignalStatus.PENDING))
    stats = get_accuracy_stats(db_session)
    assert stats.total_resolved == 0
    assert stats.overall_accuracy_pct == 0.0
    assert stats.by_market == []


def test_accuracy_stats_empty_db(db_session):
    stats = get_accuracy_stats(db_session)
    assert stats.total_resolved == 0
