"""真实天气工具：Open-Meteo（免密钥、免费、无需注册）。

天气直接影响行程可行性（雨天要室内备选、日出日落要看云量、高温要避午后），
是「攻略检索」替代不了的实时数据，所以单独做成工具而不是让模型猜。
"""
from __future__ import annotations

import httpx
from langchain.tools import tool

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 12.0

# WMO 天气代码 -> 中文。只列行程里真正影响决策的部分。
WEATHER_CODES = {
    0: "晴", 1: "大体晴", 2: "局部多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨(弱)", 53: "毛毛雨", 55: "毛毛雨(强)",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}
RAINY = {51, 53, 55, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99}
SNOWY = {71, 73, 75, 77, 85, 86}

_city_cache: dict[str, tuple[float, float, str]] = {}


def describe_code(code: int) -> str:
    return WEATHER_CODES.get(int(code), f"代码{code}")


def is_rainy(code: int) -> bool:
    return int(code) in RAINY


def geocode(city: str) -> tuple[float, float, str]:
    """城市名 -> 坐标。命中缓存则不再请求。"""
    if city in _city_cache:
        return _city_cache[city]
    resp = httpx.get(GEOCODE_URL, params={"name": city, "count": 1, "language": "zh", "format": "json"}, timeout=TIMEOUT)
    resp.raise_for_status()
    results = (resp.json() or {}).get("results") or []
    if not results:
        raise LookupError(f"找不到城市：{city}")
    first = results[0]
    located = (float(first["latitude"]), float(first["longitude"]), first.get("name") or city)
    _city_cache[city] = located
    return located


def fetch_forecast(city: str, days: int = 4) -> list[dict]:
    lat, lon, name = geocode(city)
    resp = httpx.get(
        FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,sunrise,sunset",
            "forecast_days": max(1, min(days, 7)),
            "timezone": "Asia/Shanghai",
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    daily = resp.json()["daily"]
    rows = []
    for i, day in enumerate(daily["time"]):
        rows.append(
            {
                "city": name,
                "date": day,
                "code": daily["weather_code"][i],
                "text": describe_code(daily["weather_code"][i]),
                "t_max": daily["temperature_2m_max"][i],
                "t_min": daily["temperature_2m_min"][i],
                "rain_mm": daily["precipitation_sum"][i],
                "sunrise": (daily.get("sunrise") or [""] * len(daily["time"]))[i][-5:],
                "sunset": (daily.get("sunset") or [""] * len(daily["time"]))[i][-5:],
                "rainy": is_rainy(daily["weather_code"][i]),
            }
        )
    return rows


def format_forecast(rows: list[dict]) -> str:
    if not rows:
        return "没有取到天气数据。"
    lines = [
        f"- {r['date']} {r['text']} {r['t_min']:.0f}~{r['t_max']:.0f}℃ 降水 {r['rain_mm']:.1f}mm "
        f"日出 {r['sunrise']} 日落 {r['sunset']}"
        + ("；⚠ 有雨，请安排室内备选" if r["rainy"] else "")
        for r in rows
    ]
    rainy_days = sum(1 for r in rows if r["rainy"])
    hint = f"未来 {len(rows)} 天中 {rainy_days} 天有降水"
    if any(r["rainy"] for r in rows):
        hint += "，建议为雨天准备博物馆/室内景点备选，并把日出日落拍摄安排在无雨日"
    return "\n".join([f"{rows[0]['city']}天气预报："] + lines + [hint])


def build_weather_tool(days: int = 4):
    @tool
    def weather_forecast(city: str) -> str:
        """查目的地未来几天的真实天气预报：温度、降水、天气现象、日出日落时间。

        安排室内备选、穿衣提示和日出日落拍摄点位时必须用它，不要凭季节猜测。

        Args:
            city: 城市名，中文即可，例如 "杭州" 或 "大理"
        """
        try:
            return format_forecast(fetch_forecast(city, days=days))
        except Exception as exc:
            return f"天气查询失败：{type(exc).__name__}: {str(exc)[:120]}。请改用 web_search 查询当地天气。"

    return weather_forecast