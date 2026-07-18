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
from app.detection.resolver import resolve_all_pending
from app.ingestion.txline_client import TxLineClient, TxLineError
from app.storage import init_db, save_odds_update, save_signal
from app.storage.db import SessionLocal

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


def _get_last_poll() -> datetime | None:
    return _last_successful_poll


# ---------------------------------------------------------------------------
# Background polling loop
# ---------------------------------------------------------------------------


async def _poll_cycle(engine: DetectionEngine) -> None:
    """Run one ingestion → detection cycle.  Never raises."""
    global _last_successful_poll

    logger.info("Poll cycle starting")

    if not settings.txline_base_url:
        logger.debug("TXLINE_BASE_URL not configured — skipping poll cycle")
        return

    async with TxLineClient(
        jwt=settings.txline_api_key,
        api_token=settings.txline_api_token,
    ) as client:
        try:
            odds_updates = await client.fetch_all_odds()
        except TxLineError:
            logger.exception("Failed to fetch odds from TxLINE — will retry next cycle")
            return
        except Exception:
            logger.exception("Unexpected error fetching odds — will retry next cycle")
            return

        if not odds_updates:
            logger.info("No odds updates received this cycle")
            return

        logger.info("Received %d odds updates", len(odds_updates))

        # Run detection and persist
        db = SessionLocal()
        signals_created = 0
        try:
            for update in odds_updates:
                try:
                    # Save the raw odds update
                    save_odds_update(db, update)

                    # Run detection
                    signal = engine.process(update)
                    if signal is not None:
                        save_signal(db, signal)
                        signals_created += 1
                        logger.info(
                            "Signal created: id=%s match=%s market=%s selection=%s "
                            "direction=%s confidence=%.1f reason=%s",
                            signal.id,
                            signal.match_id,
                            signal.market,
                            signal.selection,
                            signal.direction.value,
                            signal.confidence,
                            signal.reason,
                        )
                except Exception:
                    logger.exception(
                        "Error processing update for match=%s market=%s selection=%s",
                        update.match_id,
                        update.market,
                        update.selection,
                    )
        finally:
            db.close()

        # Resolve any pending signals
        resolve_db = SessionLocal()
        try:
            resolved = await resolve_all_pending(client, resolve_db)
        except Exception:
            logger.exception("Error during signal resolution")
            resolved = 0
        finally:
            resolve_db.close()

    _last_successful_poll = datetime.now(timezone.utc)
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
