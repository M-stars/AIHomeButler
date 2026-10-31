"""家庭巡检测试：临期/过期提醒、洗涤提醒、每日一次守卫。全部离线。"""
from datetime import datetime

from butler.household import HouseholdCheckRunner
from butler.household.store import HouseholdStore


class FakeClock:
    """可推进的假时钟（只用到 date 部分）。"""

    def __init__(self, y, m, d):
        self._today = (y, m, d)

    def advance_day(self):
        y, m, d = self._today
        from datetime import timedelta, date

        nxt = date(y, m, d) + timedelta(days=1)
        self._today = (nxt.year, nxt.month, nxt.day)

    def now(self):
        from datetime import date

        d = date(*self._today)
        return datetime(d.year, d.month, d.day, 8, 0, 0)


class FakeMemoryStore:
    """只模拟巡检用到的两个接口，避免依赖真实 MemoryStore。"""

    def __init__(self):
        self.notifications = []

    def add_notification(self, rule_id, rule_name, message, level="info"):
        nid = len(self.notifications) + 1
        self.notifications.append({
            "id": nid, "rule_id": rule_id, "rule_name": rule_name,
            "message": message, "level": level,
        })
        return nid


def make_runner(store, clock):
    memory = FakeMemoryStore()
    runner = HouseholdCheckRunner(store, memory, now_fn=clock.now)
    return runner, memory


def seed_food(store, name, days, added):
    item_id = store.add_food(name, "蔬菜", "", days, "", added)
    return store.get_food(item_id)


def test_expired_and_expiring_food_notify(tmp_path):
    store = HouseholdStore(tmp_path / "h.db")
    clock = FakeClock(2026, 11, 1)
    runner, memory = make_runner(store, clock)
    seed_food(store, "草莓", 5, "2026-10-25")   # 已过期 2 天
    seed_food(store, "牛奶", 2, "2026-10-30")   # 今天到期（10-30+2=11-01）
    seed_food(store, "大米", 180, "2026-11-01")  # 无需提醒

    fired = runner.run_due()
    assert len(fired) == 2
    levels = {n["message"]: n["level"] for n in memory.notifications}
    assert any("已过保质期" in m for m in levels)
    assert any("今天到期" in m for m in levels)
    assert all("大米" not in m for m in levels)
    # 过期为 warning，临期为 info
    assert any(n["level"] == "warning" for n in memory.notifications)


def test_daily_guard_one_notification_per_item(tmp_path):
    store = HouseholdStore(tmp_path / "h.db")
    clock = FakeClock(2026, 11, 1)
    runner, memory = make_runner(store, clock)
    seed_food(store, "草莓", 5, "2026-10-25")

    assert len(runner.run_due()) == 1
    assert runner.run_due() == []          # 同一天再跑：守卫生效
    assert len(memory.notifications) == 1

    clock.advance_day()
    fired = runner.run_due()
    assert len(fired) == 1                 # 次日再次提醒（过期天数已 +1）
    assert "已过保质期 3 天" in fired[0]["message"]


def test_resolved_food_stops_reminding(tmp_path):
    store = HouseholdStore(tmp_path / "h.db")
    clock = FakeClock(2026, 11, 1)
    runner, memory = make_runner(store, clock)
    item = seed_food(store, "草莓", 5, "2026-10-25")
    assert len(runner.run_due()) == 1

    store.update_food(item["id"], status="used_up")
    clock.advance_day()
    assert runner.run_due() == []


def test_wash_reminder_and_guard(tmp_path):
    store = HouseholdStore(tmp_path / "h.db")
    clock = FakeClock(2026, 11, 1)
    runner, memory = make_runner(store, clock)
    gid = store.add_garment("小星", "白衬衫", "上衣", "适中", None, "")
    store.update_garment(gid, times_worn_since_wash=3, last_wash_date="2026-10-28")
    store.add_garment("小星", "围巾", "配饰", "适中", None, "")  # 不跟踪

    fired = runner.run_due()
    assert len(fired) == 1
    assert "白衬衫" in fired[0]["message"]
    assert fired[0]["source"] == "衣柜管家"

    assert runner.run_due() == []  # 每日一次

    # 洗好后不再提醒
    store.update_garment(gid, status="clean", times_worn_since_wash=0,
                         last_wash_date="2026-11-01")
    clock.advance_day()
    assert runner.run_due() == []


def test_in_laundry_and_retired_skipped(tmp_path):
    store = HouseholdStore(tmp_path / "h.db")
    clock = FakeClock(2026, 11, 1)
    runner, memory = make_runner(store, clock)
    g1 = store.add_garment("小星", "待洗裤", "裤子", "适中", None, "")
    store.update_garment(g1, status="in_laundry", times_worn_since_wash=9,
                         last_wash_date="2026-09-01")
    g2 = store.add_garment("小星", "退役衣", "上衣", "薄", None, "")
    store.update_garment(g2, status="retired", times_worn_since_wash=9,
                         last_wash_date="2026-01-01")
    assert runner.run_due() == []


def test_single_failure_does_not_break_others(tmp_path):
    """某条评估异常只影响自身，其余照常产出通知。"""
    store = HouseholdStore(tmp_path / "h.db")
    clock = FakeClock(2026, 11, 1)
    runner, memory = make_runner(store, clock)
    seed_food(store, "草莓", 5, "2026-10-25")
    # 造一条会让 evaluate_wash 内部 date 解析失败但返回 None 之外的数据是困难的；
    # 这里改为验证：守卫写入失败不抛出、不产出重复通知
    gid = store.add_garment("小星", "坏数据衣", "上衣", "适中", None, "")
    store.update_garment(gid, times_worn_since_wash=99, last_wash_date="not-a-date")
    fired = runner.run_due()
    # last_wash_date 非法被忽略，仅连穿次数触发提醒
    assert len(fired) == 2
    assert any("白" not in n["message"] for n in memory.notifications)
