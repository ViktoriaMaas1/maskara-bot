"""
Централизованная конфигурация — единственный источник правды.

Принцип: все секреты и настройки читаются ИСКЛЮЧИТЕЛЬНО отсюда.
Нигде в коде не должно быть os.getenv() или hardcoded значений.

Pydantic Settings:
- Читает .env автоматически
- Валидирует типы на старте
- Если хоть одна обязательная переменная отсутствует — приложение не запустится
  (это лучше, чем падать в рантайме при первой сделке)
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import List

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# --------------------------------------------------------------
# Enum для окружения — защита от опечаток
# --------------------------------------------------------------
class AppEnv(str, Enum):
    development = "development"
    testnet = "testnet"
    production = "production"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


# --------------------------------------------------------------
# Главный класс настроек
# --------------------------------------------------------------
class Settings(BaseSettings):
    """
    Главный конфиг. Создаётся один раз через get_settings()
    и используется везде в приложении.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Игнорируем неизвестные переменные в .env
        # (например, переменные системы Linux не должны валить старт)
        extra="ignore",
    )

    # =========================================================
    # APP
    # =========================================================
    app_env: AppEnv = Field(default=AppEnv.development)
    log_level: LogLevel = Field(default=LogLevel.INFO)
    api_port: int = Field(default=8000, ge=1, le=65535)

    # =========================================================
    # WEBHOOK
    # =========================================================
    webhook_secret: SecretStr = Field(...)  # обязательное
    allowed_symbols: str = Field(default="BTCUSDT,ETHUSDT")
    webhook_rate_limit_per_min: int = Field(default=30, ge=1, le=1000)
    webhook_dedup_ttl_sec: int = Field(default=10, ge=1, le=300)

    # =========================================================
    # POSTGRES
    # =========================================================
    postgres_user: str = Field(...)
    postgres_password: SecretStr = Field(...)
    postgres_db: str = Field(...)
    postgres_host: str = Field(default="postgres")
    postgres_port: int = Field(default=5432, ge=1, le=65535)

    # =========================================================
    # REDIS
    # =========================================================
    redis_host: str = Field(default="redis")
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_password: SecretStr = Field(...)
    redis_db: int = Field(default=0, ge=0, le=15)

    # =========================================================
    # BYBIT (Stage 2)
    # =========================================================
    bybit_testnet: bool = Field(default=True)
    bybit_api_key: SecretStr = Field(default=SecretStr(""))
    bybit_api_secret: SecretStr = Field(default=SecretStr(""))
    bybit_readonly_mode: bool = Field(default=True)

    # ====================================================
    # RISK MANAGEMENT (Stage 3)
    # ====================================================
    # Kill switches (Stage 6/Telegram переключит на BotState в БД)
    kill_switch_enabled: bool = Field(default=False)
    bot_paused: bool = Field(default=False)

    # Лимиты на сделку
    min_final_score_trade: int = Field(default=70, ge=0, le=100)
    max_risk_per_trade: float = Field(default=0.01, gt=0, le=0.05)
    max_spread_bps: float = Field(default=5.0, gt=0)

    # Дневные лимиты
    max_daily_loss: float = Field(default=0.03, gt=0, le=0.5)
    max_consecutive_losses: int = Field(default=3, ge=1)

    # Лимиты позиций
    max_open_positions_per_symbol: int = Field(default=1, ge=1)

    # =========================================================
    # TELEGRAM (Stage 4)
    # =========================================================
    telegram_bot_token: SecretStr = Field(default=SecretStr(""))
    telegram_chat_id: str = Field(default="")

    # =========================================================
    # =========================================================
    # EXECUTION ENGINE (Stage 4)
    # =========================================================
    default_leverage: int = Field(default=5, ge=1, le=100)
    execution_order_link_id_prefix: str = Field(default="maskara")
    execution_max_retries: int = Field(default=3, ge=0, le=10)
    execution_retry_delay_sec: float = Field(default=1.0, ge=0.1, le=10.0)

    # =========================================================
    # WEBSOCKET (Stage 5)
    # =========================================================
    bybit_ws_enabled: bool = Field(default=True)
    bybit_ws_url_testnet: str = Field(default="wss://stream-testnet.bybit.com/v5/public/linear")
    bybit_ws_url_mainnet: str = Field(default="wss://stream.bybit.com/v5/public/linear")
    bybit_ws_orderbook_depth: int = Field(default=50, ge=1, le=500)
    bybit_ws_kline_intervals: str = Field(default="1,3,15,60")
    bybit_ws_reconnect_max_delay_sec: float = Field(default=60.0, ge=1.0, le=300.0)
    bybit_ws_reconnect_base_delay_sec: float = Field(default=1.0, ge=0.1, le=10.0)
    bybit_ws_ping_interval_sec: int = Field(default=20, ge=5, le=60)

    # =========================================================
    # MARKET CACHE (Stage 6)
    # =========================================================
    market_cache_enabled: bool = Field(default=True)
    market_cache_orderbook_ttl_sec: int = Field(default=60, ge=1, le=3600)
    market_cache_ticker_ttl_sec: int = Field(default=30, ge=1, le=3600)
    market_cache_trades_ttl_sec: int = Field(default=3600, ge=60, le=86400)
    market_cache_klines_ttl_sec: int = Field(default=86400, ge=300, le=604800)
    market_cache_liquidations_ttl_sec: int = Field(default=3600, ge=60, le=86400)
    market_cache_max_trades: int = Field(default=500, ge=10, le=10000)
    market_cache_max_klines: int = Field(default=200, ge=10, le=1000)
    market_cache_max_liquidations: int = Field(default=100, ge=10, le=1000)

    # EXTERNAL DATA (Stage 9)
    # =========================================================
    coinglass_api_key: SecretStr = Field(default=SecretStr(""))
    news_api_key: SecretStr = Field(default=SecretStr(""))

    # =========================================================
    # NEWS SENTIMENT / OpenAI (Stage 10 Phase 2)
    # =========================================================
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    openai_model: str = Field(default="gpt-4o-mini")
    news_sentiment_enabled: bool = Field(default=False)

    # =================================================================
    # DASHBOARD AUTH (Stage 4)
    # =================================================================
    dashboard_user: str = Field(default="")
    dashboard_password: SecretStr = Field(default=SecretStr(""))

    # =====================================================
    # SIGNALS (Stage 8)
    # =====================================================
    signal_polling_interval_sec: int = Field(default=5, ge=1, le=60)
    signal_symbols: str = Field(default="BTCUSDT")
    signal_cooldown_sec: int = Field(default=60, ge=1, le=3600)
    signal_retention_days: int = Field(default=30, ge=1, le=365)

    # Пороги правил
    signal_obi_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    signal_aggression_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    signal_cvd_min_abs_value: float = Field(default=1.0, gt=0)
    signal_large_trade_min_count: int = Field(default=5, ge=1, le=100)
    signal_tfi_large_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    signal_tfi_5m_exhaustion: float = Field(default=0.6, ge=0.0, le=1.0)
    signal_tfi_30s_reversal: float = Field(default=0.3, ge=0.0, le=1.0)

    # ---------------------------------------------------------
    # Валидаторы
    # ---------------------------------------------------------

    @field_validator("webhook_secret")
    @classmethod
    def _check_webhook_secret_strength(cls, v: SecretStr) -> SecretStr:
        """Не даём запустить бота с дефолтным/слабым секретом."""
        secret = v.get_secret_value()
        if len(secret) < 16:
            raise ValueError(
                "WEBHOOK_SECRET слишком короткий (мин. 16 символов). "
                "Сгенерируй: `openssl rand -hex 32`"
            )
        if "CHANGE_ME" in secret:
            raise ValueError(
                "WEBHOOK_SECRET всё ещё содержит CHANGE_ME — "
                "замени на реальный случайный секрет"
            )
        return v

    @field_validator("postgres_password", "redis_password")
    @classmethod
    def _check_passwords(cls, v: SecretStr) -> SecretStr:
        """Защита от запуска с дефолтными паролями."""
        pw = v.get_secret_value()
        if not pw:
            raise ValueError("Пароль не может быть пустым")
        if "CHANGE_ME" in pw:
            raise ValueError(
                "Пароль всё ещё содержит CHANGE_ME — замени в .env"
            )
        if len(pw) < 8:
            raise ValueError("Пароль слишком короткий (мин. 8 символов)")
        return v

    # ---------------------------------------------------------
    # Удобные computed properties
    # ---------------------------------------------------------

    @property
    def allowed_symbols_list(self) -> List[str]:
        """Парсит ALLOWED_SYMBOLS=BTCUSDT,ETHUSDT в список."""
        return [s.strip().upper() for s in self.allowed_symbols.split(",") if s.strip()]

    @property
    def signal_symbols_list(self) -> List[str]:
        """Парсит SIGNAL_SYMBOLS=BTCUSDT,ETHUSDT в список."""
        return [s.strip().upper() for s in self.signal_symbols.split(",") if s.strip()]

    @property
    def postgres_dsn(self) -> str:
        """SQLAlchemy DSN — собираем здесь, чтобы не размазывать по коду."""
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        pw = self.redis_password.get_secret_value()
        return f"redis://:{pw}@{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def is_production(self) -> bool:
        return self.app_env == AppEnv.production

    @property
    def is_mainnet_allowed(self) -> bool:
        """
        Защита: mainnet разблокируется ТОЛЬКО когда явно production И testnet=false.
        Любая другая комбинация = testnet.
        """
        return self.is_production and not self.bybit_testnet


# --------------------------------------------------------------
# Singleton — кеш на весь жизненный цикл приложения
# --------------------------------------------------------------
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Используй везде:
        from app.config import get_settings
        settings = get_settings()
    """
    return Settings()  # type: ignore[call-arg]
