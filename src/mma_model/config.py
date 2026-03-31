"""Central settings (env + YAML flags)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    odds_api_key: str = Field(default="", alias="ODDS_API_KEY")
    mma_database_url: str = Field(default="sqlite:///data/mma.db", alias="MMA_DATABASE_URL")
    ufcstats_request_delay_sec: float = Field(default=0.75, alias="UFCSTATS_REQUEST_DELAY_SEC")
    ufcstats_user_agent: str = Field(
        default="mma-model/0.1 (+https://github.com/)", alias="UFCSTATS_USER_AGENT"
    )
    project_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[2])


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_yaml_flags(name: str) -> dict[str, Any]:
    path = get_settings().project_root / name
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def feature_flags() -> dict[str, Any]:
    return load_yaml_flags("feature_flags.yaml")


def profile(name: str) -> dict[str, Any]:
    data = load_yaml_flags("profiles.yaml")
    return data.get(name, data.get("default", {}))
