"""后台主动检查调度器（修复"主动服务其实不主动"）。

一个 daemon 线程按固定间隔调用 RuleEngine.run_checks()，新通知交给 sink 回调。
没有它，提醒只会在用户说话后/前端轮询时才产生——用户不说话管家就永远沉默。

设计约束：
  - daemon=True：主进程退出即结束，无需优雅关闭；stop() 仅供测试与热重载；
  - 循环体与 sink 的任何异常都只记日志，线程绝不因单次错误死亡；
  - 重复 start() 是幂等的（同一时刻最多一个调度线程）；
  - 冷却机制在 RuleEngine 内部，调度高频调用不会造成通知轰炸。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

DEFAULT_INTERVAL_SECONDS = 60.0
_MIN_INTERVAL_SECONDS = 10.0

_log = logging.getLogger("butler.scheduler")


def chain_ticks(*ticks):
    """把多个每轮任务（如例行任务执行器、家庭巡检）串成一个 extra_tick。

    单个 tick 抛异常只记日志并继续其余 tick——扩展点不能因一个扩展死亡。
    返回所有 tick 产生的通知列表，统一交给 sink。
    """
    def _run_all():
        fired = []
        for tick in ticks:
            if tick is None:
                continue
            try:
                fired.extend(tick())
            except Exception:
                _log.exception("调度附加任务（extra_tick）执行失败")
        return fired
    return _run_all


class RuleScheduler:
    def __init__(
        self,
        rule_engine,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        sink: Optional[Callable[[dict], None]] = None,
        min_interval_seconds: float = _MIN_INTERVAL_SECONDS,
        extra_tick: Optional[Callable[[], list]] = None,
    ) -> None:
        self._engine = rule_engine
        self._interval = max(min_interval_seconds, float(interval_seconds))
        self._sink = sink
        # 附加的每轮任务（如例行任务执行器 run_due）；返回的通知同样走 sink
        self._extra_tick = extra_tick
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def start(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return  # 幂等：已在运行
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="butler-proactive", daemon=True
            )
            self._thread.start()
            _log.info("主动服务后台调度已启动（间隔 %ss）", self._interval)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            # 立即先检查一次：进程启动时可能已有满足条件的规则
            try:
                fired = self._engine.run_checks()
                if self._extra_tick is not None:
                    try:
                        fired = fired + list(self._extra_tick())
                    except Exception:
                        _log.exception("附加调度任务（extra_tick）执行失败")
                if self._sink is not None:
                    for notification in fired:
                        try:
                            self._sink(notification)
                        except Exception:
                            _log.exception("主动通知投递失败（sink 异常）")
            except Exception:
                _log.exception("主动规则后台检查失败")
            self._stop.wait(self._interval)
