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

BACKEND_SIMULATED = "simulated"
BACKEND_HOMEASSISTANT = "homeassistant"
_VALID_BACKENDS = (BACKEND_SIMULATED, BACKEND_HOMEASSISTANT)

TTS_BACKEND_SAPI = "sapi"
TTS_BACKEND_COSYVOICE = "cosyvoice"
_VALID_TTS_BACKENDS = (TTS_BACKEND_SAPI, TTS_BACKEND_COSYVOICE)


class ConfigError(Exception):
    """配置缺失或非法，message 中包含修复指引。"""


@dataclass
class ModelProfile:
    """一个命名模型档案：main=对话主模型，planner=任务规划，其余名字自取。

    支撑"一个主模型 + 若干次级模型"：每个消费者在装配时绑定自己的档案，
    档案未定义的字段逐项回退全局 LLM_* 配置，换厂商/换模型只动 .env。
    """

    name: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float
    max_retries: int


@dataclass
class ButlerConfig:
    base_url: str
    api_key: str
    model: str
    llm_timeout_seconds: float
    llm_max_retries: int
    max_tool_calls_per_turn: int
    # 单次模型响应携带的 tool_calls 上限：超出部分不执行并回填错误结果（评审 4.2）
    max_tool_calls_per_response: int
    # 每日 token 预算：0=不限；超限后拒绝新的模型调用（评审 4.5）
    daily_token_budget: int
    max_history_messages: int
    confirmation_mode: str
    device_backend: str
    ha_url: str
    ha_token: str
    ha_timeout_seconds: float
    ha_devices_file: Path
    proactive_check_interval_seconds: float
    data_dir: Path
    log_dir: Path
    log_level: str
    # Web 鉴权（评审 4.3）：留空则每次启动自动生成随机令牌并打印到控制台
    web_token: str
    # 语音合成（阶段 12）：默认 sapi 保持历史行为不变
    tts_backend: str
    cosyvoice_url: str
    cosyvoice_timeout_seconds: float
    cosyvoice_fallback_sapi: bool
    cosyvoice_sample_rate: int
    cosyvoice_max_text_chars: int
    # 家庭成员声纹识别（阶段 13）：默认关闭，行为不变
    speaker_recognition: bool
    speaker_threshold: float

    def model_profile(self, name: str) -> ModelProfile:
        """读取命名模型档案（.env 中 LLM_<NAME>_BASE_URL/MODEL/API_KEY/
        TIMEOUT_SECONDS/MAX_RETRIES），未定义的字段逐项回退全局 LLM_*。

        例如 LLM_PLANNER_MODEL=xxx 即可让任务规划单独用一个模型，
        不配置任何 LLM_<NAME>_* 变量时所有档案都等于全局配置（行为不变）。
        """
        key = name.strip().lower().replace("-", "_")
        if not key or not key.replace("_", "").isalnum():
            raise ConfigError(f"模型档案名非法：{name!r}（只允许字母、数字、下划线）")
        prefix = f"LLM_{key.upper()}_"
        return ModelProfile(
            name=key,
            base_url=os.environ.get(prefix + "BASE_URL", self.base_url).strip(),
            api_key=os.environ.get(prefix + "API_KEY", self.api_key).strip(),
            model=os.environ.get(prefix + "MODEL", self.model).strip(),
            timeout_seconds=_get_float(prefix + "TIMEOUT_SECONDS", self.llm_timeout_seconds),
            max_retries=_get_int(prefix + "MAX_RETRIES", self.llm_max_retries),
        )


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

    device_backend = os.environ.get("DEVICE_BACKEND", BACKEND_SIMULATED).strip().lower()
    if device_backend not in _VALID_BACKENDS:
        raise ConfigError(
            f"配置 DEVICE_BACKEND 只能为 {'/'.join(_VALID_BACKENDS)}，当前值：{device_backend!r}"
        )
    ha_url = os.environ.get("HA_URL", "http://localhost:8123").strip()
    ha_token = os.environ.get("HA_TOKEN", "").strip()
    if device_backend == BACKEND_HOMEASSISTANT:
        # fail-fast：切到真实平台前必须给齐凭证，绝不带默认值裸跑
        if not ha_token:
            raise ConfigError(
                "DEVICE_BACKEND=homeassistant 时必须配置 HA_TOKEN"
                "（HA 网页 → 用户资料 → 安全 → 长期访问令牌）"
            )
        if not ha_url.lower().startswith(("http://", "https://")):
            raise ConfigError(f"配置 HA_URL 必须以 http(s):// 开头，当前值：{ha_url!r}")

    data_dir = PROJECT_ROOT / os.environ.get("DATA_DIR", "data").strip()

    tts_backend = os.environ.get("TTS_BACKEND", TTS_BACKEND_SAPI).strip().lower()
    if tts_backend not in _VALID_TTS_BACKENDS:
        raise ConfigError(
            f"配置 TTS_BACKEND 只能为 {'/'.join(_VALID_TTS_BACKENDS)}，当前值：{tts_backend!r}"
        )
    cosyvoice_url = os.environ.get("COSYVOICE_URL", "http://127.0.0.1:9880").strip()
    if not cosyvoice_url.lower().startswith(("http://", "https://")):
        raise ConfigError(f"配置 COSYVOICE_URL 忘认以 http(s):// 开头，当前值：{cosyvoice_url!r}")

    return ButlerConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        llm_timeout_seconds=_get_float("LLM_TIMEOUT_SECONDS", 60.0),
        llm_max_retries=_get_int("LLM_MAX_RETRIES", 3),
        max_tool_calls_per_turn=_get_int("MAX_TOOL_CALLS_PER_TURN", 8),
        max_tool_calls_per_response=_get_int("MAX_TOOL_CALLS_PER_RESPONSE", 4),
        daily_token_budget=_get_int("TOKEN_BUDGET_PER_DAY", 500_000, minimum=0),
        max_history_messages=_get_int("MAX_HISTORY_MESSAGES", 60, minimum=10),
        confirmation_mode=confirmation_mode,
        device_backend=device_backend,
        ha_url=ha_url,
        ha_token=ha_token,
        ha_timeout_seconds=_get_float("HA_TIMEOUT_SECONDS", 10.0),
        ha_devices_file=PROJECT_ROOT / "config" / "ha_devices.json",
        # 主动服务后台检查间隔（下限 10s：更激进的间隔只会空转，冷却机制才是防轰炸的关键）
        proactive_check_interval_seconds=max(
            10.0, _get_float("PROACTIVE_CHECK_INTERVAL_SECONDS", 60.0)
        ),
        data_dir=data_dir,
        log_dir=PROJECT_ROOT / "logs",
        log_level=log_level,
        web_token=os.environ.get("WEB_TOKEN", "").strip(),
        # 语音合成：CosyVoice 为可选后端，默认 sapi 保证原有行为不变
        tts_backend=tts_backend,
        cosyvoice_url=cosyvoice_url,
        cosyvoice_timeout_seconds=_get_float("COSYVOICE_TIMEOUT_SECONDS", 120.0),
        cosyvoice_fallback_sapi=os.environ.get("COSYVOICE_FALLBACK_SAPI", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        # 官方服务端返回裸 PCM 流，无采样率元数据；Fun-CosyVoice3-0.5B 为 24000，
        # 换模型时以模型目录 cosyvoice3.yaml 里的 sample_rate 为准
        cosyvoice_sample_rate=_get_int("COSYVOICE_SAMPLE_RATE", 24000, minimum=8000),
        cosyvoice_max_text_chars=_get_int("COSYVOICE_MAX_TEXT_CHARS", 500, minimum=1),
        # 声纹识别：默认关闭；开启后语音模式在录音后自动识别家庭成员
        speaker_recognition=os.environ.get("SPEAKER_RECOGNITION", "0").strip().lower()
        in ("1", "true", "yes", "on"),
        speaker_threshold=min(
            0.95, max(0.05, _get_float("SPEAKER_THRESHOLD", 0.30))
        ),
    )
