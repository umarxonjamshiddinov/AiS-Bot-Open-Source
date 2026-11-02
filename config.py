from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "aiscloudbot.sqlite3"


def database_path() -> Path:
    raw = os.getenv("SQLITE_PATH", str(DEFAULT_DATABASE_PATH)).strip()
    if not raw or raw == ":memory:" or raw.startswith("file:"):
        raise RuntimeError("SQLITE_PATH must name a persistent filesystem database")
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def _admin_ids() -> frozenset[int]:
    try:
        ids = frozenset(int(value.strip()) for value in os.getenv("ADMIN_USER_IDS", "").split(",") if value.strip())
    except ValueError as error:
        raise RuntimeError("ADMIN_USER_IDS must contain comma-separated Telegram user IDs") from error
    if any(user_id <= 0 for user_id in ids):
        raise RuntimeError("Admin IDs must be positive")
    return ids


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if value < 1:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


def _nonnegative_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if value < 0:
        raise RuntimeError(f"{name} cannot be negative")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number") from error
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise RuntimeError(f"{name} must be true or false")
    return value in {"true", "1"}


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str = field(repr=False)
    ais_api_key: str = field(repr=False)
    ais_api_base_url: str = field(repr=False)
    tavily_api_key: str = field(repr=False)
    support_contact: str = ""
    ai_max_concurrent_requests: int = 4
    ai_default_max_output_tokens: int = 4096
    ai_retry_count: int = 0
    ai_request_timeout_seconds: float = 120.0
    safety_shadow_enabled: bool = True
    safety_shadow_queue_size: int = 32
    safety_shadow_timeout_seconds: float = 20.0
    sqlite_path: Path = DEFAULT_DATABASE_PATH
    sqlite_busy_timeout_ms: int = 5000
    admin_user_ids: frozenset[int] = field(default_factory=frozenset)
    usage_retention_days: int = 90
    moderation_retention_days: int = 90
    audit_retention_days: int = 365
    standard_daily_limit: int = 50
    trusted_daily_limit: int = 500
    higher_cost_daily_limit: int = 15
    grok_access_code: str = field(default="", repr=False)
    premium_activation_code: str = field(default="", repr=False)
    activation_max_attempts: int = 5
    activation_window_seconds: int = 900
    activation_cooldown_seconds: int = 900

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")
        required_names = (
            "BOT_TOKEN",
            "AIS_API_KEY",
            "AIS_API_BASE_URL",
            "TAVILY_API_KEY",
        )
        values = {name: os.getenv(name) for name in required_names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        return cls(
            bot_token=values["BOT_TOKEN"] or "",
            ais_api_key=values["AIS_API_KEY"] or "",
            ais_api_base_url=values["AIS_API_BASE_URL"] or "",
            tavily_api_key=values["TAVILY_API_KEY"] or "",
            support_contact=os.getenv("SUPPORT_CONTACT", "").strip(),
            ai_max_concurrent_requests=_positive_int(
                "AI_MAX_CONCURRENT_REQUESTS", 4
            ),
            ai_default_max_output_tokens=_positive_int(
                "AI_DEFAULT_MAX_OUTPUT_TOKENS", 4096
            ),
            ai_retry_count=_nonnegative_int("AI_RETRY_COUNT", 0),
            ai_request_timeout_seconds=_positive_float(
                "AI_REQUEST_TIMEOUT_SECONDS", 120.0
            ),
            safety_shadow_enabled=_boolean("SAFETY_SHADOW_ENABLED", True),
            safety_shadow_queue_size=_positive_int("SAFETY_SHADOW_QUEUE_SIZE", 32),
            safety_shadow_timeout_seconds=_positive_float("SAFETY_SHADOW_TIMEOUT_SECONDS", 20.0),
            sqlite_path=database_path(),
            sqlite_busy_timeout_ms=_positive_int("SQLITE_BUSY_TIMEOUT_MS", 5000),
            admin_user_ids=_admin_ids(),
            usage_retention_days=_positive_int("USAGE_RETENTION_DAYS", 90),
            moderation_retention_days=_positive_int("MODERATION_RETENTION_DAYS", 90),
            audit_retention_days=_positive_int("AUDIT_RETENTION_DAYS", 365),
            standard_daily_limit=_positive_int("STANDARD_DAILY_LIMIT", 50),
            trusted_daily_limit=_nonnegative_int("TRUSTED_DAILY_LIMIT", 500),
            higher_cost_daily_limit=_positive_int("HIGHER_COST_DAILY_LIMIT", 15),
            grok_access_code=os.getenv("GROK_ACCESS_CODE", ""),
            premium_activation_code=os.getenv("PREMIUM_ACTIVATION_CODE", ""),
            activation_max_attempts=_positive_int("ACTIVATION_MAX_ATTEMPTS", 5),
            activation_window_seconds=_positive_int("ACTIVATION_WINDOW_SECONDS", 900),
            activation_cooldown_seconds=_positive_int("ACTIVATION_COOLDOWN_SECONDS", 900),
        )
