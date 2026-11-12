"""衣柜管家测试：洗涤策略、穿着记账幂等、归属解析、推荐数据聚合。全部离线。"""
import json
from datetime import date

import pytest

from butler.confirmation import AutoApproveConfirmer
from butler.household.store import HouseholdStore
from butler.household.wardrobe import (
    build_wardrobe_tools,
    evaluate_wash,
    resolve_owner,
    wash_policy,
)
from butler.household.weather import WeatherProvider, WeatherUnavailable
from butler.tools.executor import ToolExecutor
from butler.tools.registry import ToolRegistry

TODAY = date(2026, 11, 1)


class FakeMemberContext:
    def __init__(self, member=None):
        self._member = member

    def get(self):
        return self._member


class FakeWeather:
    """可编程天气：可设为成功/失败/未启用三种行为。"""

    def __init__(self, summary=None, error=None):
        self.summary = summary
        self.error = error
        self.calls = 0

    def current_summary(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.summary


@pytest.fixture
def store(tmp_path):
    """覆盖 conftest 的同名 fixture（那是设备模拟库）：这里用家庭台账库。"""
    return HouseholdStore(tmp_path / "household.db")


@pytest.fixture
def wardrobe(store):
    registry = ToolRegistry()
    fake_weather = FakeWeather(summary={
        "temperature_c": 12.0, "condition": "阴", "temp_max_c": 15.0,
        "temp_min_c": 8.0, "rain_probability_percent": 40, "city": "杭州",
    })
    for spec in build_wardrobe_tools(
        store, fake_weather, member_context=FakeMemberContext()
    ):
        registry.register(spec)
    executor = ToolExecutor(registry, AutoApproveConfirmer(), confirmation_enabled=True)
    return registry, executor, store, fake_weather


def _call(executor, registry, name, args):
    result = executor.execute(name, json.dumps(args, ensure_ascii=False))
    assert result.status == "ok", result.message
    return result.data


# ------------------------------------------------------------- 纯函数

def test_wash_policy_defaults_and_override():
    assert wash_policy({"category": "内衣"}) == {"interval": 1, "max_wears": 1}
    assert wash_policy({"category": "配饰"}) is None
    assert wash_policy({"category": "上衣"}) == {"interval": 3, "max_wears": 3}
    # 单件覆盖天数维度，次数维度保留类目默认
    assert wash_policy({"category": "上衣", "wash_interval_days": 2}) == \
        {"interval": 2, "max_wears": 3}
    assert wash_policy({"category": "未知", "wash_interval_days": None}) == \
        {"interval": 7, "max_wears": 5}


def test_evaluate_wash_signals():
    wears_exceeded = {"id": 1, "name": "白衬衫", "owner": "小星", "category": "上衣",
                      "wash_interval_days": None, "times_worn_since_wash": 3,
                      "last_wash_date": "2026-10-30"}
    signal = evaluate_wash(wears_exceeded, TODAY)
    assert signal is not None
    assert "已连穿 3 次" in signal["message"]

    days_exceeded = {"id": 2, "name": "外套", "owner": "爷爷", "category": "外套",
                     "wash_interval_days": None, "times_worn_since_wash": 1,
                     "last_wash_date": "2026-10-01"}
    signal = evaluate_wash(days_exceeded, TODAY)
    assert "距上次洗涤 31 天" in signal["message"]

    clean_recent = {"id": 3, "name": "新裤子", "owner": "小星", "category": "裤子",
                    "wash_interval_days": None, "times_worn_since_wash": 1,
                    "last_wash_date": "2026-10-31"}
    assert evaluate_wash(clean_recent, TODAY) is None

    # 从未洗涤也从未穿着：没有起算点，不打扰
    virgin = {"id": 4, "name": "新衣", "owner": "小星", "category": "上衣",
              "wash_interval_days": None, "times_worn_since_wash": 0,
              "last_wash_date": None}
    assert evaluate_wash(virgin, TODAY) is None

    # 配饰不跟踪
    assert evaluate_wash({"id": 5, "name": "围巾", "owner": "小星", "category": "配饰",
                          "times_worn_since_wash": 99, "last_wash_date": "2020-01-01"},
                         TODAY) is None


def test_resolve_owner_priority():
    member_ctx = FakeMemberContext({"member_id": "m1", "display_name": "小星"})
    assert resolve_owner("爷爷", member_ctx) == "爷爷"
    assert resolve_owner(None, member_ctx) == "小星"
    assert resolve_owner("  ", member_ctx) == "小星"
    assert resolve_owner(None, FakeMemberContext(None)) == "我"
    assert resolve_owner(None, None) == "我"


# --------------------------------------------------------------- 工具

def test_add_garments_and_risk_levels(wardrobe):
    registry, executor, store, _ = wardrobe
    assert registry.get("add_garments").risk == "medium"
    assert registry.get("log_wear").risk == "low"
    assert registry.get("recommend_outfit").risk == "low"

    data = _call(executor, registry, "add_garments", {"items": [
        {"name": "蓝色卫衣", "category": "上衣", "warmth": "厚"},
        {"name": "牛仔裤", "category": "裤子"},
    ]})
    assert data["owner"] == "我"  # 无成员上下文、未显式指定 → 默认「我」
    assert data["count"] == 2
    garments = store.list_garments(owner="我")
    assert [g["category"] for g in sorted(garments, key=lambda g: g["name"])] == ["裤子", "上衣"]


def test_add_garments_with_member_context(wardrobe):
    registry, executor, store, _ = wardrobe
    registry2 = ToolRegistry()
    for spec in build_wardrobe_tools(
        store, FakeWeather(),
        member_context=FakeMemberContext({"member_id": "m1", "display_name": "小星"}),
    ):
        registry2.register(spec)
    executor2 = ToolExecutor(registry2, AutoApproveConfirmer(), confirmation_enabled=True)
    _call(executor2, registry2, "add_garments",
          {"items": [{"name": "小红裙", "category": "裙子"}]})
    assert store.find_garment("小星", "小红裙") is not None


def test_add_garments_re_register_updates(wardrobe):
    registry, executor, store, _ = wardrobe
    _call(executor, registry, "add_garments",
          {"items": [{"name": "风衣", "category": "外套", "warmth": "薄"}]})
    _call(executor, registry, "add_garments",
          {"items": [{"name": "风衣", "category": "外套", "warmth": "厚"}]})
    all_garments = store.list_garments(owner="我")
    assert len(all_garments) == 1
    assert all_garments[0]["warmth"] == "厚"


def test_log_wear_idempotent_and_unmatched(wardrobe):
    registry, executor, store, _ = wardrobe
    _call(executor, registry, "add_garments",
          {"items": [{"name": "蓝T恤", "category": "上衣"}]})
    gid = store.find_garment("我", "蓝t恤")["id"]

    data = _call(executor, registry, "log_wear", {"names": ["蓝T恤", "不存在的裤子"]})
    assert [g["name"] for g in data["logged"]] == ["蓝T恤"]
    assert data["unmatched"] == ["不存在的裤子"]

    # 同一天重复登记 → 幂等跳过，不重复计数
    data = _call(executor, registry, "log_wear", {"names": ["蓝T恤"]})
    assert data["skipped"] and data["logged"] == []
    assert store.get_garment(gid)["times_worn_since_wash"] == 1

    # 指定日期登记有效
    data = _call(executor, registry, "log_wear",
                 {"names": ["蓝T恤"], "worn_on": "2026-10-31"})
    assert data["logged"]
    assert store.get_garment(gid)["times_worn_since_wash"] == 2


def test_log_wear_invalid_date(wardrobe):
    registry, executor, _, _ = wardrobe
    result = executor.execute("log_wear", json.dumps(
        {"names": ["T恤"], "worn_on": "11月1日"}, ensure_ascii=False))
    assert result.status == "error"
    assert "YYYY-MM-DD" in result.message


def test_mark_washed_resets(wardrobe):
    registry, executor, store, _ = wardrobe
    _call(executor, registry, "add_garments",
          {"items": [{"name": "白衬衫", "category": "上衣"}]})
    gid = store.find_garment("我", "白衬衫")["id"]
    _call(executor, registry, "log_wear", {"names": ["白衬衫"]})
    _call(executor, registry, "log_wear", {"names": ["白衬衫"], "worn_on": "2026-10-31"})
    assert store.get_garment(gid)["times_worn_since_wash"] == 2

    data = _call(executor, registry, "mark_washed", {"names": ["白衬衫"]})
    assert [g["name"] for g in data["washed"]] == ["白衬衫"]
    g = store.get_garment(gid)
    assert g["status"] == "clean"
    assert g["times_worn_since_wash"] == 0
    assert g["last_wash_date"] == TODAY.isoformat()
    assert data["unmatched"] == []


def test_outfit_feedback_targets_garment(wardrobe):
    registry, executor, store, _ = wardrobe
    _call(executor, registry, "add_garments",
          {"items": [{"name": "牛仔裤", "category": "裤子"}]})
    data = _call(executor, registry, "set_outfit_feedback",
                 {"content": "这条裤子很暖和", "garment_name": "牛仔裤"})
    assert data["saved"]
    feedback = store.list_outfit_feedback(owner="我")
    assert feedback[0]["garment_id"] == store.find_garment("我", "牛仔裤")["id"]

    result = executor.execute("set_outfit_feedback", json.dumps(
        {"content": "好看", "garment_name": "不存在的裙子"}, ensure_ascii=False))
    assert result.status == "error"
    assert "没有找到" in result.message


def test_recommend_outfit_aggregates(wardrobe):
    registry, executor, store, fake_weather = wardrobe
    _call(executor, registry, "add_garments", {"items": [
        {"name": "厚毛衣", "category": "上衣", "warmth": "厚"},
        {"name": "短裤", "category": "裤子", "warmth": "薄"},
    ]})
    _call(executor, registry, "set_outfit_feedback", {"content": "毛衣很扎人"})

    data = _call(executor, registry, "recommend_outfit", {"occasion": "上学"})
    assert fake_weather.calls == 1
    assert data["weather"]["temperature_c"] == 12.0
    assert data["weather_note"] == ""
    assert data["occasion"] == "上学"
    assert data["weekday"] in "一二三四五六日"
    names = {g["name"] for g in data["garments"]}
    assert names == {"厚毛衣", "短裤"}
    assert data["recent_feedback"][0]["content"] == "毛衣很扎人"

    # 二次调用走缓存：FakeWeather 不经过 WeatherProvider 缓存，但工具不应重复拉取
    # （此断言在 test_recommend_outfit_weather_degradation 里覆盖 provider 缓存）


def test_recommend_outfit_weather_degradation(tmp_path):
    store = HouseholdStore(tmp_path / "household.db")
    failing = FakeWeather(error=WeatherUnavailable("网络不可达"))
    registry = ToolRegistry()
    for spec in build_wardrobe_tools(store, failing):
        registry.register(spec)
    executor = ToolExecutor(registry, AutoApproveConfirmer(), confirmation_enabled=True)
    data = _call(executor, registry, "recommend_outfit", {})
    assert data["weather"] is None
    assert "天气信息不可用" in data["weather_note"]
    assert "网络不可达" in data["weather_note"]
    assert data["garments"] == []


def test_recommend_outfit_uses_provider_cache(tmp_path):
    """WeatherProvider 缓存生效：两次推荐只发起一次真实天气请求。"""
    store = HouseholdStore(tmp_path / "household.db")
    calls = {"n": 0}

    def fake_fetcher(url, timeout):
        calls["n"] += 1
        return json.dumps({
            "current": {"temperature_2m": 20.0, "weather_code": 1},
            "daily": {"temperature_2m_max": [24.0], "temperature_2m_min": [16.0],
                      "precipitation_probability_max": [10]},
        }).encode("utf-8")

    provider = WeatherProvider(lat="30.27", lon="120.15", city="杭州",
                               cache_seconds=1800, fetcher=fake_fetcher)
    registry = ToolRegistry()
    for spec in build_wardrobe_tools(store, provider):
        registry.register(spec)
    executor = ToolExecutor(registry, AutoApproveConfirmer(), confirmation_enabled=True)
    _call(executor, registry, "recommend_outfit", {})
    _call(executor, registry, "recommend_outfit", {})
    assert calls["n"] == 1


def test_garment_schema_validation(wardrobe):
    registry, executor, _, _ = wardrobe
    # 缺少必填参数
    result = executor.execute("log_wear", json.dumps({}, ensure_ascii=False))
    assert result.status == "error"
    assert "缺少必填参数" in result.message
    # 未知参数拒绝
    result = executor.execute("add_garments", json.dumps(
        {"items": [{"name": "T"}], "hacker": 1}, ensure_ascii=False))
    assert result.status == "error"
    assert "不接受未知参数" in result.message
