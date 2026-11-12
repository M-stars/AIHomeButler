"""WeatherProvider 测试：解析、缓存、失败降级、未配置态。全部离线（注入 fetcher）。"""
import json

import pytest

from butler.household.weather import WeatherProvider, WeatherUnavailable


def ok_fetcher(payload=None):
    body = payload or {
        "current": {"temperature_2m": 23.1, "weather_code": 2},
        "daily": {"temperature_2m_max": [28.0], "temperature_2m_min": [18.0],
                  "precipitation_probability_max": [30]},
    }
    return lambda url, timeout: json.dumps(body).encode("utf-8")


def test_disabled_without_coordinates():
    provider = WeatherProvider()
    assert provider.enabled is False
    with pytest.raises(WeatherUnavailable):
        provider.current_summary()
    half = WeatherProvider(lat="30.27")
    assert half.enabled is False  # 只填一半视作未配置


def test_parse_summary():
    provider = WeatherProvider(lat="30.27", lon="120.15", city="杭州",
                               fetcher=ok_fetcher())
    summary = provider.current_summary()
    assert summary["temperature_c"] == 23.1
    assert summary["condition"] == "局部多云"
    assert summary["temp_max_c"] == 28.0
    assert summary["temp_min_c"] == 18.0
    assert summary["rain_probability_percent"] == 30
    assert summary["city"] == "杭州"


def test_unknown_weather_code_falls_back():
    provider = WeatherProvider(lat="1", lon="2", fetcher=ok_fetcher({
        "current": {"temperature_2m": 0.0, "weather_code": 42},
        "daily": {"temperature_2m_max": [1.0], "temperature_2m_min": [0.0],
                  "precipitation_probability_max": [0]},
    }))
    assert provider.current_summary()["condition"] == "多变天气"


def test_cache_within_window():
    calls = {"n": 0}

    def counting_fetcher(url, timeout):
        calls["n"] += 1
        return ok_fetcher()(url, timeout)

    provider = WeatherProvider(lat="1", lon="2", cache_seconds=600,
                               fetcher=counting_fetcher)
    first = provider.current_summary()
    second = provider.current_summary()
    assert calls["n"] == 1
    assert first == second
    # 缓存返回副本：修改返回值不影响缓存
    second["temperature_c"] = -999
    assert provider.current_summary()["temperature_c"] == 23.1


def test_failure_raises_unavailable():
    def broken_fetcher(url, timeout):
        raise OSError("network down")

    provider = WeatherProvider(lat="1", lon="2", fetcher=broken_fetcher)
    with pytest.raises(WeatherUnavailable) as excinfo:
        provider.current_summary()
    assert "OSError" in str(excinfo.value)

    # 失败不缓存：恢复后能成功
    provider2 = WeatherProvider(lat="1", lon="2", fetcher=ok_fetcher())
    assert provider2.current_summary()["temperature_c"] == 23.1


def test_malformed_payload_raises():
    provider = WeatherProvider(lat="1", lon="2",
                               fetcher=lambda url, t: b"not-json")
    with pytest.raises(WeatherUnavailable):
        provider.current_summary()

    provider2 = WeatherProvider(lat="1", lon="2", fetcher=ok_fetcher({"current": {}}))
    with pytest.raises(WeatherUnavailable):
        provider2.current_summary()


def test_status_reflects_configuration():
    assert WeatherProvider().status() == {
        "enabled": False, "city": "未配置", "lat_lon_set": (False, False),
    }
    assert WeatherProvider(lat="1", lon="2", city="杭州").status()["enabled"] is True
