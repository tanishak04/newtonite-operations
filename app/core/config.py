import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "Newtonite Operations Under Pressure"
    API_V1_PREFIX: str = "/api/v1"
    PORT: int = 8000
    HOST: str = "0.0.0.0"
    DEBUG: bool = False
    SECRET_KEY: str = "change-this-in-production-super-secret-key-at-least-32-chars"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 1 day

    # Database: Supports SQLite (aiosqlite) or PostgreSQL (asyncpg)
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "sqlite+aiosqlite:///./newtonite_ops.db"
    )

    OUTBOX_POLL_INTERVAL_SECONDS: float = 2.0
    OUTBOX_MAX_RETRIES: int = 5

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
