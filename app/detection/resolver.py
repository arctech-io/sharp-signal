"""Signal resolver — checks pending signals against match outcomes.

This module fetches final scores from TxLINE for matches with pending
signals and marks each signal correct or incorrect based on whether the
odds movement direction matched the actual result.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.ingestion.txline_client import TxLineClient, TxLineError
from app.storage.db import (
    get_match_outcome,
    get_pending_match_ids,
    get_pending_signals_for_match,
    mark_signal_resolved,
    save_match_outcome,
)
from app.storage.models import MatchOutcome, Signal, SignalDirection, SignalStatus

logger = logging.getLogger(__name__)


def _determine_signal_correctness(
    signal: Signal, outcome: MatchOutcome
) -> SignalStatus:
    """Determine if a signal's implied direction matched the actual result.

    Logic:
    - "shortening" on a selection means that outcome became MORE likely.
    - "drifting" on a selection means that outcome became LESS likely.
    We check if the selection's outcome actually happened.

    For "1X2" market: selection is "Home", "Draw", or "Away".
    For "Over/Under" market: selection is "Over" or "Under".
    """
    market = signal.market.lower()
    selection = signal.selection.lower()

    selection_won = _selection_won(selection, market, outcome)

    if signal.direction == SignalDirection.SHORTENING:
        # Odds shortened → outcome became more likely → should have happened
        return SignalStatus.CORRECT if selection_won else SignalStatus.INCORRECT
    else:
        # Odds drifted → outcome became less likely → should NOT have happened
        return SignalStatus.INCORRECT if selection_won else SignalStatus.CORRECT


def _selection_won(selection: str, market: str, outcome: MatchOutcome) -> bool:
    """Check if a given selection was the actual outcome of the match."""
    if market in ("1x2",):
        if selection == "home":
            return outcome.winner == "home"
        elif selection == "away":
            return outcome.winner == "away"
        elif selection == "draw":
            return outcome.winner == "draw"
    elif market in ("over/under 2.5", "over/under 1.5", "over/under 3.5"):
        total_goals = outcome.home_goals + outcome.away_goals
        try:
            line = float(market.split()[-1])
        except (IndexError, ValueError):
            line = 2.5
        if selection == "over":
            return total_goals > line
        elif selection == "under":
            return total_goals <= line
    elif market in ("both teams to score", "btts"):
        both_scored = outcome.home_goals > 0 and outcome.away_goals > 0
        if selection == "yes":
            return both_scored
        elif selection == "no":
            return not both_scored

    # Unknown market — conservatively mark as incorrect
    return False


async def resolve_signals_for_match(
    client: TxLineClient,
    db: Session,
    match_id: str,
) -> int:
    """Fetch the final score for *match_id* and resolve all its pending signals.

    Returns the number of signals resolved.
    """
    pending = get_pending_signals_for_match(db, match_id)
    if not pending:
        return 0

    logger.info(
        "Resolving %d pending signals for match %s", len(pending), match_id
    )

    # Try to get the scores snapshot from TxLINE
    try:
        scores = await client._request("GET", f"/api/scores/snapshot/{match_id}")
        scores_data = scores.json()
    except TxLineError:
        logger.warning(
            "Could not fetch scores for match %s — will retry next cycle",
            match_id,
        )
        return 0

    if not scores_data:
        logger.debug("No score data yet for match %s", match_id)
        return 0

    # Parse the final score from the scores snapshot
    outcome = _parse_match_outcome(match_id, scores_data)
    if outcome is None:
        logger.debug(
            "Match %s has no finalised score yet (action=game_finalised not found)",
            match_id,
        )
        return 0

    # Persist the outcome
    save_match_outcome(db, outcome)
    logger.info(
        "Recorded outcome for match %s: %d-%d (%s)",
        match_id,
        outcome.home_goals,
        outcome.away_goals,
        outcome.winner,
    )

    # Resolve each pending signal
    resolved_count = 0
    for signal in pending:
        try:
            status = _determine_signal_correctness(signal, outcome)
            mark_signal_resolved(db, signal.id, status)
            resolved_count += 1
            logger.info(
                "Signal %s resolved as %s (match=%s, market=%s, selection=%s, direction=%s)",
                signal.id,
                status.value,
                signal.match_id,
                signal.market,
                signal.selection,
                signal.direction.value,
            )
        except Exception:
            logger.exception(
                "Failed to resolve signal %s — will retry next cycle", signal.id
            )

    return resolved_count


def _parse_match_outcome(match_id: str, scores_data: list[dict]) -> MatchOutcome | None:
    """Parse a MatchOutcome from TxLINE scores snapshot data.

    Looks for action=game_finalised (statusId=100, period=100) entries.
    Falls back to the latest score entry if no finalised marker is found.
    """
    finalised = None
    latest = None

    for entry in scores_data:
        action = str(entry.get("Action", "")).lower()
        status_id = entry.get("StatusId")
        period = entry.get("Period")

        latest = entry

        if action == "game_finalised" or status_id == 100 or period == 100:
            finalised = entry
            break

    entry = finalised or latest
    if entry is None:
        return None

    home_goals = entry.get("Participant1Goals") or entry.get("HomeGoals") or 0
    away_goals = entry.get("Participant2Goals") or entry.get("AwayGoals") or 0

    try:
        home_goals = int(home_goals)
        away_goals = int(away_goals)
    except (TypeError, ValueError):
        return None

    if home_goals > away_goals:
        winner = "home"
    elif away_goals > home_goals:
        winner = "away"
    else:
        winner = "draw"

    return MatchOutcome(
        match_id=match_id,
        home_goals=home_goals,
        away_goals=away_goals,
        winner=winner,
        recorded_at=datetime.now(timezone.utc),
    )


async def resolve_all_pending(
    client: TxLineClient,
    db: Session,
) -> int:
    """Resolve all pending signals across all matches.

    Returns total number of signals resolved.
    """
    match_ids = get_pending_match_ids(db)
    if not match_ids:
        return 0

    logger.info("Checking %d matches for score resolution", len(match_ids))
    total = 0
    for match_id in match_ids:
        try:
            count = await resolve_signals_for_match(client, db, match_id)
            total += count
        except Exception:
            logger.exception(
                "Error resolving signals for match %s — continuing", match_id
            )

    if total > 0:
        logger.info("Resolved %d signals across %d matches", total, len(match_ids))

    return total
