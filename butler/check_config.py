"""配置与环境自检：python -m butler.check_config

检查项：
  1. .env 完整性与合法性（fail-fast 规则同运行时）；
  2. 大模型 API 连通性（一次最小请求）；
  3. 设备后端：simulated 检查数据目录可写；homeassistant 检查 API 可达与令牌有效；
  4. 设备清单与健康状态；
  5. 记忆数据库可写。
退出码：0=全部通过，1=存在失败项。输出中密钥永远只显示前 6 位。
"""
from __future__ import annotations

import sys
from pathlib import Path

from .config import BACKEND_HOMEASSISTANT, ConfigError, load_config
from .devices.base import DeviceError
from .logging_setup import setup_logging

_OK = "✓"
_FAIL = "✗"
_WARN = "!"


def _redact(key: str) -> str:
    return key[:6] + "…" if len(key) > 6 else "…"


def main() -> int:
    failures = 0
    print("=" * 60)
    print("AI Home Butler 配置自检")
    print("=" * 60)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"{_FAIL} 配置加载失败：{exc}")
        return 1

    print(f"{_OK} 配置加载成功")
    print(f"  模型: {config.model} @ {config.base_url}")
    print(f"  API Key: {_redact(config.api_key)}")
    print(f"  设备后端: {config.device_backend} ｜ 确认模式: {config.confirmation_mode}")

    # 1. LLM 连通性
    print("\n[1/4] 大模型 API 连通性")
    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=config.base_url, api_key=config.api_key,
            timeout=config.llm_timeout_seconds, max_retries=1,
        )
        response = client.chat.completions.create(
            model=config.model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
        )
        print(f"{_OK} 模型响应正常（{response.model}）")
    except Exception as exc:
        failures += 1
        print(f"{_FAIL} 模型调用失败：{type(exc).__name__}: {str(exc)[:200]}")

    # 2. 设备后端
    print("\n[2/4] 设备后端")
    registry = None
    try:
        from .cli import _build_registry

        registry = _build_registry(config)
        print(f"{_OK} 设备注册表构建成功（{len(registry.list())} 个设备）")
    except ConfigError as exc:
        failures += 1
        print(f"{_FAIL} 后端配置错误：{exc}")
    except DeviceError as exc:
        failures += 1
        print(f"{_FAIL} 后端错误：{exc}")
    except Exception as exc:
        failures += 1
        print(f"{_FAIL} 后端不可用：{type(exc).__name__}: {str(exc)[:200]}")

    if config.device_backend == BACKEND_HOMEASSISTANT:
        try:
            import httpx

            resp = httpx.get(
                config.ha_url.rstrip("/") + "/api/",
                headers={"Authorization": f"Bearer {config.ha_token}"},
                timeout=config.ha_timeout_seconds,
            )
            if resp.status_code == 200:
                print(f"{_OK} Home Assistant API 可达且令牌有效")
            elif resp.status_code == 401:
                failures += 1
                print(f"{_FAIL} HA 令牌无效（401）：请重新生成长期访问令牌")
            else:
                failures += 1
                print(f"{_FAIL} HA API 异常：HTTP {resp.status_code}")
        except Exception as exc:
            failures += 1
            print(f"{_FAIL} 无法连接 Home Assistant：{type(exc).__name__}")

    # 3. 设备健康
    print("\n[3/4] 设备健康")
    if registry is not None:
        for device in registry.list():
            try:
                state = device.read_state()
                print(f"{_OK} {device.info.id}: 可读")
            except DeviceError as exc:
                print(f"{_WARN} {device.info.id}: {exc}")

    # 4. 存储可写
    print("\n[4/4] 存储")
    try:
        from .memory.store import MemoryStore

        probe = MemoryStore(config.data_dir / "check_config_probe.db")
        probe.add("fact", "自检探针（立即删除）")
        memories = probe.list()
        probe.delete(memories[0]["id"])
        probe.close()
        (config.data_dir / "check_config_probe.db").unlink(missing_ok=True)
        print(f"{_OK} 记忆数据库可写（{config.data_dir}）")
    except Exception as exc:
        failures += 1
        print(f"{_FAIL} 存储不可写：{type(exc).__name__}: {exc}")

    print("\n" + "=" * 60)
    if failures:
        print(f"自检完成：{failures} 项失败，请按上方提示修复后重试")
        return 1
    print("自检完成：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
