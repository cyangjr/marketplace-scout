from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    discord_bot_token: str = ""
    discord_channel_id: str = ""
    discord_allowed_user_ids: str = ""

    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    home_zip: str = "10001"
    default_max_miles: float = 25.0
    default_poll_minutes: int = 15

    database_path: str = "data/scout.db"
    playwright_profile_dir: str = "data/playwright_profile"
    host: str = "127.0.0.1"
    port: int = 8765

    confidence_alert_threshold: float = 0.75
    confidence_ambiguous_low: float = 0.4
    confidence_ambiguous_high: float = 0.75

    # Facebook pacing / circuit breaker
    fb_poll_minutes: int = 30
    fb_stagger_seconds: float = 45.0
    fb_circuit_hours: float = 12.0
    fb_headless: bool = True
    fb_timezone: str = "America/New_York"
    fb_wait_min_seconds: float = 2.0
    fb_wait_max_seconds: float = 6.0
    fb_max_results: int = 25

    # Craigslist public HTML search (no sapi.craigslist.org)
    cl_max_pages: int = 3
    cl_max_results: int = 120
    cl_detail_limit: int = 15
    cl_detail_delay_seconds: float = 0.4

    @property
    def allowed_user_ids(self) -> set[int]:
        if not self.discord_allowed_user_ids.strip():
            return set()
        return {
            int(x.strip())
            for x in self.discord_allowed_user_ids.split(",")
            if x.strip().isdigit()
        }

    @property
    def db_path(self) -> Path:
        return Path(self.database_path)

    @property
    def playwright_dir(self) -> Path:
        return Path(self.playwright_profile_dir)


@lru_cache
def get_settings() -> Settings:
    return Settings()
