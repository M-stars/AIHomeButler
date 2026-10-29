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


def _make_config(tmp_path, monkeypatch) -> "ButlerConfig":
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-global")
    monkeypatch.setenv("LLM_MODEL", "global-model")
    return load_config(env_path=tmp_path / "missing.env")


def test_model_profile_falls_back_to_global(tmp_path, monkeypatch):
    from butler.config import ModelProfile

    config = _make_config(tmp_path, monkeypatch)
    profile = config.model_profile("planner")
    assert isinstance(profile, ModelProfile)
    assert profile.name == "planner"
    assert profile.api_key == "sk-global"
    assert profile.model == "global-model"
    assert profile.base_url == "https://api.deepseek.com"
    assert profile.timeout_seconds == 60.0
    assert profile.max_retries == 3


def test_model_profile_overrides_individual_fields(tmp_path, monkeypatch):
    config = _make_config(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_PLANNER_MODEL", "planner-model")
    monkeypatch.setenv("LLM_PLANNER_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("LLM_PLANNER_MAX_RETRIES", "2")
    profile = config.model_profile("planner")
    assert profile.model == "planner-model"
    assert profile.timeout_seconds == 5.0
    assert profile.max_retries == 2
    # 未覆盖的字段仍回退全局
    assert profile.api_key == "sk-global"
    assert profile.base_url == "https://api.deepseek.com"


def test_model_profile_name_normalized(tmp_path, monkeypatch):
    config = _make_config(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_CHEAP_FAST_MODEL", "cheap-model")
    profile = config.model_profile("Cheap-Fast")
    assert profile.name == "cheap_fast"
    assert profile.model == "cheap-model"


def test_model_profile_invalid_name_rejected(tmp_path, monkeypatch):
    config = _make_config(tmp_path, monkeypatch)
    with pytest.raises(ConfigError):
        config.model_profile("bad name!")
    with pytest.raises(ConfigError):
        config.model_profile("   ")


def test_model_profile_bad_number_rejected(tmp_path, monkeypatch):
    config = _make_config(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_PLANNER_TIMEOUT_SECONDS", "fast")
    with pytest.raises(ConfigError) as excinfo:
        config.model_profile("planner")
    assert "LLM_PLANNER_TIMEOUT_SECONDS" in str(excinfo.value)
