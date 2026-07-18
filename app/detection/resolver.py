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
    """Check if a given selection was the actual outcome of the match.

    Markets are matched on normalized tokens rather than exact strings so
    TxLINE's `SuperOddsType` values (e.g. `1X2_PARTICIPANT_RESULT`,
    `OVERUNDER_PARTICIPANT_GOALS`) resolve correctly.
    """
    m = market.lower()
    sel = selection.lower()

    if "1x2" in m or "participant_result" in m:
        # TxLINE selections arrive as part1 / draw / part2.
        if sel in ("home", "part1"):
            return outcome.winner == "home"
        elif sel in ("away", "part2"):
            return outcome.winner == "away"
        elif sel == "draw":
            return outcome.winner == "draw"
    elif "overunder" in m or ("over" in m and "under" in m):
        total_goals = outcome.home_goals + outcome.away_goals
        line = _extract_overunder_line(m)
        if sel == "over":
            return total_goals > line
        elif sel == "under":
            return total_goals <= line
    elif "both teams" in m or "btts" in m:
        both_scored = outcome.home_goals > 0 and outcome.away_goals > 0
        if sel == "yes":
            return both_scored
        elif sel == "no":
            return not both_scored

    # Unknown market — cannot determine correctness.
    logger.warning(
        "Unknown market '%s' for selection '%s' — cannot resolve signal correctness",
        market,
        selection,
    )
    return False


def _extract_overunder_line(market: str) -> float:
    """Pull the goal line (e.g. 2.5) out of an over/under market string."""
    import re

    match = re.search(r"(\d+(?:\.\d+)?)", market)
    return float(match.group(1)) if match else 2.5


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

    # Try to get the scores snapshot from TxLINE
    try:
        scores_data = await client.get_scores_snapshot(match_id)
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

    return resolve_signals_with_outcome(db, match_id, outcome)


def resolve_signals_with_outcome(
    db: Session,
    match_id: str,
    outcome: MatchOutcome,
) -> int:
    """Resolve all pending signals for *match_id* against a known *outcome*.

    Shared by the live resolver (outcome from TxLINE) and the demo seeder
    (outcome supplied synthetically). Returns the number of signals resolved.
    """
    pending = get_pending_signals_for_match(db, match_id)
    if not pending:
        return 0

    # Persist the outcome (idempotent — re-saving the same match is fine)
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


# ---------------------------------------------------------------------------
# Demo seed mode
# ---------------------------------------------------------------------------

# Final results replayed when SHARP_DEMO_SEED is enabled, so the accuracy
# panel has something to show even though the dev TxLINE feed never emits a
# finalised score. Clearly synthetic — DO NOT enable in production.
DEMO_OUTCOMES: dict[str, tuple[int, int]] = {
    "18257865": (0, 4),  # France 0 - 4 England  (winner: away)
    "18257739": (2, 1),  # Spain 2 - 1 Argentina  (winner: home)
}


def seed_demo_outcomes(db: Session) -> int:
    """Resolve pending signals against synthetic final scores (demo only).

    Returns the total number of signals resolved. Idempotent: it only acts on
    signals still in 'pending' and a match is resolved at most once (once
    signals are resolved there is nothing left to do).
    """
    total = 0
    for match_id, (home_goals, away_goals) in DEMO_OUTCOMES.items():
        winner = "home" if home_goals > away_goals else ("away" if away_goals > home_goals else "draw")
        outcome = MatchOutcome(
            match_id=match_id,
            home_goals=home_goals,
            away_goals=away_goals,
            winner=winner,
            recorded_at=datetime.now(timezone.utc),
        )
        try:
            total += resolve_signals_with_outcome(db, match_id, outcome)
        except Exception:
            logger.exception("Demo seed failed for match %s", match_id)
    if total:
        logger.info("Demo seed resolved %d signals", total)
    return total


def _parse_match_outcome(match_id: str, scores_data: list[dict]) -> MatchOutcome | None:
    """Parse a MatchOutcome from TxLINE scores snapshot data.

    Only returns a result once the match is actually finalised
    (action=game_finalised, StatusId=100, or Period=100). Live/in-progress
    scores are intentionally ignored so signals are never resolved against an
    unfinished match. Returns None if no finalised marker is present.
    """
    finalised = None

    for entry in scores_data:
        action = str(entry.get("Action", "")).lower()
        status_id = entry.get("StatusId")
        period = entry.get("Period")

        if action == "game_finalised" or status_id == 100 or period == 100:
            finalised = entry
            break

    if finalised is None:
        return None

    home_goals = finalised.get("Participant1Goals") or finalised.get("HomeGoals") or 0
    away_goals = finalised.get("Participant2Goals") or finalised.get("AwayGoals") or 0

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
