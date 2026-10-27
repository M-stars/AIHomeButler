"""build_llm 工厂：按命名模型档案装配客户端（不触网，只验证参数解析与绑定）。"""
import os

import pytest

from butler.config import ConfigError, load_config
from butler.llm_client import OpenAICompatClient, build_llm


def _clear_llm_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("LLM_"):
            monkeypatch.delenv(key, raising=False)


def _make_config(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-global")
    monkeypatch.setenv("LLM_MODEL", "global-model")
    return load_config(env_path=tmp_path / "missing.env")


def test_build_llm_default_is_main_profile(tmp_path, monkeypatch):
    config = _make_config(tmp_path, monkeypatch)
    client = build_llm(config)
    assert isinstance(client, OpenAICompatClient)
    assert client._model == "global-model"


def test_build_llm_uses_named_profile(tmp_path, monkeypatch):
    config = _make_config(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_PLANNER_MODEL", "planner-model")
    client = build_llm(config, "planner")
    assert isinstance(client, OpenAICompatClient)
    assert client._model == "planner-model"


def test_build_llm_profiles_are_independent(tmp_path, monkeypatch):
    """主模型与次级模型互不影响：改 planner 的配置不改变 main 的绑定。"""
    config = _make_config(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_PLANNER_MODEL", "planner-model")
    assert build_llm(config, "main")._model == "global-model"
    assert build_llm(config, "planner")._model == "planner-model"


def test_build_llm_invalid_profile_name_rejected(tmp_path, monkeypatch):
    config = _make_config(tmp_path, monkeypatch)
    with pytest.raises(ConfigError):
        build_llm(config, "bad name!")
