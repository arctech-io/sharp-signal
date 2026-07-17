"""Talks to the TxLINE API to fetch signal data."""


async def fetch_signals() -> list[dict]:
    """Fetch signals from TxLINE API."""
    raise NotImplementedError


async def parse_response(raw: dict) -> list[dict]:
    """Parse TxLINE API response into normalized signal dicts."""
    raise NotImplementedError
