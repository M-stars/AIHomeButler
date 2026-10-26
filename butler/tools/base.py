"""工具系统基础类型：规格、结果与风险等级。

ToolResult.status 明确区分：ok / error / declined / unknown。
"unknown"（状态未知）绝不能被表述为成功。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict

RISK_LOW = "low"        # 只读操作，自动放行
RISK_MEDIUM = "medium"  # 控制普通设备，必须用户确认
RISK_HIGH = "high"      # 高风险（锁/燃气/电热等），默认禁止

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_DECLINED = "declined"
STATUS_UNKNOWN = "unknown"


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict  # JSON Schema（顶层 object）
    risk: str
    handler: Callable[[Dict], Dict]  # 入参已校验；失败抛异常，成功返回 data

    def requires_confirmation(self) -> bool:
        return self.risk in (RISK_MEDIUM, RISK_HIGH)

    def openai_schema(self) -> Dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolResult:
    status: str
    message: str
    data: Dict = field(default_factory=dict)

    def to_payload(self) -> Dict:
        return {"status": self.status, "message": self.message, "data": self.data}
