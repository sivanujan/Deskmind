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

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ALLOWED_TRADING_MODES: tuple[str, ...] = ("paper", "testnet")
FORBIDDEN_POSTGRES_PORT = 5432
# Binance futures testnet / demo-trading hosts only; mainnet (fapi.binance.com) is refused.
ALLOWED_BINANCE_FUTURES_HOSTS: tuple[str, ...] = (
    "testnet.binancefuture.com",
    "demo-fapi.binance.com",
)
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash"
# The only place symbols are listed; everything else takes `symbol` as a parameter.
DEFAULT_DATA_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
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
    # Browser origins allowed to call the read-only API (CORS + WebSocket origin check).
    web_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3010", "http://127.0.0.1:3010"]
    )
    # USDT-M perpetuals with historical data. .env: DATA_SYMBOLS=BTCUSDT,ETHUSDT,...
    data_symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_DATA_SYMBOLS)
    )

    # LLM access for the Head agent, via OpenRouter (OpenAI-compatible API).
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_model: str = DEFAULT_OPENROUTER_MODEL
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

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
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_BINANCE_FUTURES_HOSTS:
            raise ValueError(
                f"BINANCE_FUTURES_BASE_URL={v!r} is not a testnet host; allowed: "
                f"{ALLOWED_BINANCE_FUTURES_HOSTS}"
            )
        return v.strip().rstrip("/")

    @field_validator("binance_market_data_url")
    @classmethod
    def _binance_host(cls, v: str) -> str:
        parsed = urlparse(v.strip())
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not host.endswith((".binance.com", ".binancefuture.com")):
            raise ValueError(f"BINANCE_MARKET_DATA_URL={v!r} must be an https Binance host")
        return v.strip().rstrip("/")

    @field_validator("data_symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, v: object) -> list[str]:
        items = v.split(",") if isinstance(v, str) else list(v)  # type: ignore[call-overload]
        symbols: list[str] = []
        for raw in items:
            sym = str(raw).strip().upper()
            if not sym:
                continue
            if not SYMBOL_RE.match(sym):
                raise ValueError(f"DATA_SYMBOLS entry {raw!r} is not a USDT-M symbol like BTCUSDT")
            if sym not in symbols:
                symbols.append(sym)
        if not symbols:
            raise ValueError("DATA_SYMBOLS must list at least one symbol")
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
