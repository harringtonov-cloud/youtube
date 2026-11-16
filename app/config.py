"""Конфигурация из переменных окружения / файла .env (без внешних зависимостей)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Браузер имеет право ходить ТОЛЬКО на эти хосты (песочница на этом же сервере).
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    admin_token: str = ""
    token_was_generated: bool = False
    data_dir: Path = BASE_DIR / "data"
    max_concurrent_runs: int = 1
    max_attempts: int = 3
    action_timeout_ms: int = 10_000
    scenario_timeout_s: int = 90
    sandbox_view_threshold_ms: int = 2000
    cookie_secure: bool = False
    log_level: str = "info"
    # --- YouTube Data API / Google OAuth ---
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""  # для веб-входа (HTTPS), напр. https://example.com/oauth/callback
    youtube_api_key: str = ""  # необязательно: чтение публичных данных без входа
    yt_poll_interval_min: int = 30
    yt_write_limit_per_day: int = 20
    yt_daily_quota: int = 10_000
    # Адреса Google. Меняются только в автотестах (тестовый двойник Google).
    google_oauth_base: str = "https://oauth2.googleapis.com"
    google_auth_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    youtube_api_base: str = "https://www.googleapis.com/youtube/v3"
    # Секрет песочницы: выдаётся только браузеру Playwright (cookie), живёт в памяти.
    sandbox_key: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "history.sqlite3"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def sandbox_base_url(self) -> str:
        """Браузер всегда обращается к песочнице по loopback — наружу не выходит."""
        return f"http://127.0.0.1:{self.port}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(BASE_DIR / ".env")

    token = os.environ.get("ADMIN_TOKEN", "").strip()
    generated = False
    if not token:
        token = secrets.token_urlsafe(18)
        generated = True

    data_dir = Path(os.environ.get("DATA_DIR", "") or (BASE_DIR / "data"))
    if not data_dir.is_absolute():
        data_dir = (BASE_DIR / data_dir).resolve()

    settings = Settings(
        host=os.environ.get("HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=_env_int("PORT", 8000),
        admin_token=token,
        token_was_generated=generated,
        data_dir=data_dir,
        max_concurrent_runs=_env_int("MAX_CONCURRENT_RUNS", 1),
        max_attempts=_env_int("MAX_ATTEMPTS", 3),
        action_timeout_ms=_env_int("ACTION_TIMEOUT_MS", 10_000, 1000),
        scenario_timeout_s=_env_int("SCENARIO_TIMEOUT_S", 90, 10),
        sandbox_view_threshold_ms=_env_int("SANDBOX_VIEW_THRESHOLD_MS", 2000, 500),
        cookie_secure=_env_bool("COOKIE_SECURE", False),
        log_level=(os.environ.get("LOG_LEVEL", "info").strip().lower() or "info"),
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
        google_redirect_uri=os.environ.get("GOOGLE_REDIRECT_URI", "").strip(),
        youtube_api_key=os.environ.get("YOUTUBE_API_KEY", "").strip(),
        yt_poll_interval_min=_env_int("YT_POLL_INTERVAL_MIN", 30, 5),
        yt_write_limit_per_day=_env_int("YT_WRITE_LIMIT_PER_DAY", 20, 1),
        yt_daily_quota=_env_int("YT_DAILY_QUOTA", 10_000, 100),
        google_oauth_base=os.environ.get("GOOGLE_OAUTH_BASE", "").strip().rstrip("/")
        or "https://oauth2.googleapis.com",
        google_auth_url=os.environ.get("GOOGLE_AUTH_URL", "").strip()
        or "https://accounts.google.com/o/oauth2/v2/auth",
        youtube_api_base=os.environ.get("YOUTUBE_API_BASE", "").strip().rstrip("/")
        or "https://www.googleapis.com/youtube/v3",
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
    return settings
