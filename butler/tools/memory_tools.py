"""记忆工具集（阶段 5）。

安全约束：
  - save_memory / update_memory / delete_memory 均为 MEDIUM 风险：必须经用户确认，
    模型不能悄悄写入或篡改记忆；
  - 保存内容一律标记 source=user（用户确认即视为用户陈述），推断信息不得入库；
  - list/export 为只读。
"""
from __future__ import annotations

from typing import Dict, List

from ..memory.store import MemoryStore
from .base import RISK_LOW, RISK_MEDIUM, ToolSpec

_KIND_ENUM = ["preference", "fact"]


def build_memory_tools(memory_store: MemoryStore) -> List[ToolSpec]:
    def _save_memory(args: Dict) -> Dict:
        memory_id = memory_store.add(
            kind=args["kind"], content=args["content"], source="user"
        )
        return {"memory_id": memory_id, "saved": True, "content": args["content"]}

    def _list_memories(args: Dict) -> Dict:
        memories = memory_store.list(kind=args.get("kind"))
        return {"memories": memories, "count": len(memories)}

    def _update_memory(args: Dict) -> Dict:
        updated = memory_store.update(args["memory_id"], args["content"])
        if not updated:
            from ..devices.base import DeviceError

            raise DeviceError(f"记忆 #{args['memory_id']} 不存在，无法修改")
        return {"memory_id": args["memory_id"], "updated": True}

    def _delete_memory(args: Dict) -> Dict:
        deleted = memory_store.delete(args["memory_id"])
        if not deleted:
            from ..devices.base import DeviceError

            raise DeviceError(f"记忆 #{args['memory_id']} 不存在，无法删除")
        return {"memory_id": args["memory_id"], "deleted": True}

    def _export_memories(args: Dict) -> Dict:
        return memory_store.export()

    return [
        ToolSpec(
            name="save_memory",
            description=(
                "保存用户明确陈述的长期偏好或事实（如作息偏好、称呼、家庭习惯）。"
                "只允许保存用户亲口确认的信息，禁止保存你的推测。保存前会请求用户确认。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": _KIND_ENUM,
                        "description": "preference=用户偏好；fact=家庭事实",
                    },
                    "content": {"type": "string", "description": "一句话记忆内容"},
                },
                "required": ["kind", "content"],
                "additionalProperties": False,
            },
            risk=RISK_MEDIUM,
            handler=_save_memory,
            timeout_seconds=5.0,
        ),
        ToolSpec(
            name="list_memories",
            description="列出已保存的长期记忆（可按类型过滤）。",
            parameters={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": _KIND_ENUM, "description": "可选过滤"}
                },
                "required": [],
                "additionalProperties": False,
            },
            risk=RISK_LOW,
            handler=_list_memories,
            timeout_seconds=5.0,
            retryable=True,
        ),
        ToolSpec(
            name="update_memory",
            description="修改一条已保存的记忆内容（用户纠正错误记忆时使用），需用户确认。",
            parameters={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "integer", "description": "记忆 ID"},
                    "content": {"type": "string", "description": "新的记忆内容"},
                },
                "required": ["memory_id", "content"],
                "additionalProperties": False,
            },
            risk=RISK_MEDIUM,
            handler=_update_memory,
            timeout_seconds=5.0,
        ),
        ToolSpec(
            name="delete_memory",
            description="删除一条已保存的记忆（删除后不再参与任何决策），需用户确认。",
            parameters={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "integer", "description": "记忆 ID"}
                },
                "required": ["memory_id"],
                "additionalProperties": False,
            },
            risk=RISK_MEDIUM,
            handler=_delete_memory,
            timeout_seconds=5.0,
        ),
        ToolSpec(
            name="export_memories",
            description="导出全部记忆与任务记录（JSON）。",
            parameters={"type": "object", "properties": {}, "required": []},
            risk=RISK_LOW,
            handler=_export_memories,
            timeout_seconds=5.0,
            retryable=True,
        ),
    ]
