"""Signal resolver — checks pending signals against match outcomes.

This module fetches final scores from TxLINE for matches with pending
signals and marks each signal correct or incorrect based on whether the
odds movement direction matched the actual result.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.ingestion.txline_client import TxLineClient, TxLineError
from app.storage.db import (
    get_all_matches,
    get_match_outcome,
    get_pending_match_ids,
    get_pending_signals_for_match,
    mark_signal_resolved,
    save_match,
    save_match_outcome,
    SignalRow,
)
from app.storage.models import MatchMeta, MatchOutcome, Signal, SignalDirection, SignalStatus

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

# Team-name fallback so the seed still resolves even if the live feed emits a
# different fixture ID than the ones hardcoded above.
DEMO_TEAM_MATCHES: dict[tuple[str, str], tuple[int, int]] = {
    ("France", "England"): (0, 4),
    ("Spain", "Argentina"): (2, 1),
}


def _resolve_demo_match(db: Session, match_id: str, home_goals: int, away_goals: int) -> int:
    winner = (
        "home" if home_goals > away_goals
        else ("away" if away_goals > home_goals else "draw")
    )
    outcome = MatchOutcome(
        match_id=match_id,
        home_goals=home_goals,
        away_goals=away_goals,
        winner=winner,
        recorded_at=datetime.now(timezone.utc),
    )
    try:
        return resolve_signals_with_outcome(db, match_id, outcome)
    except Exception:
        logger.exception("Demo seed failed for match %s", match_id)
        return 0


def seed_demo_outcomes(db: Session) -> int:
    """Resolve pending signals against synthetic final scores (demo only).

    Returns the total number of signals resolved. Idempotent: it only acts on
    signals still in 'pending' and a match is resolved at most once (once
    signals are resolved there is nothing left to do).

    Matches primarily on the hardcoded fixture IDs, but falls back to team
    names so the seed works regardless of which fixture ID the live feed
    actually emits.
    """
    total = 0

    # Primary: hardcoded fixture IDs.
    for match_id, (home_goals, away_goals) in DEMO_OUTCOMES.items():
        pending = get_pending_signals_for_match(db, match_id)
        if pending and _match_has_started(db, match_id):
            total += _resolve_demo_match(db, match_id, home_goals, away_goals)

    # Fallback: match pending signals by cached team names (partial,
    # case-insensitive so "Spain" / "ESP" / "Spain (Q)" all match).
    meta_by_id = {m.match_id: m for m in get_all_matches(db)}
    pending_ids = get_pending_match_ids(db)
    for match_id in pending_ids:
        meta = meta_by_id.get(match_id)
        if meta is None:
            logger.debug("Demo seed: match %s has pending signals but no cached metadata", match_id)
            continue
        home, away = (meta.home_team or "").lower(), (meta.away_team or "").lower()
        matched = None
        for (h, a), score in DEMO_TEAM_MATCHES.items():
            if _name_in(home, h) and _name_in(away, a):
                matched = score
                break
            if _name_in(home, a) and _name_in(away, h):
                # Teams swapped in the feed — flip the score.
                matched = (score[1], score[0])
                break
        if matched and _match_has_started(db, match_id):
            total += _resolve_demo_match(db, match_id, matched[0], matched[1])

    if total:
        logger.info("Demo seed resolved %d signals", total)
    else:
        logger.debug(
            "Demo seed resolved 0 signals (pending match ids: %s, cached: %s)",
            pending_ids,
            [m.match_id for m in meta_by_id.values()],
        )
    return total


def _name_in(haystack: str, needle: str) -> bool:
    """Case-insensitive substring match for team-name fallback."""
    return needle.lower() in haystack.lower()


def _match_has_started(db: Session, match_id: str) -> bool:
    """Return True if a match's kickoff is in the past (or unknown).

    Prevents the demo seed from resolving a match that hasn't been played
    yet — a future fixture should stay pending until its kickoff passes.
    Matches with no cached start_time are treated as started (the live
    resolver path, where we only act on finalised scores anyway).
    """
    now = datetime.now(timezone.utc)
    for m in get_all_matches(db):
        if m.match_id == match_id:
            if m.start_time is None:
                return True
            start = m.start_time
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            return start <= now
    return True


# Synthetic signals replayed when SHARP_DEMO_SEED is enabled, so the board has
# live-looking activity to resolve against the demo outcomes above. Clearly
# synthetic — DO NOT enable in production.
DEMO_SIGNALS: list[dict] = [
    # France 0 - 4 England (away win): Away shortens, Home/Draw drift
    {"match_id": "18257865", "market": "1X2", "selection": "Away", "odds_value": 1.85, "direction": "shortening", "confidence": 72.0, "move_pct": 6.2, "window": 14, "z": 4.1},
    {"match_id": "18257865", "market": "1X2", "selection": "Home", "odds_value": 240.0, "direction": "drifting", "confidence": 55.0, "move_pct": 4.1, "window": 16, "z": 3.0},
    {"match_id": "18257865", "market": "1X2", "selection": "Draw", "odds_value": 16.0, "direction": "drifting", "confidence": 48.0, "move_pct": 3.3, "window": 15, "z": 2.6},
    {"match_id": "18257865", "market": "Over/Under 2.5", "selection": "Over", "odds_value": 1.95, "direction": "shortening", "confidence": 60.0, "move_pct": 3.8, "window": 18, "z": 3.3},
    {"match_id": "18257865", "market": "Over/Under 2.5", "selection": "Under", "odds_value": 1.85, "direction": "drifting", "confidence": 52.0, "move_pct": 3.1, "window": 18, "z": 2.7},
    # Spain 2 - 1 Argentina (home win): most signals point home, but a couple
    # misread the market so accuracy ends up believable rather than 100%.
    {"match_id": "18257739", "market": "1X2", "selection": "Home", "odds_value": 2.10, "direction": "shortening", "confidence": 68.0, "move_pct": 5.4, "window": 17, "z": 3.8},
    {"match_id": "18257739", "market": "1X2", "selection": "Away", "odds_value": 3.40, "direction": "shortening", "confidence": 52.0, "move_pct": 4.0, "window": 16, "z": 2.9},
    {"match_id": "18257739", "market": "1X2", "selection": "Draw", "odds_value": 3.30, "direction": "drifting", "confidence": 45.0, "move_pct": 2.7, "window": 19, "z": 2.3},
    {"match_id": "18257739", "market": "Over/Under 2.5", "selection": "Over", "odds_value": 1.90, "direction": "shortening", "confidence": 58.0, "move_pct": 3.6, "window": 20, "z": 3.1},
    {"match_id": "18257739", "market": "Over/Under 2.5", "selection": "Under", "odds_value": 1.90, "direction": "drifting", "confidence": 49.0, "move_pct": 2.9, "window": 20, "z": 2.5},
]


def generate_demo_signals(db: Session) -> int:
    """Create synthetic pending signals for the demo matches (demo only).

    Idempotent: skips any (match_id, market, selection) that already has a
    signal so re-running poll cycles don't duplicate demo activity. Returns
    the number of demo signals created this call.
    """
    from app.storage.db import save_match, save_signal

    # Ensure the demo fixtures have cached metadata so the dashboard can show
    # team names even if the live feed hasn't cached them yet. Demo kickoffs
    # are set in the past so the seed treats them as already played.
    demo_kickoffs = {
        "18257865": datetime(2026, 7, 18, 21, 0, tzinfo=timezone.utc),
        "18257739": datetime(2026, 7, 18, 21, 0, tzinfo=timezone.utc),
    }
    for match_id, (home, away) in {
        "18257865": ("France", "England"),
        "18257739": ("Spain", "Argentina"),
    }.items():
        save_match(
            db,
            MatchMeta(
                match_id=match_id,
                home_team=home,
                away_team=away,
                competition="World Cup",
                start_time=demo_kickoffs[match_id],
            ),
        )

    existing = {
        (r.match_id, r.market, r.selection)
        for r in db.query(SignalRow).all()
    }
    created = 0
    for spec in DEMO_SIGNALS:
        key = (spec["match_id"], spec["market"], spec["selection"])
        if key in existing:
            continue
        save_signal(
            db,
            Signal(
                id=str(uuid.uuid4()),
                match_id=spec["match_id"],
                market=spec["market"],
                selection=spec["selection"],
                odds_value=spec["odds_value"],
                confidence=spec["confidence"],
                direction=SignalDirection(spec["direction"]),
                reason=(
                    f"Odds for {spec['selection']} moved {spec['move_pct']:.1f}% "
                    f"over {spec['window']} observations — {spec['z']:.1f} standard "
                    f"deviations from its recent average. This is a "
                    f"{'highly unusual' if spec['z'] >= 5 else 'moderately unusual'} "
                    f"movement."
                ),
                status=SignalStatus.PENDING,
                created_at=datetime.now(timezone.utc),
            ),
        )
        created += 1
    if created:
        logger.info("Demo seed generated %d synthetic signals", created)
    return created


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
