"""Database models and queries."""

from app.storage.db import (
    Base,
    get_accuracy_stats,
    get_db,
    get_match_outcome,
    get_odds_window,
    get_pending_match_ids,
    get_pending_signals,
    get_recent_signals,
    init_db,
    mark_signal_resolved,
    save_match_outcome,
    save_odds_update,
    save_signal,
)
from app.storage.models import (
    AccuracyStats,
    MarketAccuracy,
    MatchOutcome,
    OddsUpdate,
    Signal,
    SignalDirection,
    SignalStatus,
)

__all__ = [
    "AccuracyStats",
    "Base",
    "MarketAccuracy",
    "MatchOutcome",
    "OddsUpdate",
    "Signal",
    "SignalDirection",
    "SignalStatus",
    "get_accuracy_stats",
    "get_db",
    "get_match_outcome",
    "get_odds_window",
    "get_pending_match_ids",
    "get_pending_signals",
    "get_recent_signals",
    "init_db",
    "mark_signal_resolved",
    "save_match_outcome",
    "save_odds_update",
    "save_signal",
]
