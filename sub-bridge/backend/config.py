import os
from dataclasses import dataclass
from typing import Optional


def _get_env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.lower() in {"1", "true", "yes", "on"}


def _get_env_float(key: str, default: float) -> float:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return float(val)
    except Exception:
        return default


@dataclass(frozen=True)
class Config:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    tick_hz: int = int(os.getenv("TICK_HZ", "20"))
    use_redis: bool = _get_env_bool("USE_REDIS", False)
    redis_url: Optional[str] = os.getenv("REDIS_URL")
    ai_poll_s: float = _get_env_float("AI_POLL_S", 2.0)
    snapshot_s: float = _get_env_float("SNAPSHOT_S", 2.0)
    require_captain_consent: bool = _get_env_bool("REQUIRE_CAPTAIN_CONSENT", True)
    sqlite_path: str = os.getenv("SQLITE_PATH", "./sub-bridge.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


CONFIG = Config()
