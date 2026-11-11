"""真实天气工具：Open-Meteo（免密钥、免费、无需注册）。

天气直接影响行程可行性（雨天要室内备选、日出日落要看云量、高温要避午后），
是「攻略检索」替代不了的实时数据，所以单独做成工具而不是让模型猜。
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

import httpx
from langchain.tools import tool

from .security import wrap_untrusted

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 12.0
GEOCODE_LIMIT = 10

# Open-Meteo 的预报上限（含当天）。超出就只能给「未来 N 天」，并且必须说清楚——实测
# 模型会为了迁就天气窗口把用户的出行日期改写掉，这是最隐蔽的一类错误答案。
FORECAST_HORIZON_DAYS = 16

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

# GeoNames 的中文子库只覆盖国内，国际都市缺得厉害：实测用「东京」检索只会命中江苏/浙江
# 的两个村庄，用「大阪」检索直接 0 条。常用境外目的地给一份拉丁名兜底——用拉丁名检索、
# language=zh 返回的 country 仍是中文，可以和 country 参数对齐。
LATIN_ALIASES = {
    "东京": "Tokyo", "大阪": "Osaka", "京都": "Kyoto", "名古屋": "Nagoya", "札幌": "Sapporo",
    "福冈": "Fukuoka", "奈良": "Nara", "横滨": "Yokohama", "冲绳": "Okinawa", "那霸": "Naha",
    "首尔": "Seoul", "釜山": "Busan", "济州": "Jeju City",
    "曼谷": "Bangkok", "清迈": "Chiang Mai", "普吉": "Phuket", "暹粒": "Siem Reap",
    "新加坡": "Singapore", "吉隆坡": "Kuala Lumpur", "亚庇": "Kota Kinabalu", "巴厘岛": "Bali",
    "雅加达": "Jakarta", "马尼拉": "Manila", "宿务": "Cebu", "河内": "Hanoi",
    "胡志明": "Ho Chi Minh City", "岘港": "Da Nang",
    "巴黎": "Paris", "伦敦": "London", "罗马": "Rome", "米兰": "Milan", "威尼斯": "Venice",
    "佛罗伦萨": "Florence", "巴塞罗那": "Barcelona", "马德里": "Madrid", "塞维利亚": "Seville",
    "柏林": "Berlin", "慕尼黑": "Munich", "法兰克福": "Frankfurt", "阿姆斯特丹": "Amsterdam",
    "布鲁塞尔": "Brussels", "苏黎世": "Zurich", "日内瓦": "Geneva", "维也纳": "Vienna",
    "萨尔茨堡": "Salzburg", "布拉格": "Prague", "布达佩斯": "Budapest", "华沙": "Warsaw",
    "哥本哈根": "Copenhagen", "斯德哥尔摩": "Stockholm", "奥斯陆": "Oslo", "赫尔辛基": "Helsinki",
    "雷克雅未克": "Reykjavik", "都柏林": "Dublin", "爱丁堡": "Edinburgh", "雅典": "Athens",
    "里斯本": "Lisbon", "伊斯坦布尔": "Istanbul", "莫斯科": "Moscow", "圣彼得堡": "Saint Petersburg",
    "开罗": "Cairo", "马拉喀什": "Marrakesh", "开普敦": "Cape Town", "迪拜": "Dubai",
    "阿布扎比": "Abu Dhabi", "多哈": "Doha", "纽约": "New York", "波士顿": "Boston",
    "华盛顿": "Washington", "芝加哥": "Chicago", "洛杉矶": "Los Angeles", "旧金山": "San Francisco",
    "拉斯维加斯": "Las Vegas", "西雅图": "Seattle", "温哥华": "Vancouver", "多伦多": "Toronto",
    "墨西哥城": "Mexico City", "坎昆": "Cancun", "里约": "Rio de Janeiro", "悉尼": "Sydney",
    "墨尔本": "Melbourne", "奥克兰": "Auckland", "皇后镇": "Queenstown", "檀香山": "Honolulu",
}

# 行政级别权重：级别越高越像「用户想问的那个城市」。PPL=普通居民点，PPLC=首都，PPLA*=首府。
FEATURE_WEIGHT = {
    "PPLC": 40.0, "LCC": 35.0, "PPLA": 30.0, "PPLA2": 24.0,
    "PPLA3": 20.0, "PPLA4": 16.0, "PPL": 10.0, "PPLX": 8.0,
}
UNKNOWN_WEIGHT = 5.0
COUNTRY_MATCH_BONUS = 50.0
COUNTRY_MISMATCH_PENALTY = 60.0

# (city, country) -> 定位结果；同一个会话里反复查同城不再打接口
_city_cache: dict[tuple[str, str], dict] = {}


def describe_code(code: int) -> str:
    return WEATHER_CODES.get(int(code), f"代码{code}")


def is_rainy(code: int) -> bool:
    return int(code) in RAINY


def place_label(result: dict) -> str:
    """给模型看的地名「城市，省州，国家」：只回城市名的话，它分不清江苏的东京和日本东京。"""
    return "，".join(str(part) for part in (result.get("name"), result.get("admin1"), result.get("country")) if part)


def score_result(result: dict, country: str = "") -> float:
    """候选地打分：行政级别 + 人口量级，用户点名国家时国家级匹配压倒一切。纯函数。"""
    score = FEATURE_WEIGHT.get(result.get("feature_code") or "", UNKNOWN_WEIGHT)
    population = result.get("population")
    if isinstance(population, (int, float)) and population > 0:
        score += math.log10(population) * 2.0
    if country:
        hint = country.strip().lower()
        located = {str(result.get("country") or "").strip().lower(), str(result.get("country_code") or "").strip().lower()}
        score += COUNTRY_MATCH_BONUS if hint in located else -COUNTRY_MISMATCH_PENALTY
    return round(score, 2)


def pick_result(results: list[dict], country: str = "") -> dict | None:
    """从候选里挑最像「用户要的城市」的那个；同分优先人口多、geonames id 小的。纯函数。"""
    scored = [
        (score_result(item, country), item.get("population") or 0, -int(item.get("id") or 0), item)
        for item in results
        if isinstance(item, dict)
    ]
    return max(scored, key=lambda row: row[:3])[3] if scored else None


def query_variants(name: str) -> list[str]:
    """检索词候选：原名 -> 拉丁别名 -> 去掉「市/县/区」后缀（为了「大理市」这类写法）。"""
    stripped = re.sub(r"(市|县|区|自治州|特别行政区)$", "", name.strip()) or name.strip()
    alias = LATIN_ALIASES.get(name.strip()) or LATIN_ALIASES.get(stripped)
    variants: list[str] = []
    for term in (name.strip(), alias, stripped):
        if term and term not in variants:
            variants.append(term)
    return variants


def _get_json(url: str, params: dict) -> dict:
    """唯一的出网点：单测替换它就能全程离线。"""
    resp = httpx.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json() or {}


def geocode(city: str, country: str = "") -> dict:
    """城市名 -> {"latitude","longitude","label","minor"}。命中缓存则不再请求。

    minor=True 表示定位到的像是小居民点，输出里必须提醒模型复核，别让它拿着村庄天气排行程。
    """
    key = (city.strip(), (country or "").strip())
    cached = _city_cache.get(key)
    if cached:
        return cached

    candidates: list[dict] = []
    for term in query_variants(key[0]):
        payload = _get_json(GEOCODE_URL, {"name": term, "count": GEOCODE_LIMIT, "language": "zh", "format": "json"})
        candidates.extend(payload.get("results") or [])

    best = pick_result(candidates, country=key[1])
    if best is None:
        raise LookupError(f"找不到城市：{city}。可改用英文城市名，或带上 country 参数（例如 country=\"日本\"）。")

    located = {
        "latitude": float(best["latitude"]),
        "longitude": float(best["longitude"]),
        "label": place_label(best),
        "minor": not best.get("population") and (best.get("feature_code") or "PPL") == "PPL",
    }
    _city_cache[key] = located
    return located


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def forecast_window(start_date: str | None, days: int, today: date | None = None) -> tuple[dict, str]:
    """把「哪天出发」翻译成 Open-Meteo 参数，返回 (请求参数, 给模型看的提示)。

    出行日期在预报窗口内就精确取那几天；在窗口外就退回未来 N 天并明确说明——
    工具输出必须自带边界声明，否则模型会拿近期天气当成用户真正那天的天气。
    """
    base = today or date.today()
    horizon = base + timedelta(days=FORECAST_HORIZON_DAYS - 1)
    span = max(1, min(days, 7))
    fallback = {"forecast_days": span}
    text = (start_date or "").strip()
    if not text:
        return fallback, ""
    try:
        first = date.fromisoformat(text)
    except ValueError:
        return fallback, f"出发日期「{text}」没法识别，已改为未来 {span} 天天气；请按季节常识安排并提醒用户临行前再查。"
    if first < base:
        return fallback, f"{first:%Y-%m-%d} 已经过去了，下面给的是未来 {span} 天天气，不要写进这天的行程。"
    if first > horizon:
        return fallback, (
            f"{first:%Y-%m-%d} 超出预报范围（只到 {horizon:%Y-%m-%d}），下面是未来 {span} 天的天气，只能当近期参考，"
            "不能作为出行当天的事实：请按季节常识编排，保留用户给的日期，并把「临行前复查天气」写进 risks。"
        )
    last = min(first + timedelta(days=span - 1), horizon)
    return {"start_date": first.isoformat(), "end_date": last.isoformat()}, ""


def fetch_forecast(city: str, days: int = 4, country: str = "", start_date: str = "") -> list[dict]:
    place = geocode(city, country=country)
    window, note = forecast_window(start_date, days)
    payload = _get_json(
        FORECAST_URL,
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,sunrise,sunset",
            **window,
            # auto = 按目的地当地时区返回。写死 Asia/Shanghai 会让巴黎的日落显示成
            # 次日 00:16（北京时间），模型照此安排拍摄点位就全错了；日期也会串到天。
            "timezone": "auto",
        },
    )
    daily = payload["daily"]
    dates = daily.get("time") or []
    rows = []
    for i, day in enumerate(dates):
        code = _number((daily.get("weather_code") or [None] * len(dates))[i])
        rows.append(
            {
                "place": place["label"],
                "minor": place["minor"],
                "note": note,
                "date": day,
                "code": code,
                "text": describe_code(code) if code is not None else "天气现象未知",
                "t_max": _number((daily.get("temperature_2m_max") or [None] * len(dates))[i]),
                "t_min": _number((daily.get("temperature_2m_min") or [None] * len(dates))[i]),
                "rain_mm": _number((daily.get("precipitation_sum") or [None] * len(dates))[i]),
                "sunrise": ((daily.get("sunrise") or [""] * len(dates))[i] or "")[-5:],
                "sunset": ((daily.get("sunset") or [""] * len(dates))[i] or "")[-5:],
                "rainy": is_rainy(code) if code is not None else False,
            }
        )
    return rows


def format_forecast(rows: list[dict]) -> str:
    if not rows:
        return "没有取到天气数据。"
    head = f"{rows[0]['place']}天气预报："
    if rows[0].get("minor"):
        head += "\n⚠ 定位到的是小居民点，可能不是用户要的目的地：请带 country 参数或用英文城市名重试，仍不行就改用 web_search。"
    if rows[0].get("note"):
        head += f"\n⚠ {rows[0]['note']}"
    lines = [
        f"- {r['date']} {r['text']} "
        + ("温度未知" if r["t_min"] is None or r["t_max"] is None else f"{r['t_min']:.0f}~{r['t_max']:.0f}℃")
        + " "
        + ("降水未知" if r["rain_mm"] is None else f"降水 {r['rain_mm']:.1f}mm")
        + f" 日出 {r['sunrise'] or '?'} 日落 {r['sunset'] or '?'}"
        + ("；⚠ 有雨，请安排室内备选" if r["rainy"] else "")
        for r in rows
    ]
    rainy_days = sum(1 for r in rows if r["rainy"])
    hint = f"未来 {len(rows)} 天中 {rainy_days} 天有降水"
    if any(r["rainy"] for r in rows):
        hint += "，建议为雨天准备博物馆/室内景点备选，并把日出日落拍摄安排在无雨日"
    return "\n".join([head] + lines + [hint])


def build_weather_tool(days: int = 4):
    @tool
    def weather_forecast(city: str, country: str = "", start_date: str = "") -> str:
        """查目的地未来几天的真实天气预报：温度、降水、天气现象、日出日落时间（按当地时区）。

        安排室内备选、穿衣提示和日出日落拍摄点位时必须用它，不要凭季节猜测。
        境外目的地把国家一起传进来，避免同名小地名被误匹配。

        Args:
            city: 城市名，中文即可，例如 "杭州"、"大理"、"东京"
            country: 可选，国家名，例如 "日本"，用于消除同名歧义
            start_date: 可选，出行第一天，格式 YYYY-MM-DD。用户说了出发日期就必须传进来；
                超出预报范围时工具会明确告知，此时保留用户日期、按季节常识安排
        """
        try:
            return wrap_untrusted(
            format_forecast(fetch_forecast(city, days=days, country=country, start_date=start_date)),
            "open-meteo",
        )
        except Exception as exc:
            return f"天气查询失败：{type(exc).__name__}: {str(exc)[:120]}。请改用 web_search 查询当地天气。"

    return weather_forecast
