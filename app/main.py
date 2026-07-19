"""Sharp Signal — FastAPI application entrypoint."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.api.routes import router
from app.config import settings
from app.detection.engine import DetectionConfig, DetectionEngine
from app.detection.resolver import (
    generate_demo_signals,
    resolve_all_pending,
    seed_demo_outcomes,
)
from app.ingestion.txline_client import TxLineClient, TxLineError
from app.storage import init_db, save_odds_update, save_signal
from app.storage.db import SessionLocal
from app.storage.models import MatchMeta

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("sharp_signal")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_startup_ts: float = time.time()
_last_successful_poll: datetime | None = None

# Most-recent poll outcome, surfaced to the dashboard so auth/connectivity
# problems (e.g. an expired TxLINE token) are obvious instead of silently
# showing an empty board.
_poll_status: str = "starting"  # starting | ok | auth_error | not_configured | error | no_data
_poll_detail: str = ""


def _get_last_poll() -> datetime | None:
    return _last_successful_poll


def _set_poll_status(status: str, detail: str = "") -> None:
    global _poll_status, _poll_detail
    _poll_status = status
    _poll_detail = detail


# ---------------------------------------------------------------------------
# Background polling loop
# ---------------------------------------------------------------------------


def _cache_fixtures(fixtures: list[dict]) -> None:
    """Persist fixture metadata so dashboards can resolve match_ids to teams."""
    from app.storage.db import save_match

    db = SessionLocal()
    try:
        for fx in fixtures:
            match_id = fx.get("FixtureId")
            if match_id is None:
                continue
            start_ms = fx.get("StartTime")
            start_time = (
                datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
                if start_ms
                else None
            )
            save_match(
                db,
                MatchMeta(
                    match_id=str(match_id),
                    home_team=fx.get("Participant1", "Unknown"),
                    away_team=fx.get("Participant2", "Unknown"),
                    competition=fx.get("Competition", "Unknown"),
                    start_time=start_time,
                ),
            )
    except Exception:
        logger.exception("Error caching fixture metadata")
    finally:
        db.close()


def _filter_exclusive_shortening(signals: list) -> list:
    """Enforce that a mutually-exclusive market has at most one "shortening" signal.

    In markets like 1X2 (Home/Draw/Away) the outcomes are competitors: if one
    becomes more likely, the others must become less likely. The per-selection
    detector can flag several as "shortening" when the whole market swings, which
    is physically impossible (implied probabilities can't all rise). Keep only
    the single strongest "shortening" signal per market; drop the rest.
    """
    from app.detection.engine import SignalDirection

    shortening_by_market: dict[str, list] = {}
    others: list = []
    for s in signals:
        if s.direction == SignalDirection.SHORTENING:
            shortening_by_market.setdefault(s.market, []).append(s)
        else:
            others.append(s)

    kept: list = list(others)
    for market, group in shortening_by_market.items():
        if len(group) <= 1:
            kept.extend(group)
        else:
            # Keep only the most confident; log the contradiction.
            group.sort(key=lambda x: x.confidence, reverse=True)
            kept.append(group[0])
            logger.warning(
                "Market '%s' had %d selections simultaneously 'shortening' — "
                "kept only %s (conf %.1f), dropped %d contradictory signals",
                market, len(group), group[0].selection, group[0].confidence,
                len(group) - 1,
            )
    return kept


async def _poll_cycle(engine: DetectionEngine) -> None:
    """Run one ingestion → detection cycle.  Never raises."""
    global _last_successful_poll

    logger.info("Poll cycle starting")

    if not settings.txline_base_url:
        logger.debug("TXLINE_BASE_URL not configured — skipping poll cycle")
        _set_poll_status("not_configured", "TXLINE_BASE_URL is not set")
        return

    async with TxLineClient(
        jwt=settings.txline_api_key,
        api_token=settings.txline_api_token,
    ) as client:
        try:
            fixtures = await client.get_fixtures(
                competition_filter=settings.txline_competition_filter or None
            )
        except TxLineError as exc:
            if exc.status_code in (401, 403):
                logger.error("TxLINE auth failed (%s) — token may be invalid or expired", exc.status_code)
                _set_poll_status("auth_error", f"TxLINE returned {exc.status_code} — check TXLINE_API_KEY / TXLINE_API_TOKEN")
            else:
                logger.warning("Failed to fetch fixtures from TxLINE (%s)", exc.status_code)
                _set_poll_status("error", f"TxLINE error {exc.status_code}: {exc.message}")
            return
        except Exception:
            logger.exception("Unexpected error fetching fixtures — will retry next cycle")
            _set_poll_status("error", "Unexpected error fetching fixtures")
            return

        # Cache fixture metadata so the dashboard can show team names
        _cache_fixtures(fixtures)

        try:
            odds_updates = await client.fetch_all_odds(
                competition_filter=settings.txline_competition_filter or None
            )
        except TxLineError as exc:
            if exc.status_code in (401, 403):
                logger.error("TxLINE auth failed (%s) — token may be invalid or expired", exc.status_code)
                _set_poll_status("auth_error", f"TxLINE returned {exc.status_code} — check TXLINE_API_KEY / TXLINE_API_TOKEN")
            else:
                logger.warning("Failed to fetch odds from TxLINE (%s)", exc.status_code)
                _set_poll_status("error", f"TxLINE error {exc.status_code}: {exc.message}")
            return
        except Exception:
            logger.exception("Unexpected error fetching odds — will retry next cycle")
            _set_poll_status("error", "Unexpected error fetching odds")
            return

        if not odds_updates:
            logger.info("No odds updates received this cycle")
            _set_poll_status("no_data", "TxLINE returned no odds this cycle (matches may not be live yet)")
            return

        logger.info("Received %d odds updates", len(odds_updates))

        # Run detection for the whole cycle, collecting signals so we can
        # apply cross-selection plausibility rules before persisting.
        db = SessionLocal()
        cycle_signals: list = []
        signals_created = 0
        try:
            for update in odds_updates:
                try:
                    # Save the raw odds update
                    save_odds_update(db, update)

                    # Run detection
                    signal = engine.process(update)
                    if signal is not None:
                        cycle_signals.append(signal)
                except Exception:
                    logger.exception(
                        "Error processing update for match=%s market=%s selection=%s",
                        update.match_id,
                        update.market,
                        update.selection,
                    )

            # Drop physically-impossible signals: in a mutually-exclusive market
            # (e.g. 1X2) at most one selection can be "shortening" (more likely)
            # at once. If several are, keep only the strongest.
            cycle_signals = _filter_exclusive_shortening(cycle_signals)
            for signal in cycle_signals:
                save_signal(db, signal)
                signals_created += 1
                logger.info(
                    "Signal created: id=%s match=%s market=%s selection=%s "
                    "direction=%s confidence=%.1f reason=%s",
                    signal.id, signal.match_id, signal.market, signal.selection,
                    signal.direction.value, signal.confidence, signal.reason,
                )
        finally:
            db.close()

        # Resolve any pending signals (live feed + optional demo seed)
        resolve_db = SessionLocal()
        try:
            if settings.sharp_demo_seed:
                # Generate synthetic activity so the board has something to
                # resolve against the demo outcomes, then resolve it.
                generate_demo_signals(resolve_db)
            resolved = await resolve_all_pending(client, resolve_db)
            if settings.sharp_demo_seed:
                resolved += seed_demo_outcomes(resolve_db)
        except Exception:
            logger.exception("Error during signal resolution")
            resolved = 0
        finally:
            resolve_db.close()

    _last_successful_poll = datetime.now(timezone.utc)
    _set_poll_status("ok")
    logger.info(
        "Poll cycle complete: %d odds processed, %d signals created, %d resolved",
        len(odds_updates),
        signals_created,
        resolved,
    )


async def _background_loop(shutdown_event: asyncio.Event) -> None:
    """Infinite loop that runs _poll_cycle every POLL_INTERVAL_SECONDS.

    Checks the shutdown_event before each sleep so the loop exits promptly
    when the application is shutting down.
    """
    logger.info(
        "Background loop started (interval=%ds)", settings.poll_interval_seconds
    )
    engine = DetectionEngine(
        DetectionConfig(
            z_score_threshold=settings.z_score_threshold,
            pct_change_threshold=settings.pct_change_threshold,
            rolling_window_size=settings.rolling_window_size,
            min_window_size=settings.min_window_size,
            signal_cooldown_seconds=settings.signal_cooldown_seconds,
        )
    )
    while not shutdown_event.is_set():
        await _poll_cycle(engine)
        logger.info("Sleeping for %ds until next cycle", settings.poll_interval_seconds)
        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=settings.poll_interval_seconds
            )
            break  # shutdown requested
        except asyncio.TimeoutError:
            pass  # normal timeout — continue to next cycle


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------

_background_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of the background polling loop."""
    global _background_task

    shutdown_event = asyncio.Event()

    logger.info("Initializing database")
    if settings.sharp_reset_db:
        import os

        from app.storage.db import engine as _db_engine

        db_path = settings.database_url.replace("sqlite:///", "")
        logger.warning("SHARP_RESET_DB set — dropping all tables and starting fresh")
        _db_engine.dispose()
        if db_path and os.path.exists(db_path):
            os.remove(db_path)
    init_db()
    logger.info("Starting background poll loop")
    _background_task = asyncio.create_task(_background_loop(shutdown_event))

    yield

    logger.info("Shutting down background poll loop")
    shutdown_event.set()
    if _background_task:
        try:
            await asyncio.wait_for(_background_task, timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _background_task.cancel()
            try:
                await _background_task
            except asyncio.CancelledError:
                pass
    logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Sharp Signal",
    description="Signal ingestion, detection, and flagging service",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)

_DASHBOARD_PATH = Path(__file__).parent / "api" / "dashboard.html"


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the live dashboard."""
    return _DASHBOARD_PATH.read_text(encoding="utf-8")
