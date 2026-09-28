"""Application settings, loaded from environment / .env.

Startup is refused if TRADING_MODE is anything other than "paper" or "testnet",
or if the database is pointed at the forbidden default Postgres port (5432).
"""

from __future__ import annotations

import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ALLOWED_TRADING_MODES: tuple[str, ...] = ("paper", "testnet")
FORBIDDEN_POSTGRES_PORT = 5432
# Binance futures testnet / demo-trading hosts only; mainnet (fapi.binance.com) is refused.
ALLOWED_BINANCE_FUTURES_REST_HOSTS: tuple[str, ...] = (
    "testnet.binancefuture.com",
    "demo-fapi.binance.com",
)
ALLOWED_BINANCE_FUTURES_WS_HOSTS: tuple[str, ...] = (
    "demo-fstream.binance.com",
    "stream.binancefuture.com",
    "fstream.binancefuture.com",
)
ALLOWED_BINANCE_FUTURES_HOSTS: tuple[str, ...] = (
    ALLOWED_BINANCE_FUTURES_REST_HOSTS + ALLOWED_BINANCE_FUTURES_WS_HOSTS
)
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash"
# The only place symbols are listed; everything else takes `symbol` as a parameter.
DEFAULT_DATA_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
# Symbols the trade service may open NEW positions on (also needs universe state `tradeable`).
DEFAULT_TRADE_SYMBOLS = ("BTCUSDT",)
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")

TradingMode = Literal["paper", "testnet"]

# Backtest engine constants
BACKTEST_ENGINE_VERSION = "1.0.0"

# Splits: train 2023-01-01 -> 2024-12-31, validation 2025-01-01 -> 2025-12-31,
# test 2026-01-01 -> latest
BACKTEST_SPLITS: dict[str, tuple[str, str]] = {
    "train": ("2023-01-01", "2024-12-31"),
    "validation": ("2025-01-01", "2025-12-31"),
    "test": ("2026-01-01", "2026-09-26"),
}

# Binance USDT-M VIP0 fee rates
DEFAULT_MAKER_FEE_RATE = Decimal("0.0002")  # 0.02%
DEFAULT_TAKER_FEE_RATE = Decimal("0.0005")  # 0.05%

# Conservative default slippage: 1.0 bps fixed + 0.05 * ATR / price
DEFAULT_SLIPPAGE_BPS = Decimal("1.0")
DEFAULT_SLIPPAGE_ATR_FACTOR = Decimal("0.05")

# Initial backtest cash (USDT)
DEFAULT_INITIAL_CASH = Decimal("10000.0")

# Conservative Binance maintenance margin tiers (max_notional_usdt, mmr_rate)
DEFAULT_MAINTENANCE_BRACKETS: list[tuple[Decimal, Decimal]] = [
    (Decimal("50000"), Decimal("0.005")),  # Tier 1: 0 - 50k -> 0.5% MMR
    (Decimal("250000"), Decimal("0.010")),  # Tier 2: 50k - 250k -> 1.0% MMR
    (Decimal("1000000"), Decimal("0.020")),  # Tier 3: 250k - 1M -> 2.0% MMR
    (Decimal("10000000"), Decimal("0.050")),  # Tier 4: 1M - 10M -> 5.0% MMR
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    trading_mode: TradingMode = "testnet"
    log_level: str = "INFO"

    postgres_user: str = "deskmind"
    postgres_password: SecretStr = SecretStr("")
    postgres_db: str = "deskmind"
    postgres_host: str = "127.0.0.1"
    postgres_host_port: int = Field(default=5435, ge=1, le=65535)

    redis_host: str = "127.0.0.1"
    redis_host_port: int = Field(default=6380, ge=1, le=65535)

    # Binance USDT-M futures. Keys are only ever sent to a testnet host (see validator).
    binance_api_key: SecretStr = SecretStr("")
    binance_secret_key: SecretStr = SecretStr("")
    binance_futures_base_url: str = "https://testnet.binancefuture.com"
    binance_futures_ws_base_url: str = "wss://demo-fstream.binance.com"
    approval_mode: Literal["auto", "manual"] = "auto"
    testnet_price_divergence_max_pct: Decimal = Decimal("0.05")

    # Binance Mainnet Read-Only API client (Part 1). Strictly read-only, never usable by executors.
    binance_mainnet_readonly_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "binance_mainnet_readonly_api_key",
            "BINANCE_MAINNET_READONLY_API_KEY",
        ),
    )
    binance_mainnet_readonly_api_secret: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "binance_mainnet_readonly_api_secret",
            "BINANCE_MAINNET_READONLY_API_SECRET",
            "binance_mainnet_readonly_secret",
            "BINANCE_MAINNET_READONLY_SECRET",
        ),
    )
    binance_mainnet_api_url: str = "https://api.binance.com"
    binance_mainnet_futures_url: str = "https://fapi.binance.com"

    # Public market data (klines, mark price, funding, exchangeInfo). Unauthenticated GETs only:
    # the market-data client has no way to send API keys or sign requests.
    binance_market_data_url: str = "https://fapi.binance.com"
    # Live market data WebSocket. Binance routes kline/markPrice streams on the /market path;
    # unrouted connections no longer receive them.
    binance_ws_market_url: str = "wss://fstream.binance.com/market"
    live_timeframes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["5m", "15m", "1h", "2h"]
    )
    data_dir: Path = Path("data")
    # Streams per live WebSocket connection (Binance allows 1024); more symbols -> more connections.
    live_max_streams_per_connection: int = Field(default=200, ge=5, le=1024)
    # Browser origins allowed to call the read-only API (CORS + WebSocket origin check).
    web_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3010", "http://127.0.0.1:3010"]
    )
    # USDT-M perpetuals with historical data. .env: DATA_SYMBOLS=BTCUSDT,ETHUSDT,...
    data_symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_DATA_SYMBOLS)
    )
    # Allowlist for NEW entries (Phase 1: BTCUSDT). .env: TRADE_SYMBOLS=BTCUSDT
    trade_symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_TRADE_SYMBOLS)
    )

    # Liquidity universe + daily job (deskmind.universe). Liquidity only: volatility / trend /
    # volume spikes belong to the Market Scanner, which works inside the universe.
    universe_top_n: int = Field(default=30, ge=1, le=200)
    universe_exit_rank: int = Field(default=40, ge=1, le=300)
    universe_lookback_days: int = Field(default=30, ge=7, le=90)
    universe_min_listing_age_days: int = Field(default=180, ge=0)
    universe_history_days: int = Field(default=365, ge=30)
    universe_warmup_days: int = Field(default=30, ge=1)
    universe_download_concurrency: int = Field(default=2, ge=1, le=4)
    # Disk guard: new full downloads stop when the data drive has less free space than this.
    universe_disk_min_free_gb: float = Field(default=5.0, ge=0.5)

    # LLM access for the Head agent, via OpenRouter (OpenAI-compatible API).
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_model: str = DEFAULT_OPENROUTER_MODEL
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Dashboard Control authentication & session settings (Part 2)
    dashboard_username: str = Field(
        default="admin",
        validation_alias=AliasChoices("dashboard_username", "DASHBOARD_USERNAME"),
    )
    dashboard_password_hash: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("dashboard_password_hash", "DASHBOARD_PASSWORD_HASH"),
    )
    session_secret_key: SecretStr = Field(
        default=SecretStr("deskmind-dashboard-dev-secret-key-change-in-prod"),
        validation_alias=AliasChoices(
            "session_secret_key",
            "SESSION_SECRET_KEY",
            "dashboard_secret_key",
            "DASHBOARD_SECRET_KEY",
        ),
    )

    @field_validator("trading_mode", mode="before")
    @classmethod
    def _validate_trading_mode(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().lower()
        if v not in ALLOWED_TRADING_MODES:
            raise ValueError(
                f"TRADING_MODE={v!r} is not allowed; must be one of {ALLOWED_TRADING_MODES}"
            )
        return v

    @field_validator("postgres_host_port")
    @classmethod
    def _forbid_default_postgres_port(cls, v: int) -> int:
        if v == FORBIDDEN_POSTGRES_PORT:
            raise ValueError(
                "POSTGRES_HOST_PORT=5432 belongs to another project; Deskmind must use its own "
                "container port (default 5435)"
            )
        return v

    @field_validator("binance_futures_base_url")
    @classmethod
    def _testnet_only(cls, v: str) -> str:
        parsed = urlparse(v.strip())
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_BINANCE_FUTURES_REST_HOSTS:
            raise ValueError(
                f"BINANCE_FUTURES_BASE_URL={v!r} is not an allowed testnet REST host; allowed: "
                f"{ALLOWED_BINANCE_FUTURES_REST_HOSTS}"
            )
        return v.strip().rstrip("/")

    @field_validator("binance_futures_ws_base_url")
    @classmethod
    def _testnet_ws_only(cls, v: str) -> str:
        parsed = urlparse(v.strip())
        if parsed.scheme != "wss" or parsed.hostname not in ALLOWED_BINANCE_FUTURES_WS_HOSTS:
            raise ValueError(
                f"BINANCE_FUTURES_WS_BASE_URL={v!r} is not an allowed testnet WS host; allowed: "
                f"{ALLOWED_BINANCE_FUTURES_WS_HOSTS}"
            )
        return v.strip().rstrip("/")

    @field_validator("approval_mode", mode="before")
    @classmethod
    def _validate_approval_mode(cls, v: object) -> str:
        if isinstance(v, str):
            v = v.strip().lower()
        if v not in ("auto", "manual"):
            raise ValueError(f"APPROVAL_MODE={v!r} must be 'auto' or 'manual'")
        return str(v)

    @field_validator("binance_market_data_url")
    @classmethod
    def _binance_host(cls, v: str) -> str:
        parsed = urlparse(v.strip())
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not host.endswith((".binance.com", ".binancefuture.com")):
            raise ValueError(f"BINANCE_MARKET_DATA_URL={v!r} must be an https Binance host")
        return v.strip().rstrip("/")

    @field_validator("data_symbols", "trade_symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, v: object, info: ValidationInfo) -> list[str]:
        setting = (info.field_name or "symbols").upper()
        items = v.split(",") if isinstance(v, str) else list(v)  # type: ignore[call-overload]
        symbols: list[str] = []
        for raw in items:
            sym = str(raw).strip().upper()
            if not sym:
                continue
            if not SYMBOL_RE.match(sym):
                raise ValueError(f"{setting} entry {raw!r} is not a USDT-M symbol like BTCUSDT")
            if sym not in symbols:
                symbols.append(sym)
        if not symbols:
            raise ValueError(f"{setting} must list at least one symbol")
        return symbols

    @field_validator("binance_ws_market_url")
    @classmethod
    def _binance_ws_host(cls, v: str) -> str:
        parsed = urlparse(v.strip())
        host = parsed.hostname or ""
        if parsed.scheme != "wss" or not host.endswith((".binance.com", ".binancefuture.com")):
            raise ValueError(f"BINANCE_WS_MARKET_URL={v!r} must be a wss:// Binance host")
        return v.strip().rstrip("/")

    @field_validator("live_timeframes", mode="before")
    @classmethod
    def _parse_timeframes(cls, v: object) -> list[str]:
        items = v.split(",") if isinstance(v, str) else list(v)  # type: ignore[call-overload]
        tfs = [str(x).strip() for x in items if str(x).strip()]
        allowed = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"}
        bad = [t for t in tfs if t not in allowed]
        if bad or not tfs:
            raise ValueError(f"LIVE_TIMEFRAMES must be a non-empty subset of {sorted(allowed)}")
        return list(dict.fromkeys(tfs))

    @field_validator("web_origins", mode="before")
    @classmethod
    def _parse_origins(cls, v: object) -> list[str]:
        items = v.split(",") if isinstance(v, str) else list(v)  # type: ignore[call-overload]
        return [str(x).strip().rstrip("/") for x in items if str(x).strip()]

    @field_validator("openrouter_model")
    @classmethod
    def _model_id_shape(cls, v: str) -> str:
        v = v.strip()
        if "/" not in v:
            raise ValueError(f"OPENROUTER_MODEL={v!r} must look like 'provider/model'")
        return v

    @property
    def database_url(self) -> str:
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_host_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_host_port}/0"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
