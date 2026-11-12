"""天气提供方（阶段 14.7）：Open-Meteo 免密钥接口，穿搭推荐用。

设计约束：
  - 不新增依赖：urllib 标准库直连，超时短（默认 5s）；
  - 不配置坐标 = 功能关闭（enabled=False），绝不猜测用户位置；
  - 任何失败（网络/超时/解析）都抛 WeatherUnavailable，由调用方降级——
    穿搭推荐没有天气也能给建议，只是如实说明"未考虑天气"；
  - 结果按 cache_seconds 缓存（默认 30 分钟）：后台巡检与多轮对话
    共享，避免对公网服务的无谓请求。
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Callable, Dict, Optional

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes → 中文短语（只覆盖常用段，其余归"多变天气"）
_WMO_CONDITIONS = {
    0: "晴", 1: "大致晴朗", 2: "局部多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "浓毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}


class WeatherUnavailable(Exception):
    """天气不可用（未配置/网络失败/数据异常）。调用方必须降级，不得编造。"""


def _describe_code(code: Any) -> str:
    try:
        return _WMO_CONDITIONS.get(int(code), "多变天气")
    except (TypeError, ValueError):
        return "多变天气"


def _default_fetcher(url: str, timeout_seconds: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "AIHomeButler/14.7"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as resp:
        return resp.read()


class WeatherProvider:
    def __init__(
        self,
        lat: str = "",
        lon: str = "",
        city: str = "",
        timeout_seconds: float = 5.0,
        cache_seconds: int = 1800,
        fetcher: Optional[Callable[[str, float], bytes]] = None,
    ) -> None:
        self._lat = str(lat or "").strip()
        self._lon = str(lon or "").strip()
        self._city = str(city or "").strip()
        self._timeout = float(timeout_seconds)
        self._cache_seconds = max(0, int(cache_seconds))
        self._fetcher = fetcher or _default_fetcher
        self._cache: Optional[Dict[str, Any]] = None
        self._cached_at: float = 0.0

    @property
    def enabled(self) -> bool:
        """坐标齐备才算启用；只填一半视作未配置（宁可降级不可猜）。"""
        return bool(self._lat and self._lon)

    @property
    def city(self) -> str:
        return self._city

    def status(self) -> Dict[str, Any]:
        """装配/自检用：只反映配置态，不做网络请求。"""
        return {
            "enabled": self.enabled,
            "city": self._city or ("已配置坐标" if self.enabled else "未配置"),
            "lat_lon_set": (bool(self._lat), bool(self._lon)),
        }

    def _build_url(self) -> str:
        return (
            f"{OPEN_METEO_URL}?latitude={self._lat}&longitude={self._lon}"
            "&current=temperature_2m,weather_code"
            "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            "&forecast_days=1&timezone=auto"
        )

    def _parse(self, raw: bytes) -> Dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8"))
            current = payload["current"]
            daily = payload["daily"]
            summary = {
                "temperature_c": float(current["temperature_2m"]),
                "condition": _describe_code(current.get("weather_code")),
                "temp_max_c": float(daily["temperature_2m_max"][0]),
                "temp_min_c": float(daily["temperature_2m_min"][0]),
                "rain_probability_percent": int(daily["precipitation_probability_max"][0]),
            }
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise WeatherUnavailable(f"天气数据解析失败：{exc}") from None
        if self._city:
            summary["city"] = self._city
        summary["observed_at"] = time.strftime("%Y-%m-%d %H:%M")
        return summary

    def current_summary(self) -> Dict[str, Any]:
        """当前天气摘要；失败抛 WeatherUnavailable。缓存期内直接返回。"""
        if not self.enabled:
            raise WeatherUnavailable("未配置天气坐标（.env 设 WEATHER_LAT/WEATHER_LON 开启）")
        now = time.monotonic()
        if self._cache is not None and (now - self._cached_at) < self._cache_seconds:
            return dict(self._cache)
        try:
            raw = self._fetcher(self._build_url(), self._timeout)
            summary = self._parse(raw)
        except WeatherUnavailable:
            raise
        except Exception as exc:
            raise WeatherUnavailable(f"天气服务请求失败：{type(exc).__name__}") from None
        self._cache = dict(summary)
        self._cached_at = now
        return summary
