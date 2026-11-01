"""Settings from environment (.env) plus per-source config from sources.yaml."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_user: str = "carscout"
    postgres_password: str = "carscout"
    postgres_db: str = "carscout"
    postgres_host: str = "db"
    postgres_port: int = 5432
    database_url_override: str | None = Field(default=None, alias="DATABASE_URL")

    web_port: int = 8000  # port inside the container; host port is CARSCOUT_PORT
    scrape_cron: str = "0 */6 * * *"
    schedule_tz: str = "Australia/Melbourne"

    default_delay_seconds: float = 4.0
    default_jitter_seconds: float = 3.0
    default_concurrency: int = 1
    max_retries: int = 3
    backoff_base_seconds: float = 10.0
    respect_robots: bool = True
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    )

    enabled_sources: str = ""
    sources_file: Path = Path("config/sources.yaml")
    log_level: str = "INFO"

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def enabled_allowlist(self) -> set[str] | None:
        names = {n.strip() for n in self.enabled_sources.split(",") if n.strip()}
        return names or None


class SourceConfig(BaseModel):
    name: str
    label: str
    enabled: bool = True
    implemented: bool = True
    use_browser: bool = False
    delay: float
    jitter: float
    concurrency: int
    options: dict[str, Any] = Field(default_factory=dict)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_sources(settings: Settings | None = None) -> dict[str, SourceConfig]:
    """Read sources.yaml fresh (it is re-read every run so edits apply live)."""
    settings = settings or get_settings()
    raw = yaml.safe_load(settings.sources_file.read_text()) or {}
    allow = settings.enabled_allowlist
    out: dict[str, SourceConfig] = {}
    for name, entry in (raw.get("sources") or {}).items():
        entry = entry or {}
        enabled = bool(entry.get("enabled", True))
        if allow is not None:
            enabled = name in allow
        out[name] = SourceConfig(
            name=name,
            label=entry.get("label", name.title()),
            enabled=enabled,
            implemented=entry.get("implemented", True),
            use_browser=entry.get("use_browser", False),
            delay=entry.get("delay", settings.default_delay_seconds),
            jitter=entry.get("jitter", settings.default_jitter_seconds),
            concurrency=entry.get("concurrency", settings.default_concurrency),
            options=entry.get("options") or {},
        )
    return out
