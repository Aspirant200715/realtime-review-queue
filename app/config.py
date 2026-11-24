import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database configuration
    database_url: Optional[str] = None
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    postgres_db: str = "review_queue"
    postgres_socket_dir: Optional[str] = None

    db_min_connections: int = 5
    db_max_connections: int = 30

    # Queue & Lease parameters
    lease_duration_seconds: float = 8.0
    heartbeat_interval_seconds: float = 2.5
    sweeper_interval_seconds: float = 1.0

    # Reviewer bounds
    max_reviewers: int = 6

    # Load Generator configuration
    generator_steady_rate: float = 5.0          # items/sec
    generator_burst_size: int = 250             # items per burst
    generator_burst_duration: float = 8.0       # duration in seconds (< 10s requirement)
    generator_min_burst_interval: float = 35.0  # seconds between random bursts
    generator_max_burst_interval: float = 75.0  # seconds between random bursts
    generator_auto_start: bool = False

    # Environment
    environment: str = "development"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def __init__(self, **values):
        super().__init__(**values)
        local_socket = "/home/aspirant/Projects/realtime-review-queue/.tools/run"
        if not self.postgres_socket_dir and not self.database_url and os.path.exists(local_socket) and not os.environ.get("POSTGRES_HOST"):
            self.postgres_socket_dir = local_socket
            self.postgres_port = 54329

    def get_dsn(self) -> str:
        if self.database_url:
            return self.database_url
        if self.postgres_socket_dir:
            return f"postgresql://{self.postgres_user}:{self.postgres_password}@/{self.postgres_db}?host={self.postgres_socket_dir}&port={self.postgres_port}"
        return f"postgresql://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"


settings = Settings()
