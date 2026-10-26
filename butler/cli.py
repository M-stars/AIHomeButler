"""AI Home Butler 命令行入口。

Windows 终端默认 GBK 编码，启动时对标准流做 UTF-8 重配置（失败则降级继续）。
"""
from __future__ import annotations

import sys
from datetime import datetime

from .agent import Agent, build_system_prompt
from .config import (
    BACKEND_HOMEASSISTANT,
    CONFIRMATION_ALWAYS,
    CONFIRMATION_NEVER,
    ButlerConfig,
    ConfigError,
    load_config,
)
from .confirmation import ConsoleConfirmer
from .conversation import Conversation
from .devices.registry import DeviceRegistry
from .llm_client import OpenAICompatClient
from .logging_setup import setup_logging
from .tools.executor import ToolExecutor
from .tools.home import build_default_devices, build_home_tools
from .tools.registry import ToolRegistry

HELP = """命令：
  直接输入自然语言与管家对话，例如：现在客厅温度是多少？
  /devices   列出设备
  /help      显示本帮助
  /exit      退出（或 Ctrl+C）"""

_STATUS_MARKS = {"ok": "✓", "error": "✗", "declined": "⊘", "unknown": "?"}


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # 降级为系统默认编码，不影响运行


def _inventory(registry: DeviceRegistry) -> str:
    lines = [
        f"  - {d.info.id}（{d.info.name}，{d.info.area}，类型 {d.info.device_type}）"
        for d in registry.list()
    ]
    return "\n".join(lines) if lines else "  （无设备）"


def _print_devices(registry: DeviceRegistry) -> None:
    for d in registry.list():
        availability = "在线" if d.is_available() else "离线"
        print(f"  {d.info.id}  {d.info.name}  [{d.info.area}]  {availability}")


def _build_registry(config: ButlerConfig) -> DeviceRegistry:
    """按配置选择设备后端：模拟（默认，安全）或 Home Assistant（真实设备）。"""
    if config.device_backend == BACKEND_HOMEASSISTANT:
        from .devices.homeassistant import build_ha_registry

        return build_ha_registry(config)
    return build_default_devices(config.data_dir)


def _build_agent(config: ButlerConfig, registry: DeviceRegistry) -> Agent:
    tool_registry = ToolRegistry()
    for spec in build_home_tools(registry):
        tool_registry.register(spec)

    confirmer = ConsoleConfirmer()
    executor = ToolExecutor(
        tool_registry,
        confirmer,
        confirmation_enabled=(config.confirmation_mode == CONFIRMATION_ALWAYS),
    )
    llm = OpenAICompatClient(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        timeout_seconds=config.llm_timeout_seconds,
        max_retries=config.llm_max_retries,
    )
    conversation = Conversation(
        build_system_prompt(_inventory(registry), datetime.now()),
        max_messages=config.max_history_messages,
    )
    return Agent(
        llm=llm,
        conversation=conversation,
        tool_registry=tool_registry,
        tool_executor=executor,
        max_tool_calls_per_turn=config.max_tool_calls_per_turn,
    )


def main() -> int:
    _force_utf8_stdio()
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2

    logger = setup_logging(config.log_dir, config.log_level)
    if config.confirmation_mode == CONFIRMATION_NEVER:
        logger.warning("CONFIRMATION_MODE=never：控制类操作不再请求确认，仅限调试环境使用！")

    registry = _build_registry(config)
    agent = _build_agent(config, registry)

    backend_label = (
        f"Home Assistant @ {config.ha_url}"
        if config.device_backend == BACKEND_HOMEASSISTANT
        else "模拟设备模式"
    )
    print("=" * 56)
    print("AI Home Butler 管家已就绪")
    print(f"设备后端: {backend_label}")
    print(f"模型: {config.model} @ {config.base_url}")
    print("确认模式: " + ("每次控制操作需确认" if config.confirmation_mode == CONFIRMATION_ALWAYS else "已关闭（仅限调试）"))
    print("设备清单:")
    print(_inventory(registry))
    print(HELP)
    print("=" * 56)

    while True:
        try:
            line = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n管家: 再见！")
            return 0

        if not line:
            continue
        if line in ("/exit", "/quit", "/q"):
            print("管家: 再见！")
            return 0
        if line == "/help":
            print(HELP)
            continue
        if line == "/devices":
            _print_devices(registry)
            continue

        result = agent.process(line)
        for event in result.tool_events:
            mark = _STATUS_MARKS.get(event.status, "·")
            print(f"  [{mark} 工具] {event.tool}: {event.status} — {event.message}")
        print(f"管家: {result.reply}")


if __name__ == "__main__":
    sys.exit(main())
