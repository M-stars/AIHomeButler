"""工具系统基础类型：规格、结果、风险等级与结果阶段。

ToolResult.status：ok / error / declined / unknown（unknown 绝不能被表述为成功）。
ToolResult.stage（阶段 4 引入）进一步区分执行进度：
  executed=请求已执行  verified=执行并回读验证通过  done=只读操作完成
  cancelled=用户取消   failed=操作失败              duplicate_skipped=重复请求已跳过
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

RISK_LOW = "low"        # 只读操作，自动放行，可自动重试
RISK_MEDIUM = "medium"  # 控制普通设备，必须用户确认，绝不自动重试
RISK_HIGH = "high"      # 高风险（锁/燃气/电热等），默认禁止

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_DECLINED = "declined"
STATUS_UNKNOWN = "unknown"

STAGE_DONE = "done"
STAGE_EXECUTED = "executed"
STAGE_VERIFIED = "verified"
STAGE_CANCELLED = "cancelled"
STAGE_FAILED = "failed"
STAGE_DUPLICATE_SKIPPED = "duplicate_skipped"


class ToolTimeoutError(Exception):
    """工具执行超过其时间预算。注意：同步 handler 的线程无法被强制终止，
    超时只代表"不再等待"，实际操作可能仍在后台进行。"""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict  # JSON Schema（顶层 object）
    risk: str
    handler: Callable[[Dict], Dict]  # 入参已校验；失败抛异常，成功返回 data
    timeout_seconds: float = 10.0    # 执行时间预算；0 = 不限（不建议用于控制类）
    retryable: bool = False          # 仅只读操作允许自动重试（离线类瞬时错误）

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
    stage: str = STAGE_DONE

    def to_payload(self) -> Dict:
        return {
            "status": self.status,
            "stage": self.stage,
            "message": self.message,
            "data": self.data,
        }

