"""SQLAlchemy table definitions and CRUD operations."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Integer, String, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings
from app.storage.models import (
    AccuracyStats,
    MarketAccuracy,
    MatchOutcome,
    Signal,
    SignalDirection,
    SignalStatus,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""


class OddsHistoryRow(Base):
    """Raw odds snapshot stored for rolling-window calculations."""

    __tablename__ = "odds_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(String, nullable=False, index=True)
    market = Column(String, nullable=False)
    selection = Column(String, nullable=False)
    odds_value = Column(Float, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class SignalRow(Base):
    """A detected sharp movement."""

    __tablename__ = "signals"

    id = Column(String, primary_key=True)
    match_id = Column(String, nullable=False, index=True)
    market = Column(String, nullable=False)
    selection = Column(String, nullable=False)
    odds_value = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    direction = Column(String, nullable=False)  # "shortening" | "drifting"
    reason = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")
    created_at = Column(DateTime, nullable=False)
    resolved_at = Column(DateTime, nullable=True)


class MatchOutcomeRow(Base):
    """Actual match result for accuracy tracking."""

    __tablename__ = "match_outcomes"

    match_id = Column(String, primary_key=True)
    home_goals = Column(Integer, nullable=False)
    away_goals = Column(Integer, nullable=False)
    winner = Column(String, nullable=False)  # "home" | "away" | "draw"
    recorded_at = Column(DateTime, nullable=False)


# ---------------------------------------------------------------------------
# Engine / session helpers
# ---------------------------------------------------------------------------

engine = create_engine(settings.database_url, echo=False)
SessionLocal = sessionmaker(bind=engine)


def get_db():
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(db_url: str | None = None) -> Session:
    """Create all tables. Accepts an optional override URL for testing."""
    if db_url:
        test_engine = create_engine(db_url, echo=False)
        Base.metadata.create_all(bind=test_engine)
        return sessionmaker(bind=test_engine)()
    Base.metadata.create_all(bind=engine)
    return SessionLocal()


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def save_odds_update(db: Session, odds: "OddsUpdate") -> None:
    """Persist a single odds snapshot."""
    row = OddsHistoryRow(
        match_id=odds.match_id,
        market=odds.market,
        selection=odds.selection,
        odds_value=odds.odds_value,
        timestamp=odds.timestamp,
    )
    db.add(row)
    db.commit()


def get_odds_window(
    db: Session,
    match_id: str,
    market: str,
    selection: str,
    limit: int = 20,
) -> list[float]:
    """Return the most recent N odds values for a match/market/selection."""
    rows = (
        db.query(OddsHistoryRow)
        .filter_by(match_id=match_id, market=market, selection=selection)
        .order_by(OddsHistoryRow.timestamp.desc())
        .limit(limit)
        .all()
    )
    return [r.odds_value for r in reversed(rows)]


def save_signal(db: Session, signal: Signal) -> None:
    """Persist a detected signal."""
    row = SignalRow(
        id=signal.id,
        match_id=signal.match_id,
        market=signal.market,
        selection=signal.selection,
        odds_value=signal.odds_value,
        confidence=signal.confidence,
        direction=signal.direction.value,
        reason=signal.reason,
        status=signal.status.value,
        created_at=signal.created_at or _now(),
    )
    db.add(row)
    db.commit()


def mark_signal_resolved(
    db: Session, signal_id: str, status: SignalStatus
) -> None:
    """Mark a signal as correct or incorrect."""
    row = db.query(SignalRow).filter_by(id=signal_id).one_or_none()
    if row is None:
        raise ValueError(f"Signal {signal_id} not found")
    row.status = status.value
    row.resolved_at = _now()
    db.commit()


def get_recent_signals(
    db: Session, limit: int = 50, status: str | None = None
) -> list[Signal]:
    """Return the most recent signals, newest first."""
    query = db.query(SignalRow).order_by(SignalRow.created_at.desc())
    if status:
        query = query.filter_by(status=status)
    rows = query.limit(limit).all()
    return [
        Signal(
            id=r.id,
            match_id=r.match_id,
            market=r.market,
            selection=r.selection,
            odds_value=r.odds_value,
            confidence=r.confidence,
            direction=SignalDirection(r.direction),
            reason=r.reason,
            status=SignalStatus(r.status),
            created_at=r.created_at,
        )
        for r in rows
    ]


def get_pending_signals(db: Session) -> list[Signal]:
    """Return all signals with status 'pending'."""
    rows = (
        db.query(SignalRow)
        .filter_by(status="pending")
        .order_by(SignalRow.created_at.asc())
        .all()
    )
    return [
        Signal(
            id=r.id,
            match_id=r.match_id,
            market=r.market,
            selection=r.selection,
            odds_value=r.odds_value,
            confidence=r.confidence,
            direction=SignalDirection(r.direction),
            reason=r.reason,
            status=SignalStatus(r.status),
            created_at=r.created_at,
        )
        for r in rows
    ]


def get_pending_signals_for_match(db: Session, match_id: str) -> list[Signal]:
    """Return pending signals for a specific match."""
    rows = (
        db.query(SignalRow)
        .filter_by(status="pending", match_id=match_id)
        .order_by(SignalRow.created_at.asc())
        .all()
    )
    return [
        Signal(
            id=r.id,
            match_id=r.match_id,
            market=r.market,
            selection=r.selection,
            odds_value=r.odds_value,
            confidence=r.confidence,
            direction=SignalDirection(r.direction),
            reason=r.reason,
            status=SignalStatus(r.status),
            created_at=r.created_at,
        )
        for r in rows
    ]


def get_pending_match_ids(db: Session) -> list[str]:
    """Return distinct match_ids that have at least one pending signal."""
    rows = (
        db.query(SignalRow.match_id)
        .filter_by(status="pending")
        .distinct()
        .all()
    )
    return [r[0] for r in rows]


def save_match_outcome(db: Session, outcome: "MatchOutcome") -> None:
    """Persist a match outcome, updating if already exists."""
    existing = db.query(MatchOutcomeRow).filter_by(match_id=outcome.match_id).one_or_none()
    if existing:
        existing.home_goals = outcome.home_goals
        existing.away_goals = outcome.away_goals
        existing.winner = outcome.winner
        existing.recorded_at = outcome.recorded_at or _now()
    else:
        row = MatchOutcomeRow(
            match_id=outcome.match_id,
            home_goals=outcome.home_goals,
            away_goals=outcome.away_goals,
            winner=outcome.winner,
            recorded_at=outcome.recorded_at or _now(),
        )
        db.add(row)
    db.commit()


def get_match_outcome(db: Session, match_id: str) -> "MatchOutcome | None":
    """Fetch a match outcome by match_id, or None if not found."""
    row = db.query(MatchOutcomeRow).filter_by(match_id=match_id).one_or_none()
    if row is None:
        return None
    return MatchOutcome(
        match_id=row.match_id,
        home_goals=row.home_goals,
        away_goals=row.away_goals,
        winner=row.winner,
        recorded_at=row.recorded_at,
    )


def get_accuracy_stats(db: Session) -> AccuracyStats:
    """Compute overall and per-market accuracy from resolved signals."""
    resolved = (
        db.query(SignalRow)
        .filter(SignalRow.status.in_(["correct", "incorrect"]))
        .all()
    )

    correct = sum(1 for r in resolved if r.status == "correct")
    incorrect = sum(1 for r in resolved if r.status == "incorrect")
    total = correct + incorrect

    if total == 0:
        return AccuracyStats(
            total_resolved=0,
            correct=0,
            incorrect=0,
            overall_accuracy_pct=0.0,
            by_market=[],
        )

    by_market: dict[str, dict[str, int]] = {}
    for r in resolved:
        by_market.setdefault(r.market, {"correct": 0, "incorrect": 0})
        by_market[r.market][r.status] += 1

    market_stats = []
    for market, counts in sorted(by_market.items()):
        m_total = counts["correct"] + counts["incorrect"]
        market_stats.append(
            MarketAccuracy(
                market=market,
                total=m_total,
                correct=counts["correct"],
                incorrect=counts["incorrect"],
                accuracy_pct=round(counts["correct"] / m_total * 100, 1),
            )
        )

    return AccuracyStats(
        total_resolved=total,
        correct=correct,
        incorrect=incorrect,
        overall_accuracy_pct=round(correct / total * 100, 1),
        by_market=market_stats,
    )
