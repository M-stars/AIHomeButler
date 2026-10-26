import os

import pytest

from butler.config import ConfigError, load_config


def _clear_llm_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("LLM_") or key in ("CONFIRMATION_MODE", "DATA_DIR", "LOG_LEVEL"):
            monkeypatch.delenv(key, raising=False)


def test_missing_api_key_gives_guidance(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_BASE_URL=https://api.deepseek.com\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(env_path=env_file)
    message = str(excinfo.value)
    assert "LLM_API_KEY" in message
    assert ".env" in message


def test_loads_from_env_vars(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    config = load_config(env_path=tmp_path / "missing.env")
    assert config.api_key == "sk-test"
    assert config.model == "test-model"
    assert config.confirmation_mode == "always"  # 安全默认


def test_invalid_confirmation_mode_rejected(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("CONFIRMATION_MODE", "sometimes")
    with pytest.raises(ConfigError):
        load_config(env_path=tmp_path / "missing.env")


def test_bad_number_config_rejected(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("MAX_TOOL_CALLS_PER_TURN", "zero")
    with pytest.raises(ConfigError):
        load_config(env_path=tmp_path / "missing.env")
