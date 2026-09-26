import pytest
from pydantic import ValidationError

from deskmind.config import Settings


def make(**overrides) -> Settings:
    return Settings(_env_file=None, postgres_password="x", **overrides)


@pytest.mark.parametrize("mode", ["paper", "testnet", "TESTNET", " paper "])
def test_allowed_trading_modes(mode: str) -> None:
    assert make(trading_mode=mode).trading_mode == mode.strip().lower()


@pytest.mark.parametrize("mode", ["live", "mainnet", "prod", ""])
def test_rejected_trading_modes(mode: str) -> None:
    with pytest.raises(ValidationError, match="TRADING_MODE"):
        make(trading_mode=mode)


def test_trading_mode_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_forbidden_postgres_port() -> None:
    with pytest.raises(ValidationError, match="5432"):
        make(postgres_host_port=5432)


def test_defaults_use_deskmind_ports() -> None:
    s = make()
    assert s.postgres_host_port == 5435
    assert s.redis_host_port == 6380
    assert ":5435/deskmind" in s.database_url
    assert s.redis_url == "redis://127.0.0.1:6380/0"


def test_secrets_not_in_repr() -> None:
    s = make(openrouter_api_key="sk-or-secret", binance_secret_key="bn-secret")
    assert "sk-or-secret" not in repr(s)
    assert "bn-secret" not in repr(s)


def test_binance_defaults_to_testnet() -> None:
    assert make().binance_futures_base_url == "https://testnet.binancefuture.com"


@pytest.mark.parametrize(
    "url",
    [
        "https://fapi.binance.com",
        "https://api.binance.com",
        "http://testnet.binancefuture.com",
        "https://testnet.binancefuture.com.evil.io",
    ],
)
def test_binance_mainnet_urls_refused(url: str) -> None:
    with pytest.raises(ValidationError, match="testnet"):
        make(binance_futures_base_url=url)


def test_openrouter_defaults() -> None:
    s = make()
    assert s.openrouter_model == "deepseek/deepseek-v4.1-flash"
    assert s.openrouter_base_url == "https://openrouter.ai/api/v1"


def test_openrouter_model_must_be_namespaced() -> None:
    with pytest.raises(ValidationError, match="OPENROUTER_MODEL"):
        make(openrouter_model="gpt")
