"""家庭生活服务（阶段 14.7）：食材管家 + 衣柜管家。

两个功能共用"物品台账 + 时间跟踪 + 反馈"骨架：
  - 食材：入库/补货 → 保质期倒计时 → 临期/过期每日提醒 → 用掉/丢弃登记；
  - 衣物：登记 → 穿着记账 → 洗涤提醒 → 洗后重置 → 穿搭推荐（天气+反馈）。

对外入口：
  - build_pantry_tools / build_wardrobe_tools：注册进工具注册表；
  - HouseholdCheckRunner：挂 RuleScheduler.extra_tick 做每日巡检；
  - WeatherProvider：Open-Meteo 免密钥天气（未配置坐标时优雅降级）。
"""
from .checks import DEFAULT_WARN_DAYS, HouseholdCheckRunner
from .pantry import (
    CATEGORY_EXPIRY_DAYS,
    FOOD_CATEGORIES,
    build_pantry_tools,
    days_left,
    default_expiry_days,
    evaluate_expiry,
)
from .store import HouseholdStore, normalize_name
from .wardrobe import (
    CATEGORY_WASH_POLICY,
    GARMENT_CATEGORIES,
    WARMTH_LEVELS,
    build_wardrobe_tools,
    evaluate_wash,
    resolve_owner,
    wash_policy,
)
from .weather import WeatherProvider, WeatherUnavailable

__all__ = [
    "DEFAULT_WARN_DAYS",
    "HouseholdCheckRunner",
    "HouseholdStore",
    "normalize_name",
    "CATEGORY_EXPIRY_DAYS",
    "FOOD_CATEGORIES",
    "build_pantry_tools",
    "days_left",
    "default_expiry_days",
    "evaluate_expiry",
    "CATEGORY_WASH_POLICY",
    "GARMENT_CATEGORIES",
    "WARMTH_LEVELS",
    "build_wardrobe_tools",
    "evaluate_wash",
    "resolve_owner",
    "wash_policy",
    "WeatherProvider",
    "WeatherUnavailable",
]
