"""用户确认接口。

控制类（MEDIUM 及以上风险）操作执行前必须经过 Confirmer。输入端只回答布尔值，
拒绝即不执行——安全决策在执行器，不在交互层。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Protocol


class Confirmer(Protocol):
    def confirm(self, summary: str, arguments: Dict[str, Any]) -> bool:
        ...


class ConsoleConfirmer:
    """终端交互确认：y/yes 确认，其余一律视为拒绝（安全默认）。"""

    def confirm(self, summary: str, arguments: Dict[str, Any]) -> bool:
        print(f"\n[需要确认] {summary}")
        if arguments:
            print("  参数: " + json.dumps(arguments, ensure_ascii=False))
        try:
            answer = input("是否执行？[y/N]: ").strip().lower()
        except (EOFError, OSError):
            print("  （无法读取输入，按拒绝处理）")
            return False
        return answer in ("y", "yes")


class AutoApproveConfirmer:
    """测试/调试用：自动确认。禁止在生产配置中替代真实确认。"""

    def confirm(self, summary: str, arguments: Dict[str, Any]) -> bool:
        return True


class AutoDenyConfirmer:
    """测试用：自动拒绝。"""

    def confirm(self, summary: str, arguments: Dict[str, Any]) -> bool:
        return False
