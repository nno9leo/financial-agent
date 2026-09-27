from pathlib import Path

from pydantic import Field, field_validator
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_DATABASE_URL = "postgresql://financial_research:financial_research@127.0.0.1:5432/financial_research"


class Settings(BaseSettings):
    openrouter_api_key: SecretStr | None = None
    sec_user_agent: str | None = None
    data_dir: Path = Path("data")
    reports_dir: Path = Path("artifacts/reports")
    request_timeout: float = 20.0
    api_port: int = 8765
    max_retries: int = 3
    min_request_interval: float = 0.15
    fastembed_cache_dir: Path = Path("data/models")
    embedding_model_id: str = "BAAI/bge-small-en-v1.5"
    embedding_query_prefix: str = ""
    chunk_target_tokens: int = 350
    chunk_max_tokens: int = 512
    database_url: str = Field(default=LOCAL_DATABASE_URL, description="Local development database URL only; never log unmasked credentials.")

    @field_validator("database_url", mode="before")
    @classmethod
    def empty_database_url_uses_local_default(cls, value):
        return LOCAL_DATABASE_URL if value is None or value == "" else value

    @field_validator("openrouter_api_key", mode="before")
    @classmethod
    def empty_openrouter_key_is_unset(cls, value):
        return None if value is None or value == "" else value

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
