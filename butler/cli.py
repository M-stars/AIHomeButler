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
from .memory.store import MemoryStore
from .proactive import RuleEngine, build_proactive_tools
from .scheduler import RuleScheduler
from .tools.executor import ToolExecutor
from .tools.home import build_default_devices, build_home_tools
from .tools.memory_tools import build_memory_tools
from .tools.registry import ToolRegistry

HELP = """命令：
  直接输入自然语言与管家对话，例如：现在客厅温度是多少？
  /devices   列出设备
  /memory    查看已保存的长期记忆
  /task      多步骤任务模式，例如：/task 我准备睡觉了，关掉卧室灯并打开风扇低档
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


def _print_memories(memory_store: MemoryStore) -> None:
    memories = memory_store.list()
    if not memories:
        print("  （暂无长期记忆。对话中说『记住我喜欢……』即可保存，保存前会请你确认。）")
        return
    kind_label = {"preference": "偏好", "fact": "事实"}
    for m in memories:
        print(f"  #{m['id']} [{kind_label.get(m['kind'], m['kind'])}] {m['content']}  ({m['updated_at']})")


def _print_step(step: dict) -> None:
    status = step.get("status", "pending")
    mark = {"ok": "✓", "error": "✗", "declined": "⊘", "unknown": "?"}.get(status, "·")
    print(f"  [{mark} 步骤] {step.get('tool')} → {status}：{step.get('message', '')}")


def _deliver_notifications(rule_engine: RuleEngine) -> None:
    """对话轮次结束后的即时检查（与后台调度器互补，冷却机制防重复）。"""
    try:
        fired = rule_engine.run_checks()
    except Exception:
        logging.getLogger("butler.cli").exception("主动规则检查失败")
        return
    for notification in fired:
        print(f"  [🔔 主动提醒] {notification['message']}")


def _console_notification_sink(notification: dict) -> None:
    """后台调度器的通知出口：随时打印（用户输入中的行会被打断，重打提示符）。"""
    print(f"\n  [🔔 主动提醒] {notification['message']}")
    print("你: ", end="", flush=True)


def _start_proactive_scheduler(config: ButlerConfig, rule_engine: RuleEngine) -> RuleScheduler:
    scheduler = RuleScheduler(
        rule_engine,
        interval_seconds=config.proactive_check_interval_seconds,
        sink=_console_notification_sink,
    )
    scheduler.start()
    return scheduler


def _run_task(config: ButlerConfig, registry: DeviceRegistry, memory_store: MemoryStore, user_text: str) -> None:
    """阶段 6 的多步骤任务入口。"""
    from .planner import PlanRunner

    runner = PlanRunner.for_cli(config, registry, memory_store)
    report = runner.run(user_text, on_step=_print_step)
    print(f"任务结果：{report['status']}")
    print(report["summary"])


def _build_registry(config: ButlerConfig) -> DeviceRegistry:
    """按配置选择设备后端：模拟（默认，安全）或 Home Assistant（真实设备）。"""
    if config.device_backend == BACKEND_HOMEASSISTANT:
        from .devices.homeassistant import build_ha_registry

        return build_ha_registry(config)
    return build_default_devices(config.data_dir)


def _build_agent(
    config: ButlerConfig,
    registry: DeviceRegistry,
    memory_store: MemoryStore,
    confirmer=None,
) -> tuple:
    tool_registry = ToolRegistry()
    for spec in build_home_tools(registry):
        tool_registry.register(spec)
    for spec in build_memory_tools(memory_store):
        tool_registry.register(spec)
    for spec in build_proactive_tools(memory_store):
        tool_registry.register(spec)

    confirmer = confirmer or ConsoleConfirmer()
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

    def system_prompt_provider() -> str:
        return build_system_prompt(
            _inventory(registry), datetime.now(), memory_store.prompt_block()
        )

    conversation = Conversation(system_prompt_provider(), config.max_history_messages)
    agent = Agent(
        llm=llm,
        conversation=conversation,
        tool_registry=tool_registry,
        tool_executor=executor,
        max_tool_calls_per_turn=config.max_tool_calls_per_turn,
        system_prompt_provider=system_prompt_provider,
    )
    rule_engine = RuleEngine(registry, memory_store)
    return agent, rule_engine


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
    memory_store = MemoryStore(config.data_dir / "memory.db")
    agent, rule_engine = _build_agent(config, registry, memory_store)
    _start_proactive_scheduler(config, rule_engine)

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
        if line == "/memory":
            _print_memories(memory_store)
            continue
        if line.startswith("/task "):
            _run_task(config, registry, memory_store, line[len("/task "):].strip())
            continue
        if line == "/task":
            print("用法：/task 任务描述（例如 /task 我准备睡觉了，关掉卧室灯）")
            continue

        result = agent.process(line)
        for event in result.tool_events:
            mark = _STATUS_MARKS.get(event.status, "·")
            print(f"  [{mark} 工具] {event.tool}: {event.status} — {event.message}")
        print(f"管家: {result.reply}")
        _deliver_notifications(rule_engine)


if __name__ == "__main__":
    sys.exit(main())
