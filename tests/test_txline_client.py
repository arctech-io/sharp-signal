"""Tests for the TxLINE ingestion client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.ingestion.txline_client import TxLineClient, TxLineError, _normalize_odds
from app.storage.models import OddsUpdate


# ---------------------------------------------------------------------------
# Sample API responses
# ---------------------------------------------------------------------------

SAMPLE_FIXTURES = [
    {
        "FixtureId": 12345678,
        "StartTime": 1700000000000,
        "Competition": "World Cup 2026",
        "CompetitionId": 500005,
        "Participant1": "Brazil",
        "Participant2": "Germany",
        "Participant1IsHome": True,
    },
    {
        "FixtureId": 87654321,
        "StartTime": 1700003600000,
        "Competition": "World Cup 2026",
        "CompetitionId": 500005,
        "Participant1": "France",
        "Participant2": "Argentina",
        "Participant1IsHome": False,
    },
]

SAMPLE_ODDS = [
    {
        "FixtureId": 12345678,
        "MessageId": "msg-001",
        "Ts": 1700000000000,
        "Bookmaker": "StablePrice",
        "BookmakerId": 1,
        "SuperOddsType": "1X2",
        "InRunning": True,
        "PriceNames": ["Home", "Draw", "Away"],
        "Prices": [2100, 3400, 3500],
        "Pct": ["47.619", "29.412", "28.571"],
    },
    {
        "FixtureId": 12345678,
        "MessageId": "msg-002",
        "Ts": 1700000000000,
        "Bookmaker": "StablePrice",
        "BookmakerId": 1,
        "SuperOddsType": "Over/Under 2.5",
        "InRunning": True,
        "PriceNames": ["Over", "Under"],
        "Prices": [1850, 1950],
        "Pct": ["54.054", "51.282"],
    },
]


# ---------------------------------------------------------------------------
# Normalization tests
# ---------------------------------------------------------------------------


def test_normalize_odds_basic():
    """_normalize_odds converts raw payloads into OddsUpdate models."""
    updates = _normalize_odds(12345678, SAMPLE_ODDS)

    assert len(updates) == 5  # 3 from 1X2 + 2 from Over/Under

    h = updates[0]
    assert isinstance(h, OddsUpdate)
    assert h.match_id == "12345678"
    assert h.market == "1X2"
    assert h.selection == "Home"
    assert h.odds_value == pytest.approx(2.1)
    assert h.timestamp.year == 2023

    ou = updates[3]
    assert ou.market == "Over/Under 2.5"
    assert ou.selection == "Over"
    assert ou.odds_value == pytest.approx(1.85)


def test_normalize_odds_empty_list():
    """_normalize_odds with empty payloads returns empty list."""
    assert _normalize_odds(123, []) == []


def test_normalize_odds_mismatched_lengths():
    """_normalize_odds handles PriceNames/Prices length mismatch gracefully."""
    payload = [
        {
            "FixtureId": 1,
            "Ts": 1700000000000,
            "SuperOddsType": "1X2",
            "PriceNames": ["Home", "Draw", "Away"],
            "Prices": [2100],  # only one price for three names
        }
    ]
    updates = _normalize_odds(1, payload)
    assert len(updates) == 3
    assert updates[0].odds_value == 2.1
    assert updates[1].odds_value == 0.0  # no price for this index
    assert updates[2].odds_value == 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(status_code=200, json_data=None, text=""):
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text or str(json_data)
    if json_data is not None:
        resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        err = httpx.HTTPStatusError(
            message=f"{status_code}",
            request=MagicMock(),
            response=resp,
        )
        resp.raise_for_status.side_effect = err
    return resp


def _make_mock_client(*responses):
    """Build a mock httpx.AsyncClient whose request() returns given responses sequentially."""
    mock = AsyncMock(spec=httpx.AsyncClient)
    mock.request = AsyncMock(side_effect=list(responses))
    return mock


# ---------------------------------------------------------------------------
# Client tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_fetch_all_odds():
    """fetch_all_odds returns normalized OddsUpdate list on success."""
    fixtures_resp = _mock_response(200, json_data=SAMPLE_FIXTURES[:1])
    odds_resp = _mock_response(200, json_data=SAMPLE_ODDS)

    mock = _make_mock_client(fixtures_resp, odds_resp)

    client = TxLineClient(
        base_url="https://txline.test.com",
        jwt="test-jwt",
        api_token="test-token",
    )
    client._client = mock

    results = await client.fetch_all_odds()

    assert len(results) == 5
    assert all(isinstance(o, OddsUpdate) for o in results)
    assert results[0].match_id == "12345678"
    assert results[0].market == "1X2"
    assert results[0].selection == "Home"
    assert results[0].odds_value == pytest.approx(2.1)


@pytest.mark.asyncio
async def test_timeout_retries_then_succeeds():
    """Client retries on timeout and succeeds on the second attempt."""
    fixtures_resp = _mock_response(200, json_data=[])
    timeout_err = httpx.TimeoutException("timeout")

    mock = _make_mock_client(timeout_err, fixtures_resp)

    client = TxLineClient(
        base_url="https://txline.test.com",
        jwt="jwt",
        api_token="tok",
    )
    client._client = mock

    with patch("app.ingestion.txline_client.asyncio.sleep", new_callable=AsyncMock):
        results = await client.get_fixtures()

    assert results == []


@pytest.mark.asyncio
async def test_timeout_all_retries_exhausted():
    """Client raises TxLineError after exhausting all retry attempts."""
    mock = _make_mock_client(
        httpx.TimeoutException("t"),
        httpx.TimeoutException("t"),
        httpx.TimeoutException("t"),
    )

    client = TxLineClient(
        base_url="https://txline.test.com",
        jwt="jwt",
        api_token="tok",
    )
    client._client = mock

    with patch("app.ingestion.txline_client.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(TxLineError, match="All 3 attempts failed"):
            await client.get_fixtures()


@pytest.mark.asyncio
async def test_malformed_json_response():
    """Client raises on invalid JSON from the API."""
    bad_resp = _mock_response(200, text="<<<not json>>>")
    bad_resp.json.side_effect = ValueError("Expecting value")

    mock = _make_mock_client(bad_resp)

    client = TxLineClient(
        base_url="https://txline.test.com",
        jwt="jwt",
        api_token="tok",
    )
    client._client = mock

    with pytest.raises(ValueError, match="Expecting value"):
        await client.get_fixtures()


@pytest.mark.asyncio
async def test_empty_fixtures_list():
    """Client returns empty list when API returns no fixtures."""
    empty_resp = _mock_response(200, json_data=[])

    mock = _make_mock_client(empty_resp, empty_resp)

    client = TxLineClient(
        base_url="https://txline.test.com",
        jwt="jwt",
        api_token="tok",
    )
    client._client = mock

    fixtures = await client.get_fixtures()
    assert fixtures == []

    odds = await client.fetch_all_odds()
    assert odds == []


@pytest.mark.asyncio
async def test_client_error_no_retry():
    """4xx errors raise immediately without retrying."""
    error_resp = _mock_response(401, text="Unauthorized")

    mock = _make_mock_client(error_resp)

    client = TxLineClient(
        base_url="https://txline.test.com",
        jwt="jwt",
        api_token="tok",
    )
    client._client = mock

    with pytest.raises(TxLineError, match="Client error 401"):
        await client.get_fixtures()

    assert mock.request.call_count == 1


@pytest.mark.asyncio
async def test_5xx_retries_then_fails():
    """Server errors retry and eventually raise TxLineError."""
    server_err = _mock_response(502, text="Bad Gateway")

    mock = _make_mock_client(server_err, server_err, server_err)

    client = TxLineClient(
        base_url="https://txline.test.com",
        jwt="jwt",
        api_token="tok",
    )
    client._client = mock

    with patch("app.ingestion.txline_client.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(TxLineError, match="All 3 attempts failed"):
            await client.get_fixtures()

    assert mock.request.call_count == 3


@pytest.mark.asyncio
async def test_context_manager_required():
    """Calling methods without entering context raises RuntimeError."""
    client = TxLineClient(
        base_url="https://txline.test.com",
        jwt="jwt",
        api_token="tok",
    )
    with pytest.raises(RuntimeError, match="async context manager"):
        await client.get_fixtures()


@pytest.mark.asyncio
async def test_odds_fetch_skips_failed_fixtures():
    """fetch_all_odds skips fixtures whose odds request fails."""
    fixtures_resp = _mock_response(200, json_data=SAMPLE_FIXTURES)
    odds_error = _mock_response(500, text="Internal Server Error")
    odds_error.raise_for_status.side_effect = httpx.HTTPStatusError(
        message="500", request=MagicMock(), response=odds_error
    )
    odds_ok = _mock_response(200, json_data=SAMPLE_ODDS)

    # call 1: fixtures
    # calls 2-4: odds for fixture 1 (500, retried 3 times)
    # call 5: odds for fixture 2 (200)
    mock = _make_mock_client(
        fixtures_resp, odds_error, odds_error, odds_error, odds_ok
    )

    client = TxLineClient(
        base_url="https://txline.test.com",
        jwt="jwt",
        api_token="tok",
    )
    client._client = mock

    with patch("app.ingestion.txline_client.asyncio.sleep", new_callable=AsyncMock):
        results = await client.fetch_all_odds()

    # Only odds from the second fixture (5 odds) should come through
    assert len(results) == 5
    assert all(o.match_id == "87654321" for o in results)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5xx_succeeds_on_retry():
    """Server error on first attempt, success on second — no exception."""
    server_err = _mock_response(502, text="Bad Gateway")
    ok_resp = _mock_response(200, json_data=[])

    mock = _make_mock_client(server_err, ok_resp)

    client = TxLineClient(base_url="https://t.test", jwt="j", api_token="t")
    client._client = mock

    with patch("app.ingestion.txline_client.asyncio.sleep", new_callable=AsyncMock):
        result = await client.get_fixtures()

    assert result == []
    assert mock.request.call_count == 2


@pytest.mark.asyncio
async def test_connection_error_retries():
    """Network connection error triggers retry."""
    conn_err = httpx.ConnectError("Connection refused")
    ok_resp = _mock_response(200, json_data=[])

    mock = _make_mock_client(conn_err, ok_resp)

    client = TxLineClient(base_url="https://t.test", jwt="j", api_token="t")
    client._client = mock

    with patch("app.ingestion.txline_client.asyncio.sleep", new_callable=AsyncMock):
        result = await client.get_fixtures()

    assert result == []


@pytest.mark.asyncio
async def test_fixture_missing_fixture_id_skipped():
    """Fixture without FixtureId is skipped in fetch_all_odds."""
    fixtures = [{"Competition": "WC"}]  # no FixtureId
    fixtures_resp = _mock_response(200, json_data=fixtures)
    empty_resp = _mock_response(200, json_data=[])

    mock = _make_mock_client(fixtures_resp, empty_resp)

    client = TxLineClient(base_url="https://t.test", jwt="j", api_token="t")
    client._client = mock

    result = await client.fetch_all_odds()
    assert result == []


def test_normalize_odds_missing_supertype():
    """Missing SuperOddsType defaults to 'unknown'."""
    payload = [{"FixtureId": 1, "Ts": 1000, "PriceNames": ["X"], "Prices": [1500]}]
    updates = _normalize_odds(1, payload)
    assert updates[0].market == "unknown"


def test_normalize_odds_none_pricenames():
    """None PriceNames treated as empty list."""
    payload = [{"FixtureId": 1, "Ts": 1000, "SuperOddsType": "1X2", "PriceNames": None, "Prices": None}]
    assert _normalize_odds(1, payload) == []


def test_normalize_odds_zero_timestamp():
    """Ts=0 produces a valid datetime (epoch)."""
    payload = [{"FixtureId": 1, "Ts": 0, "PriceNames": ["A"], "Prices": [1000]}]
    updates = _normalize_odds(1, payload)
    assert updates[0].timestamp.year == 1970


def test_normalize_odds_includes_line_and_period():
    """Different Over/Under lines are tracked as separate markets.

    TxLINE returns many lines (2.5, 3.5, ...) under one SuperOddsType. The
    market key must include the line/period so detection doesn't merge them
    into a single (false) series.
    """
    payload = [
        {"FixtureId": 1, "Ts": 1000, "SuperOddsType": "OVERUNDER_PARTICIPANT_GOALS",
         "MarketParameters": "line=2.5", "PriceNames": ["over", "under"], "Prices": [1680, 2470]},
        {"FixtureId": 1, "Ts": 1000, "SuperOddsType": "OVERUNDER_PARTICIPANT_GOALS",
         "MarketParameters": "line=3.5", "PriceNames": ["over", "under"], "Prices": [4650, 1274]},
        {"FixtureId": 1, "Ts": 1000, "SuperOddsType": "OVERUNDER_PARTICIPANT_GOALS",
         "MarketParameters": "line=2.5", "MarketPeriod": "half=1",
         "PriceNames": ["over", "under"], "Prices": [1687, 2457]},
        {"FixtureId": 1, "Ts": 1000, "SuperOddsType": "1X2_PARTICIPANT_RESULT",
         "PriceNames": ["part1", "draw", "part2"], "Prices": [49300, 13400, 1105]},
    ]
    updates = _normalize_odds(1, payload)
    markets = {u.market for u in updates}
    assert "OVERUNDER_PARTICIPANT_GOALS line=2.5" in markets
    assert "OVERUNDER_PARTICIPANT_GOALS line=3.5" in markets
    assert "OVERUNDER_PARTICIPANT_GOALS line=2.5 half=1" in markets
    # 1X2 has no line/period, so it stays bare
    assert "1X2_PARTICIPANT_RESULT" in markets
    # under@2.5 vs under@3.5 are distinct series, not the same market
    under_25 = [u for u in updates if u.market.endswith("line=2.5") and u.selection == "under"]
    under_35 = [u for u in updates if u.market.endswith("line=3.5") and u.selection == "under"]
    assert len(under_25) == 1 and len(under_35) == 1
    assert under_25[0].odds_value == 2.47
    assert under_35[0].odds_value == 1.274


@pytest.mark.asyncio
async def test_context_manager_closes_client():
    """__aexit__ closes the httpx client."""
    client = TxLineClient(base_url="https://t.test", jwt="j", api_token="t")
    mock = AsyncMock(spec=httpx.AsyncClient)
    client._client = mock
    await client.__aexit__(None, None, None)
    mock.aclose.assert_awaited_once()
    assert client._client is None
