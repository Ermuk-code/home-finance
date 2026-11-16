from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    BOT_TOKEN: str

    # --- Пользователи (Life OS: два человека в семье) ---
    ADMIN_TELEGRAM_ID: Optional[int] = None
    ADMIN_NAME: str = "Admin"
    # Второй участник (role='user'). Если задан — сид-скрипт заведёт его в БД.
    PARTNER_TELEGRAM_ID: Optional[int] = None
    PARTNER_NAME: str = "Partner"

    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str
    POSTGRES_HOST: str
    POSTGRES_PORT: int = 5432

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ADMIN_USERNAME: str
    ADMIN_PASSWORD: str

    # Публичный адрес веб-панели: нужен, чтобы бот мог выдать ссылку для входа (/web)
    WEB_BASE_URL: str = "http://localhost:8000"

    # Секрет для вебхуков Home Assistant (заголовок X-Webhook-Token).
    # Пусто — вебхуки отключены (эндпоинт отвечает 503).
    HEALTH_WEBHOOK_TOKEN: Optional[str] = None
    # Присылать admin-у уведомление «партнёр взвесился» (без значения веса), когда вес пришёл не от admin
    WEIGHT_ADMIN_DIGEST: bool = False

    # Суточная активность и вечерние напоминания (локальное время семьи).
    ACTIVITY_TZ: str = "Europe/Moscow"
    ACTIVITY_NUDGE_ENABLED: bool = False
    ACTIVITY_NUDGE_STEPS: int = Field(default=5000, gt=0, le=2147483647)

    @field_validator("ACTIVITY_TZ")
    @classmethod
    def _validate_activity_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("ACTIVITY_TZ должен быть часовым поясом IANA, например Europe/Moscow") from exc
        return value

    # FatSecret Platform API (OAuth 1.0a): Consumer Key / Shared Secret из developer console.
    # Пусто — интеграция выключена. Пользовательские методы (food_entries.get) работают только по OAuth 1.0a.
    FATSECRET_CONSUMER_KEY: Optional[str] = None
    FATSECRET_CONSUMER_SECRET: Optional[str] = None
    # Куда FatSecret вернёт пользователя после «Разрешить». По умолчанию WEB_BASE_URL + /api/health/fatsecret/callback
    FATSECRET_CALLBACK_URL: Optional[str] = None
    # Часовой пояс «сегодняшнего дня» для дневника питания и расписания синхронизации
    FATSECRET_TZ: str = "Europe/Moscow"

    @property
    def fatsecret_callback_url(self) -> str:
        return self.FATSECRET_CALLBACK_URL or f"{self.WEB_BASE_URL.rstrip('/')}/api/health/fatsecret/callback"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
