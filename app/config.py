"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Load settings from .env file and environment."""

    txline_api_key: str = ""
    txline_api_token: str = ""
    txline_base_url: str = ""
    poll_interval_seconds: int = 60
    database_url: str = "sqlite:///./sharp_signal.db"
    z_score_threshold: float = 2.0
    pct_change_threshold: float = 5.0
    rolling_window_size: int = 20
    min_window_size: int = 5

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
