"""食材管家测试：过期评估纯函数 + 工具行为（入库/补货/移除/确认风险）。全部离线。"""
import json
from datetime import date, timedelta

import pytest

from butler.confirmation import AutoApproveConfirmer, AutoDenyConfirmer
from butler.devices.base import DeviceError
from butler.household.pantry import (
    build_pantry_tools,
    days_left,
    default_expiry_days,
    evaluate_expiry,
)
from butler.household.store import HouseholdStore
from butler.tools.executor import ToolExecutor
from butler.tools.registry import ToolRegistry

TODAY = date(2026, 11, 1)


@pytest.fixture
def store(tmp_path):
    """覆盖 conftest 的同名 fixture（那是设备模拟库）：这里用家庭台账库。"""
    return HouseholdStore(tmp_path / "household.db")


def make_item(store, name="西红柿", days=7, added=None, quantity="3个"):
    added = added or TODAY.isoformat()
    item_id = store.add_food(name, "蔬菜", quantity, days, "", added)
    return store.get_food(item_id)


# ------------------------------------------------------------- 纯函数

def test_days_left_bands():
    assert days_left({"added_date": "2026-11-01", "expires_in_days": 7}, TODAY) == 7
    assert days_left({"added_date": "2026-10-30", "expires_in_days": 2}, TODAY) == 0
    assert days_left({"added_date": "2026-10-29", "expires_in_days": 2}, TODAY) == -1
    assert days_left({"added_date": "2026-11-01", "expires_in_days": None}, TODAY) is None
    assert days_left({"added_date": "bad-date", "expires_in_days": 3}, TODAY) is None


def test_default_expiry_days_by_category():
    assert default_expiry_days("海鲜") == 3
    assert default_expiry_days("不认识的类目") == 14
    assert default_expiry_days("") == 14


def test_evaluate_expiry_levels():
    expired = {"id": 1, "name": "草莓", "quantity": "", "added_date": "2026-10-25",
               "expires_in_days": 5}
    signal = evaluate_expiry(expired, TODAY)
    assert signal["level"] == "warning"
    assert "已过保质期 2 天" in signal["message"]

    soon = {"id": 2, "name": "牛奶", "quantity": "1盒", "added_date": "2026-10-30",
            "expires_in_days": 4}
    signal = evaluate_expiry(soon, TODAY)
    assert signal["level"] == "info"
    assert "还剩 2 天" in signal["message"] and "（1盒）" in signal["message"]

    today_due = {"id": 3, "name": "豆腐", "quantity": "", "added_date": "2026-10-31",
                 "expires_in_days": 1}
    assert "今天到期" in evaluate_expiry(today_due, TODAY)["message"]

    fine = {"id": 4, "name": "大米", "quantity": "", "added_date": "2026-11-01",
            "expires_in_days": 180}
    assert evaluate_expiry(fine, TODAY, warn_days=2) is None

    untracked = {"id": 5, "name": "酱油", "quantity": "", "added_date": "2026-11-01",
                 "expires_in_days": None}
    assert evaluate_expiry(untracked, TODAY) is None


# --------------------------------------------------------------- 工具

@pytest.fixture
def pantry(store):
    registry = ToolRegistry()
    for spec in build_pantry_tools(store):
        registry.register(spec)
    executor = ToolExecutor(registry, AutoApproveConfirmer(), confirmation_enabled=True)
    return registry, executor, store


def _call(executor, registry, name, args):
    result = executor.execute(name, json.dumps(args, ensure_ascii=False))
    assert result.status == "ok", result.message
    return result.data


def test_tool_risk_levels(pantry):
    registry = pantry[0]
    assert registry.get("add_food_items").risk == "medium"
    assert registry.get("remove_food_item").risk == "medium"
    assert registry.get("list_food_items").risk == "low"


def test_add_food_items_batch(pantry):
    registry, executor, store = pantry
    data = _call(executor, registry, "add_food_items", {"items": [
        {"name": "西红柿", "quantity": "3个", "expires_in_days": 7},
        {"name": "排骨", "category": "肉禽蛋"},
    ]})
    assert data["count"] == 2
    added = {r["name"]: r for r in data["items"]}
    assert added["西红柿"]["action"] == "added"
    assert added["西红柿"]["expires_on"] == (TODAY + timedelta(days=7)).isoformat()
    # 未给保质期 → 类目默认（肉禽蛋 5 天）
    assert added["排骨"]["expires_in_days"] == 5
    assert added["排骨"]["category"] == "肉禽蛋"
    assert len(store.list_food(status="in_stock")) == 2


def test_add_food_restock_resets_added_date(pantry):
    registry, executor, store = pantry
    _call(executor, registry, "add_food_items", {
        "items": [{"name": "西红柿", "quantity": "3个", "expires_in_days": 7}],
    })
    old = store.find_food_in_stock("西红柿")
    # 手动把入库日期拨回 10 天前
    store.update_food(old["id"], added_date="2026-10-22")
    data = _call(executor, registry, "add_food_items", {
        "items": [{"name": "西红柿", "quantity": "5个", "expires_in_days": 7}],
    })
    assert data["items"][0]["action"] == "restocked"
    refreshed = store.get_food(old["id"])
    assert refreshed["quantity"] == "5个"
    assert refreshed["added_date"] == TODAY.isoformat()  # 重置：保质期重新起算
    # 同名仍只有一条在库记录
    assert len([i for i in store.list_food(status="in_stock") if i["name"] == "西红柿"]) == 1


def test_add_food_unknown_category_falls_back(pantry):
    registry, executor, store = pantry
    data = _call(executor, registry, "add_food_items", {
        "items": [{"name": "木耳", "category": "干货", "notes": "用户口语类目"}],
    })
    assert data["items"][0]["category"] == "其他"
    assert store.get_food(data["items"][0]["id"])["notes"] == "用户口语类目"


def test_add_food_invalid_expiry_rejected(pantry):
    registry, executor, _ = pantry
    result = executor.execute("add_food_items", json.dumps(
        {"items": [{"name": "牛奶", "expires_in_days": -3}]}, ensure_ascii=False))
    assert result.status == "error"
    assert "0-3650" in result.message


def test_list_food_filters(pantry):
    registry, executor, store = pantry
    stale = make_item(store, name="草莓", days=3, added="2026-10-28")
    make_item(store, name="大米", days=180)
    store.update_food(stale["id"], status="used_up")

    data = _call(executor, registry, "list_food_items", {})
    assert data["count"] == 1 and data["items"][0]["name"] == "大米"

    data = _call(executor, registry, "list_food_items", {"filter": "all"})
    assert data["count"] == 2

    # 草莓已 used_up 不再出现在临期列表
    data = _call(executor, registry, "list_food_items", {"filter": "expiring"})
    assert data["count"] == 0

    fresh_soon = make_item(store, name="牛奶", days=3, added="2026-10-30")
    data = _call(executor, registry, "list_food_items", {"filter": "expiring"})
    assert [i["name"] for i in data["items"]] == ["牛奶"]
    assert data["items"][0]["days_left"] == 1  # 10-30 入库保质期 3 天 → 11-02 到期


def test_remove_food_item(pantry):
    registry, executor, store = pantry
    item = make_item(store, name="鸡蛋", days=14)
    data = _call(executor, registry, "remove_food_item",
                 {"item_id": item["id"], "reason": "used_up"})
    assert data["result"] == "已用完"
    assert store.get_food(item["id"])["status"] == "used_up"

    result = executor.execute("remove_food_item", json.dumps(
        {"item_id": item["id"], "reason": "discarded"}, ensure_ascii=False))
    assert result.status == "error"  # 已移除的不能再次移除
    assert "无需再次移除" in result.message

    result = executor.execute("remove_food_item", json.dumps(
        {"item_id": 9999, "reason": "used_up"}, ensure_ascii=False))
    assert result.status == "error"
    assert "不存在" in result.message


def test_add_food_requires_confirmation(pantry):
    registry, _, store = pantry
    executor = ToolExecutor(registry, AutoDenyConfirmer(), confirmation_enabled=True)
    result = executor.execute("add_food_items", json.dumps(
        {"items": [{"name": "可乐"}]}, ensure_ascii=False))
    assert result.status == "declined"
    assert store.list_food(status="in_stock") == []


def test_confirmation_summary_shows_item_names(pantry):
    """确认摘要必须带具体清单：用户不能对着一个工具名盲签。"""
    registry, _, _ = pantry
    seen = {}

    class RecordingConfirmer:
        def confirm(self, summary, arguments):
            seen["summary"] = summary
            return True

    executor = ToolExecutor(registry, RecordingConfirmer(), confirmation_enabled=True)
    executor.execute("add_food_items", json.dumps(
        {"items": [{"name": "西红柿"}, {"name": "排骨"}]}, ensure_ascii=False))
    assert "共 2 项" in seen["summary"]
    assert "西红柿" in seen["summary"] and "排骨" in seen["summary"]
