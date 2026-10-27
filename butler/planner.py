"""任务规划与多步骤执行（阶段 6）。

流程：规划（LLM 输出 JSON 计划）→ 服务端验证（工具必须已注册、参数过 Schema、
步数封顶）→ 顺序执行（复用 ToolExecutor：确认/超时/防重放/验证全部生效）
→ 逐步记录 → 中止即停（不做自动重规划，防止失控）→ 写任务日志。

关键设计：
  - "不得无条件信任模型生成的计划"：任何步骤在执行前整体验证，验证不过整单拒绝，
    绝不出现"执行了一半才发现计划非法"；
  - 控制步骤被用户拒绝 → 剩余步骤全部中止（用户意愿优先）；
  - 出错即中止，已完成/失败/未执行的步骤在报告中如实区分；
  - 不假设设备间存在原子事务。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from .config import ButlerConfig
from .confirmation import ConsoleConfirmer
from .devices.registry import DeviceRegistry
from .llm_client import LLMClient, LLMError, OpenAICompatClient
from .memory.store import MemoryStore
from .tools.executor import ToolExecutor
from .tools.home import build_home_tools
from .tools.memory_tools import build_memory_tools
from .tools.registry import ToolRegistry
from .tools.validation import validate_arguments

_MAX_PLAN_STEPS = 8
_plan_log = logging.getLogger("butler.planner")

_PLANNING_SYSTEM = """你是"AI Home Butler"的任务规划器。把用户的任务拆解为有限的工具调用步骤。

可用工具（JSON Schema）：
{tool_schemas}

当前设备清单：
{device_inventory}

用户长期记忆：
{memory_block}

输出要求（严格 JSON，不要输出其他文字）：
{{"steps": [{{"tool": "工具名", "args": {{参数对象}}, "reason": "这一步做什么"}}]}}

约束：
1. 步数不超过 {max_steps}；只使用列出的工具；参数必须符合对应 Schema。
2. 需要先了解状态的，先放查询步骤。
3. 涉及用户长期偏好的，先读取记忆（list_memories）再决定参数。
4. 如果任务超出工具能力，返回 {{"steps": [], "explanation": "原因"}}。
5. 不要包含任何需要确认的猜测参数；控制步骤的参数必须明确。
"""

_SUMMARY_SYSTEM = """你是 AI 管家的任务汇报员。根据任务与逐步执行结果，用简体中文向用户汇报：
完成了什么、设备当前状态、哪些没完成及原因。不要编造未发生的结果。"""


class PlanRunner:
    def __init__(
        self,
        llm: LLMClient,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        device_registry: DeviceRegistry,
        memory_store: Optional[MemoryStore] = None,
        max_steps: int = _MAX_PLAN_STEPS,
        logger: logging.Logger = None,
    ) -> None:
        self._llm = llm
        self._tool_registry = tool_registry
        self._executor = tool_executor
        self._devices = device_registry
        self._memory = memory_store
        self._max_steps = max(1, int(max_steps))
        self._log = logger or _plan_log

    @classmethod
    def for_cli(
        cls,
        config: ButlerConfig,
        registry: DeviceRegistry,
        memory_store: MemoryStore,
        executor: Optional[ToolExecutor] = None,
    ) -> "PlanRunner":
        """executor 传入时与调用方共用（共享确认器与防重放缓存）；
        不传则使用独立的控制台确认执行器。"""
        tool_registry = ToolRegistry()
        for spec in build_home_tools(registry):
            tool_registry.register(spec)
        for spec in build_memory_tools(memory_store):
            tool_registry.register(spec)
        for spec in build_proactive_tools(memory_store, device_registry=registry):
            tool_registry.register(spec)
        if executor is None:
            executor = ToolExecutor(tool_registry, ConsoleConfirmer())
        llm = OpenAICompatClient(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            timeout_seconds=config.llm_timeout_seconds,
            max_retries=config.llm_max_retries,
        )
        return cls(llm, tool_registry, executor, registry, memory_store)

    # ------------------------------------------------------------------ 主流程

    def run(
        self,
        user_text: str,
        on_step: Optional[Callable[[Dict], None]] = None,
    ) -> Dict[str, Any]:
        """执行一个多步骤任务，返回 {"status", "steps", "summary"}。"""
        plan = self._plan(user_text)
        if plan is None:
            report = {
                "status": "failed",
                "steps": [],
                "summary": "任务规划失败：模型两次都未输出有效的 JSON 计划，任务未执行。",
            }
            self._record(user_text, report)
            return report
        if not plan["steps"]:
            report = {
                "status": "failed",
                "steps": [],
                "summary": f"无法规划该任务：{plan.get('explanation', '超出当前工具能力')}",
            }
            self._record(user_text, report)
            return report

        validation_error = self._validate_plan(plan["steps"])
        if validation_error:
            report = {
                "status": "failed",
                "steps": [],
                "summary": f"计划未通过服务端验证，任务未执行任何一步：{validation_error}",
            }
            self._record(user_text, report)
            return report

        step_records = self._execute_steps(plan["steps"], on_step)
        status = self._verdict(step_records)
        report = {"status": status, "steps": step_records, "summary": ""}
        report["summary"] = self._summarize(user_text, report)
        self._record(user_text, report)
        return report

    # ------------------------------------------------------------------ 规划

    def _plan(self, user_text: str) -> Optional[Dict[str, Any]]:
        prompt = _PLANNING_SYSTEM.format(
            tool_schemas=json.dumps(self._tool_registry.openai_schemas(), ensure_ascii=False, indent=1),
            device_inventory=self._inventory_text(),
            memory_block=self._memory.prompt_block() if self._memory else "（无）",
            max_steps=self._max_steps,
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_text},
        ]
        last_error = ""
        for attempt in (1, 2):  # 一次自纠机会
            try:
                response = self._llm.chat(messages)
            except LLMError as exc:
                self._log.error("规划阶段模型调用失败：%s", exc)
                return None
            content = response.content or ""
            parsed = self._parse_plan_json(content)
            if parsed is not None:
                return parsed
            last_error = content[:300]
            messages = messages[:2] + [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": "上面的输出不是合法 JSON 或缺少 steps 字段。请重新只输出符合要求的 JSON。",
                },
            ]
        self._log.warning("两次规划输出均无效，最后输出片段：%s", last_error)
        return None

    @staticmethod
    def _parse_plan_json(content: str) -> Optional[Dict[str, Any]]:
        text = content.strip()
        # 剥离可能的 ```json 围栏
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        steps = data.get("steps")
        if steps is None:
            return None
        if not isinstance(steps, list):
            return None
        clean = []
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
                return None
            clean.append(
                {
                    "tool": step["tool"],
                    "args": step.get("args") if isinstance(step.get("args"), dict) else {},
                    "reason": str(step.get("reason", "")),
                }
            )
        return {
            "steps": clean[: _MAX_PLAN_STEPS + 1],  # 先不截断，交给验证报错
            "explanation": str(data.get("explanation", "")),
        }

    # ------------------------------------------------------------------ 验证

    def _validate_plan(self, steps: List[Dict[str, Any]]) -> Optional[str]:
        """执行前整体验证计划；返回错误描述或 None。"""
        if not steps:
            return "计划为空"
        if len(steps) > self._max_steps:
            return f"计划步数 {len(steps)} 超过上限 {self._max_steps}"
        for index, step in enumerate(steps, start=1):
            spec = self._tool_registry.get(step["tool"])
            if spec is None:
                return f"第 {index} 步使用了未注册工具 {step['tool']!r}"
            errors = validate_arguments(step["args"], spec.parameters)
            if errors:
                return f"第 {index} 步（{step['tool']}）参数不合法：{'；'.join(errors)}"
            if spec.risk == "high":
                return f"第 {index} 步（{step['tool']}）属于高风险操作，禁止进入计划"
        return None

    # ------------------------------------------------------------------ 执行

    def _execute_steps(
        self, steps: List[Dict[str, Any]], on_step: Optional[Callable[[Dict], None]]
    ) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for index, step in enumerate(steps, start=1):
            record = {
                "index": index,
                "tool": step["tool"],
                "args": step["args"],
                "reason": step["reason"],
                "status": "pending",
                "message": "",
            }
            try:
                result = self._executor.execute(step["tool"], json.dumps(step["args"], ensure_ascii=False))
                record["status"] = result.status
                record["stage"] = result.stage
                record["message"] = result.message
                record["data"] = result.data
            except Exception as exc:  # 执行器异常兜底
                self._log.exception("步骤 %s 执行异常", index)
                record["status"] = "error"
                record["message"] = f"步骤执行异常：{type(exc).__name__}"
            records.append(record)
            if on_step:
                try:
                    on_step(record)
                except Exception:
                    pass
            if record["status"] in ("error", "declined", "unknown"):
                # 出错/拒绝即中止：剩余步骤保持 pending，如实呈现
                for rest_index, rest in enumerate(steps[index:], start=index + 1):
                    records.append(
                        {
                            "index": rest_index,
                            "tool": rest["tool"],
                            "args": rest["args"],
                            "reason": rest["reason"],
                            "status": "pending",
                            "message": "因前序步骤未成功而中止",
                        }
                    )
                    if on_step:
                        try:
                            on_step(records[-1])
                        except Exception:
                            pass
                break
        return records

    @staticmethod
    def _verdict(step_records: List[Dict[str, Any]]) -> str:
        statuses = {record["status"] for record in step_records}
        if statuses <= {"ok"}:
            return "completed"
        if "declined" in statuses or "pending" in statuses or "unknown" in statuses:
            return "aborted"
        return "failed"

    # ------------------------------------------------------------------ 汇报

    def _summarize(self, user_text: str, report: Dict[str, Any]) -> str:
        """先用模型生成汇报；失败则退化为确定性汇总。"""
        fallback = self._deterministic_summary(report)
        try:
            response = self._llm.chat(
                [
                    {"role": "system", "content": _SUMMARY_SYSTEM},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"任务": user_text, "结果": report["steps"]},
                            ensure_ascii=False,
                        ),
                    },
                ]
            )
            return response.content or fallback
        except LLMError:
            return fallback

    @staticmethod
    def _deterministic_summary(report: Dict[str, Any]) -> str:
        lines = []
        for record in report["steps"]:
            status_label = {
                "ok": "成功", "error": "失败", "declined": "已取消",
                "unknown": "状态未知", "pending": "未执行",
            }.get(record["status"], record["status"])
            lines.append(f"{record['index']}. {record['tool']}：{status_label}（{record['message']}）")
        return "；".join(lines) if lines else "没有执行任何步骤。"

    # ------------------------------------------------------------------ 记录

    def _inventory_text(self) -> str:
        lines = [
            f"- {d.info.id}（{d.info.name}，{d.info.area}，类型 {d.info.device_type}）"
            for d in self._devices.list()
        ]
        return "\n".join(lines) or "（无设备）"

    def _record(self, user_text: str, report: Dict[str, Any]) -> None:
        if self._memory is None:
            return
        try:
            self._memory.add_task_log(user_text, report["status"], report["steps"])
        except Exception:
            self._log.exception("任务日志写入失败")
