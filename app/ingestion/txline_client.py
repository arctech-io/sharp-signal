"""Async TxLINE API client with retry and exponential backoff."""

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.storage.models import OddsUpdate

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0
RATE_LIMIT_DELAY = 0.5  # seconds between fixture odds requests


class TxLineError(Exception):
    """Raised when the TxLINE API returns a non-retryable error."""


class TxLineClient:
    """Async client for the TxLINE sports data API.

    Reads TXLINE_BASE_URL and the JWT + API token from config.
    Retries on network errors and 5xx responses with exponential backoff.
    """

    def __init__(
        self,
        base_url: str | None = None,
        jwt: str = "",
        api_token: str = "",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url or settings.txline_base_url).rstrip("/") + "/"
        self.jwt = jwt
        self.api_token = api_token
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    # -- context manager ----------------------------------------------------

    async def __aenter__(self) -> "TxLineClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            headers=self._headers(),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # -- internal helpers ---------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.jwt:
            h["Authorization"] = f"Bearer {self.jwt}"
        if self.api_token:
            h["X-Api-Token"] = self.api_token
        return h

    async def _request(
        self, method: str, path: str, **kwargs
    ) -> httpx.Response:
        """Issue an HTTP request with retry + exponential backoff."""
        if self._client is None:
            raise RuntimeError(
                "TxLineClient must be used as an async context manager"
            )

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await self._client.request(method, path, **kwargs)

                if resp.status_code >= 500:
                    body = resp.text[:200]
                    logger.warning(
                        "TxLINE %s %s returned %d (attempt %d/%d): %s",
                        method,
                        path,
                        resp.status_code,
                        attempt,
                        MAX_RETRIES,
                        body,
                    )
                    last_exc = TxLineError(
                        f"Server error {resp.status_code} on {path}"
                    )
                else:
                    resp.raise_for_status()
                    return resp

            except httpx.TimeoutException as exc:
                logger.warning(
                    "TxLINE %s %s timed out (attempt %d/%d): %s",
                    method,
                    path,
                    attempt,
                    MAX_RETRIES,
                    exc,
                )
                last_exc = exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code >= 500:
                    logger.warning(
                        "TxLINE %s %s returned %d (attempt %d/%d)",
                        method,
                        path,
                        exc.response.status_code,
                        attempt,
                        MAX_RETRIES,
                    )
                    last_exc = exc
                else:
                    raise TxLineError(
                        f"Client error {exc.response.status_code} on {path}: "
                        f"{exc.response.text[:200]}"
                    ) from exc
            except httpx.RequestError as exc:
                logger.warning(
                    "TxLINE %s %s network error (attempt %d/%d): %s",
                    method,
                    path,
                    attempt,
                    MAX_RETRIES,
                    exc,
                )
                last_exc = exc

            if attempt < MAX_RETRIES:
                delay = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)
                logger.info("Retrying in %.1fs...", delay)
                await asyncio.sleep(delay)

        raise TxLineError(
            f"All {MAX_RETRIES} attempts failed for {path}"
        ) from last_exc

    # -- public API ---------------------------------------------------------

    async def get_fixtures(self) -> list[dict]:
        """Fetch current fixture snapshots from TxLINE.

        Returns:
            Raw fixture dicts from the API.
        """
        resp = await self._request("GET", "fixtures/snapshot")
        return resp.json()

    async def get_odds_for_fixture(self, fixture_id: int) -> list[dict]:
        """Fetch odds snapshot for a specific fixture.

        Args:
            fixture_id: TxLINE fixture ID.

        Returns:
            Raw odds payload dicts from the API.
        """
        resp = await self._request("GET", f"odds/snapshot/{fixture_id}")
        return resp.json()

    async def get_live_matches(self) -> list[dict]:
        """Fetch all live/upcoming fixtures.

        Returns:
            List of raw fixture dicts.
        """
        return await self.get_fixtures()

    async def fetch_all_odds(self) -> list[OddsUpdate]:
        """Fetch fixtures then odds for each, normalized to OddsUpdate.

        Returns:
            List of OddsUpdate models ready for persistence.
        """
        fixtures = await self.get_fixtures()
        all_odds: list[OddsUpdate] = []

        for fx in fixtures:
            fixture_id = fx.get("FixtureId")
            if fixture_id is None:
                continue

            if all_odds:
                await asyncio.sleep(RATE_LIMIT_DELAY)

            try:
                odds_list = await self.get_odds_for_fixture(int(fixture_id))
            except TxLineError:
                logger.warning(
                    "Skipping fixture %s — odds fetch failed", fixture_id
                )
                continue

            all_odds.extend(_normalize_odds(fixture_id, odds_list))

        return all_odds


def _normalize_odds(fixture_id: int, odds_payloads: list[dict]) -> list[OddsUpdate]:
    """Convert raw TxLINE OddsPayload list into OddsUpdate models.

    Each OddsPayload has PriceNames[] and Prices[] arrays.  We emit one
    OddsUpdate per (market, selection) pair.

    Args:
        fixture_id: The fixture these odds belong to.
        odds_payloads: Raw API response dicts.

    Returns:
        Flat list of OddsUpdate models.
    """
    updates: list[OddsUpdate] = []

    for payload in odds_payloads:
        market = payload.get("SuperOddsType", "unknown")
        price_names = payload.get("PriceNames") or []
        raw_prices = payload.get("Prices") or []
        ts_ms = payload.get("Ts", 0)

        timestamp = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

        for i, selection in enumerate(price_names):
            if i < len(raw_prices):
                odds_value = raw_prices[i] / 1000.0
            else:
                odds_value = 0.0

            updates.append(
                OddsUpdate(
                    match_id=str(fixture_id),
                    market=market,
                    selection=selection,
                    odds_value=odds_value,
                    timestamp=timestamp,
                )
            )

    return updates
