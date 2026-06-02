from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    writer_model: str
    writer_temperature: float | None = Field(default=None, ge=0)
    writer_top_p: float | None = Field(default=None, ge=0, le=1)
    writer_agent_mode: str
    writer_frontend_origin: str
    openai_api_key: str
    openai_base_url: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
