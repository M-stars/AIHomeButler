"""P0 回归：后台主动检查调度器——用户不说话也要能提醒。"""
import threading
import time

from butler.scheduler import RuleScheduler


class StubEngine:
    def __init__(self):
        self.calls = 0
        self.notifications = [{"id": 1, "message": "测试提醒"}]

    def run_checks(self):
        self.calls += 1
        return list(self.notifications)


def test_scheduler_checks_periodically_and_delivers_to_sink():
    engine = StubEngine()
    received = []
    scheduler = RuleScheduler(
        engine, interval_seconds=0.1, sink=received.append, min_interval_seconds=0.05
    )
    scheduler.start()
    try:
        deadline = time.time() + 3
        while len(received) < 1 and time.time() < deadline:
            time.sleep(0.05)
        assert engine.calls >= 1, "启动后应立即检查一次"
        assert received and received[0]["message"] == "测试提醒"
        first_calls = engine.calls
        time.sleep(0.35)  # 覆盖至少 2-3 个间隔
        assert engine.calls > first_calls, "必须周期性持续检查，而非只查一次"
    finally:
        scheduler.stop()


def test_scheduler_stop_terminates_loop():
    engine = StubEngine()
    scheduler = RuleScheduler(
        engine, interval_seconds=0.1, min_interval_seconds=0.05
    )
    scheduler.start()
    time.sleep(0.2)
    scheduler.stop(timeout=2.0)
    assert scheduler._thread is None
    calls_at_stop = engine.calls
    time.sleep(0.3)
    assert engine.calls == calls_at_stop, "stop 后不得继续检查"


def test_scheduler_survives_engine_exceptions():
    class BrokenEngine:
        def __init__(self):
            self.calls = 0

        def run_checks(self):
            self.calls += 1
            raise RuntimeError("故意失败")

    engine = BrokenEngine()
    scheduler = RuleScheduler(engine, interval_seconds=0.1, min_interval_seconds=0.05)
    scheduler.start()
    try:
        time.sleep(0.35)
        assert engine.calls >= 2, "单次异常不得杀死调度线程"
    finally:
        scheduler.stop()


def test_scheduler_sink_exception_does_not_kill_loop():
    def bad_sink(notification):
        raise RuntimeError("sink 故障")

    engine = StubEngine()
    scheduler = RuleScheduler(
        engine, interval_seconds=0.1, sink=bad_sink, min_interval_seconds=0.05
    )
    scheduler.start()
    try:
        time.sleep(0.35)
        assert engine.calls >= 2, "sink 异常不得杀死调度线程"
    finally:
        scheduler.stop()


def test_scheduler_start_is_idempotent():
    engine = StubEngine()
    scheduler = RuleScheduler(engine, interval_seconds=0.2, min_interval_seconds=0.05)
    scheduler.start()
    try:
        scheduler.start()  # 第二次 start 不应产生第二个线程
        time.sleep(0.5)
        threads = [t for t in threading.enumerate() if t.name == "butler-proactive"]
        assert len(threads) == 1, f"应只有 1 个调度线程，实际 {len(threads)}"
    finally:
        scheduler.stop()
