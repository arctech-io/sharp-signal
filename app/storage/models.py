"""Pydantic models for the data layer."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class SignalDirection(str, Enum):
    SHORTENING = "shortening"
    DRIFTING = "drifting"


class SignalStatus(str, Enum):
    PENDING = "pending"
    CORRECT = "correct"
    INCORRECT = "incorrect"


class OddsUpdate(BaseModel):
    """A single odds snapshot fetched from TxLINE."""

    match_id: str
    market: str
    selection: str
    odds_value: float
    timestamp: datetime


class Signal(BaseModel):
    """A detected sharp movement."""

    id: str | None = None
    match_id: str
    market: str
    selection: str
    odds_value: float
    confidence: float = Field(ge=0, le=100)
    direction: SignalDirection
    reason: str
    status: SignalStatus = SignalStatus.PENDING
    created_at: datetime | None = None


class MatchOutcome(BaseModel):
    """The actual result of a finished match."""

    match_id: str
    home_goals: int
    away_goals: int
    winner: str  # "home", "away", "draw"
    recorded_at: datetime | None = None


class MatchMeta(BaseModel):
    """Human-readable metadata for a fixture from TxLINE."""

    match_id: str
    home_team: str
    away_team: str
    competition: str
    start_time: datetime | None = None


class MarketAccuracy(BaseModel):
    """Accuracy breakdown for a single market type."""

    market: str
    total: int
    correct: int
    incorrect: int
    accuracy_pct: float


class AccuracyStats(BaseModel):
    """Aggregate accuracy report across all resolved signals."""

    total_resolved: int
    correct: int
    incorrect: int
    overall_accuracy_pct: float
    by_market: list[MarketAccuracy]
