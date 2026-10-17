import re
from pathlib import Path
from typing import Annotated

from pydantic import Field, StringConstraints, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

OwnerID = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,18}$")]
ChannelID = Annotated[str, StringConstraints(pattern=r"^-100[0-9]{1,16}$")]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EHTG_", env_file=".env", extra="forbid")

    owner_id: OwnerID
    channel_id: ChannelID
    database_path: Path = Path("data/db/app.sqlite3")
    allowed_origins: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    root_path: str = ""
    max_pages: int = Field(default=2000, ge=1, le=2000)
    max_queue: int = Field(default=100, ge=1, le=1000)
    max_body_bytes: int = Field(default=16384, ge=1024, le=1048576)
    pairing_attempts_per_minute: int = Field(default=10, ge=1, le=60)
    source_enabled: bool = False
    source_session_path: Path = Path("secrets/source-session.json")
    source_request_interval: float = Field(default=3, ge=1, le=60)
    source_request_timeout: float = Field(default=20, ge=1, le=60)
    source_max_html_bytes: int = Field(default=2097152, ge=65536, le=8388608)
    source_max_index_pages: int = Field(default=200, ge=1, le=2000)
    source_memory_mb: int = Field(default=256, ge=128, le=512)
    analysis_timeout: float = Field(default=900, ge=30, le=3600)
    analysis_lease_seconds: float = Field(default=30, ge=5, le=300)
    analysis_max_attempts: int = Field(default=3, ge=1, le=10)
    analysis_retry_seconds: float = Field(default=5, ge=1, le=60)

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, origins: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(r"chrome-extension://[a-p]{32}", origin) is None for origin in origins):
            raise ValueError("Only exact Chromium extension origins are allowed")
        return origins

    @field_validator("allowed_hosts")
    @classmethod
    def validate_hosts(cls, hosts: tuple[str, ...]) -> tuple[str, ...]:
        if not hosts or any(not host or "*" in host or "/" in host for host in hosts):
            raise ValueError("Configure explicit trusted host names")
        return hosts

    @field_validator("root_path")
    @classmethod
    def validate_root_path(cls, path: str) -> str:
        if path and (not path.startswith("/") or path.endswith("/") or "//" in path):
            raise ValueError("root_path must be empty or a path such as /ehentai-to-tg")
        return path
