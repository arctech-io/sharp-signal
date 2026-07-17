"""Database query helpers."""

from sqlalchemy.ext.asyncio import AsyncSession


async def save_signal(session: AsyncSession, signal: dict) -> None:
    """Persist a signal record to the database."""
    raise NotImplementedError


async def get_signals(session: AsyncSession, limit: int = 50) -> list[dict]:
    """Retrieve recent signals from the database."""
    raise NotImplementedError
