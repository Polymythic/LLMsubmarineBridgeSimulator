import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

# Load .env at startup
load_dotenv()


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
    use_enemy_ai: bool = _get_env_bool("USE_ENEMY_AI", False)
    enemy_static: bool = _get_env_bool("ENEMY_STATIC", True)
    # AI Orchestrator / Agents
    use_ai_orchestrator: bool = _get_env_bool("USE_AI_ORCHESTRATOR", False)
    ai_fleet_engine: str = os.getenv("AI_FLEET_ENGINE", "stub")  # stub|ollama|openai
    ai_ship_engine: str = os.getenv("AI_SHIP_ENGINE", "stub")    # stub|ollama|openai
    ai_fleet_model: str = os.getenv("AI_FLEET_MODEL", "stub")
    ai_ship_model: str = os.getenv("AI_SHIP_MODEL", "stub")
    # Agent cadences (seconds)
    ai_fleet_cadence_s: float = _get_env_float("AI_FLEET_CADENCE_S", 45.0)
    ai_ship_cadence_s: float = _get_env_float("AI_SHIP_CADENCE_S", 20.0)
    ai_ship_alert_cadence_s: float = _get_env_float("AI_SHIP_ALERT_CADENCE_S", 10.0)
    # Engines configuration
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    openai_api_key: Optional[str] = os.getenv("OPENAI_API_KEY")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    # Maintenance/task tuning
    first_task_delay_s: float = _get_env_float("FIRST_TASK_DELAY_S", 30.0)
    maint_spawn_scale: float = _get_env_float("MAINT_SPAWN_SCALE", 1.0)


CONFIG = Config()


def reload_from_env() -> Config:
    """Reload environment variables from .env and rebuild CONFIG.

    Returns the new CONFIG instance.
    """
    load_dotenv(override=True)
    global CONFIG
    CONFIG = Config()
    return CONFIG
