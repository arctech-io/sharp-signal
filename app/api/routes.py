"""FastAPI routes for health, signals, accuracy, and the dashboard."""

import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.storage.db import (
    SignalRow,
    get_accuracy_stats,
    get_all_matches,
    get_db,
    get_recent_signals,
)
from app.storage.models import MatchMeta, Signal, SignalDirection, SignalStatus

router = APIRouter()


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@router.get("/health")
async def health_check():
    """Return uptime, last successful poll, and the latest poll status.

    ``poll_status`` is one of: starting, ok, auth_error, not_configured,
    error, no_data. It lets the dashboard surface connectivity/auth problems
    (e.g. an expired TxLINE token) instead of silently showing an empty board.
    """
    from app.main import (
        _get_last_poll,
        _poll_detail,
        _poll_status,
        _startup_ts,
    )

    last_poll = _get_last_poll()
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - _startup_ts, 1),
        "last_successful_poll": last_poll.isoformat() if last_poll else None,
        "poll_status": _poll_status,
        "poll_detail": _poll_detail,
    }


@router.get("/signals")
async def list_signals(
    limit: int = 50,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    """List recent signals with confidence, reasoning, and status."""
    signals = get_recent_signals(db, limit=limit, status=status)
    return {
        "total": len(signals),
        "signals": [s.model_dump(mode="json") for s in signals],
    }


@router.get("/signals/{signal_id}")
async def get_signal(signal_id: str, db: Session = Depends(get_db)):
    """Get a specific signal by ID."""
    row = db.query(SignalRow).filter_by(id=signal_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return Signal(
        id=row.id,
        match_id=row.match_id,
        market=row.market,
        selection=row.selection,
        odds_value=row.odds_value,
        confidence=row.confidence,
        direction=SignalDirection(row.direction),
        reason=row.reason,
        status=SignalStatus(row.status),
        created_at=row.created_at,
    ).model_dump(mode="json")


@router.get("/accuracy")
async def accuracy_stats(db: Session = Depends(get_db)):
    """Return overall and per-market accuracy stats."""
    stats = get_accuracy_stats(db)
    return stats.model_dump(mode="json")


@router.get("/matches")
async def list_matches(db: Session = Depends(get_db)):
    """List cached fixture metadata (team names, competition, kickoff)."""
    matches = get_all_matches(db)
    return {
        "total": len(matches),
        "matches": [m.model_dump(mode="json") for m in matches],
    }
