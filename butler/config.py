"""配置加载与校验。

密钥只从 .env / 环境变量读取，绝不写入代码与日志；配置缺失时给出修复指引，
而不是使用危险的默认值（例如放开设备权限）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv 为必装依赖
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIRMATION_ALWAYS = "always"
CONFIRMATION_NEVER = "never"
_VALID_CONFIRMATION_MODES = (CONFIRMATION_ALWAYS, CONFIRMATION_NEVER)


class ConfigError(Exception):
    """配置缺失或非法，message 中包含修复指引。"""


@dataclass
class ButlerConfig:
    base_url: str
    api_key: str
    model: str
    llm_timeout_seconds: float
    llm_max_retries: int
    max_tool_calls_per_turn: int
    max_history_messages: int
    confirmation_mode: str
    data_dir: Path
    log_dir: Path
    log_level: str


def _require(key: str) -> str:
    value = os.environ.get(key)
    if not value or not value.strip():
        raise ConfigError(
            f"缺少配置 {key}。请在项目根目录创建 .env 文件"
            f"（可复制 .env.example）并填写 {key}。"
        )
    return value.strip()


def _get_float(key: str, default: float) -> float:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"配置 {key} 必须是数字，当前值：{raw!r}") from None
    if value <= 0:
        raise ConfigError(f"配置 {key} 必须大于 0，当前值：{raw!r}")
    return value


def _get_int(key: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"配置 {key} 必须是整数，当前值：{raw!r}") from None
    if value < minimum:
        raise ConfigError(f"配置 {key} 不能小于 {minimum}，当前值：{raw!r}")
    return value


def load_config(env_path: Optional[Path] = None) -> ButlerConfig:
    """加载配置。默认读取项目根目录 .env；真实环境变量优先于 .env 文件。"""
    path = env_path if env_path is not None else PROJECT_ROOT / ".env"
    if load_dotenv is not None and path.exists():
        load_dotenv(path, override=False)

    api_key = _require("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").strip()
    model = os.environ.get("LLM_MODEL", "deepseek-chat").strip()
    confirmation_mode = os.environ.get("CONFIRMATION_MODE", CONFIRMATION_ALWAYS).strip().lower()
    if confirmation_mode not in _VALID_CONFIRMATION_MODES:
        raise ConfigError(
            f"配置 CONFIRMATION_MODE 只能为 {'/'.join(_VALID_CONFIRMATION_MODES)}，"
            f"当前值：{confirmation_mode!r}"
        )
    log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError(f"配置 LOG_LEVEL 非法：{log_level!r}")

    data_dir = PROJECT_ROOT / os.environ.get("DATA_DIR", "data").strip()
    return ButlerConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        llm_timeout_seconds=_get_float("LLM_TIMEOUT_SECONDS", 60.0),
        llm_max_retries=_get_int("LLM_MAX_RETRIES", 3),
        max_tool_calls_per_turn=_get_int("MAX_TOOL_CALLS_PER_TURN", 8),
        max_history_messages=_get_int("MAX_HISTORY_MESSAGES", 60, minimum=10),
        confirmation_mode=confirmation_mode,
        data_dir=data_dir,
        log_dir=PROJECT_ROOT / "logs",
        log_level=log_level,
    )
