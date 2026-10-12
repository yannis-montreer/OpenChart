import os

import pytest

from openchart.config import ConfigError, load_config, mask_secret


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # load_dotenv writes into os.environ; give each test a private copy.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for key in [
        "OPENCHART_OANDA_ENV",
        "OPENCHART_OANDA_TOKEN",
        "OPENCHART_OANDA_ACCOUNT_ID",
        "OPENCHART_DATA_DIR",
        "OPENCHART_HOST",
    ]:
        monkeypatch.delenv(key, raising=False)


def test_defaults_are_safe():
    config = load_config(env_file=None)
    assert config.host == "127.0.0.1"
    assert config.oanda.environment == "practice"
    assert config.oanda.rest_url == "https://api-fxpractice.oanda.com"
    assert not config.oanda.configured


def test_env_file_is_loaded(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OPENCHART_OANDA_ENV=live\nOPENCHART_OANDA_ACCOUNT_ID=001-1\n")
    config = load_config(env)
    assert config.oanda.stream_url == "https://stream-fxtrade.oanda.com"
    assert config.oanda.account_id == "001-1"


def test_invalid_environment_rejected(monkeypatch):
    monkeypatch.setenv("OPENCHART_OANDA_ENV", "demo")
    with pytest.raises(ConfigError):
        load_config(env_file=None)


def test_token_never_in_repr(monkeypatch):
    monkeypatch.setenv("OPENCHART_OANDA_TOKEN", "abcdef-secret-token-1234")
    config = load_config(env_file=None)
    assert "secret" not in repr(config)
    assert "secret" not in repr(config.oanda)
    assert mask_secret(config.oanda.token) == "****1234"
