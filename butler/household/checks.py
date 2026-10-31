"""家庭生活服务每日巡检（阶段 14.7）：食材临期/过期 + 衣物该洗，纯通知。

挂载点：RuleScheduler 的 extra_tick（与例行任务执行器并列），每轮调度
（默认 60s）调用一次 run_due()。本模块自己保证"每项每天至多提醒一次"
（last_remind_date 守卫，重启安全），调度高频调用不会造成通知轰炸。

通知走 MemoryStore.add_notification（rule_id=None），与主动服务规则
的通知同一条通道：Web 页面/语音播报零改动即可看到。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from .pantry import evaluate_expiry
from .store import HouseholdStore
from .wardrobe import evaluate_wash

_log = logging.getLogger("butler.household")

DEFAULT_WARN_DAYS = 2


class HouseholdCheckRunner:
    def __init__(
        self,
        store: HouseholdStore,
        memory_store,
        now_fn: Callable[[], datetime] = datetime.now,
        warn_days: int = DEFAULT_WARN_DAYS,
        logger: logging.Logger = None,
    ) -> None:
        self._store = store
        self._memory = memory_store
        self._now_fn = now_fn
        self._warn_days = max(0, int(warn_days))
        self._log = logger or _log

    def run_due(self) -> List[Dict[str, Any]]:
        """检查全部在库食材与干净衣物，返回本轮新产生的通知（已落库）。

        任何单条失败都不影响其余检查（异常只记日志）。
        """
        today = self._now_fn().date()
        fired: List[Dict[str, Any]] = []
        for source, items, evaluate, mark in (
            ("食材管家", self._store.list_food(status="in_stock"),
             evaluate_expiry, self._store.update_food),
            ("衣柜管家", self._store.list_garments(status="clean"),
             evaluate_wash, self._store.update_garment),
        ):
            for item in items:
                try:
                    signal = evaluate(item, today)
                except Exception:
                    self._log.exception("巡检评估异常：%s #%s", source, item.get("id"))
                    continue
                if signal is None:
                    continue
                if (item.get("last_remind_date") or "") == today.isoformat():
                    continue  # 今天已提醒过：每项每天至多一次
                try:
                    mark(item["id"], last_remind_date=today.isoformat())
                except Exception:
                    self._log.exception("巡检守卫写入失败：%s #%s", source, item.get("id"))
                    continue
                fired.append(self._notify(source, signal))
        return fired

    def _notify(self, source: str, signal: Dict[str, Any]) -> Dict[str, Any]:
        notification_id = self._memory.add_notification(
            rule_id=None, rule_name=source,
            message=signal["message"], level=signal.get("level", "info"),
        )
        self._log.info("家庭巡检通知 [%s] %s", source, signal["message"])
        return {
            "id": notification_id,
            "source": source,
            "message": signal["message"],
            "level": signal.get("level", "info"),
        }
