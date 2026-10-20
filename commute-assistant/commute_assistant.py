"""Create a Shanghai driving-commute brief with weather and route conditions."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from openai import APIConnectionError, APITimeoutError, OpenAI


ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "commute_config.json"
REPORT_DIR = ROOT / "commute_reports"
WEATHER_API = "https://api.open-meteo.com/v1/forecast"
AMAP_GEOCODE_API = "https://restapi.amap.com/v3/geocode/geo"
AMAP_DRIVING_API = "https://restapi.amap.com/v5/direction/driving"
TIMEOUT_SECONDS = 30
MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")

WEATHER_CODES = {
    0: "晴",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇雾",
    51: "毛毛雨",
    53: "中等毛毛雨",
    55: "较强毛毛雨",
    56: "冻毛毛雨",
    57: "较强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "较强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "中等阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "伴小冰雹的雷暴",
    99: "伴较大冰雹的雷暴",
}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            "找不到 commute_config.json。请先复制 commute_config.example.json 并保存为 commute_config.json。"
        )
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("commute_config.json 顶层必须是 JSON 对象。")
    for key in ("latitude", "longitude", "morning_start", "morning_end", "evening_start", "evening_end"):
        if key not in config:
            raise ValueError(f"commute_config.json 缺少设置：{key}")
    return config


def fetch_forecast(config: dict) -> dict:
    params = {
        "latitude": config["latitude"],
        "longitude": config["longitude"],
        "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code,visibility,wind_speed_10m,wind_gusts_10m",
        "forecast_days": 2,
        "timezone": "Asia/Shanghai",
        "temperature_unit": "celsius",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
    }
    url = f"{WEATHER_API}?{urlencode(params)}"
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"天气服务返回 HTTP {exc.code}。") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接天气服务：{exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("天气服务返回的数据不是有效 JSON。") from exc


def get_json(url: str, params: dict, service_name: str) -> dict:
    request_url = f"{url}?{urlencode(params)}"
    request = Request(request_url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"{service_name}返回 HTTP {exc.code}。") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接{service_name}：{exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{service_name}返回的数据不是有效 JSON。") from exc
    if data.get("status") != "1":
        detail = data.get("info", "接口返回失败")
        raise RuntimeError(f"{service_name}调用失败：{detail}。请检查高德 Key 和服务权限。")
    return data


def geocode_landmark(address: str, city: str, amap_key: str) -> str:
    data = get_json(
        AMAP_GEOCODE_API,
        {"key": amap_key, "address": address, "city": city, "output": "JSON"},
        "高德地理编码服务",
    )
    geocodes = data.get("geocodes") or []
    if not geocodes or not geocodes[0].get("location"):
        raise RuntimeError(f"高德没有找到地点“{address}”的坐标。请在 commute_config.json 中改用更明确的地标。")
    return geocodes[0]["location"]


def fetch_driving_routes(config: dict) -> dict:
    amap_key = os.getenv("AMAP_WEB_KEY")
    if not amap_key:
        raise RuntimeError("没有检测到 AMAP_WEB_KEY。请先在当前 PowerShell 窗口设置高德 Web 服务 Key。")
    city = config.get("route_city", "上海市")
    origin_address = config.get("origin", "上海市普陀区真北路与古浪路交叉口")
    destination_address = config.get("destination", "上海市闵行区上海交通大学闵行校区")
    origin = geocode_landmark(origin_address, city, amap_key)
    destination = geocode_landmark(destination_address, city, amap_key)
    return get_json(
        AMAP_DRIVING_API,
        {
            "key": amap_key,
            "origin": origin,
            "destination": destination,
            "strategy": config.get("route_strategy", 0),
            "show_fields": "cost,tmcs",
        },
        "高德驾车路线服务",
    )


def summarize_routes(route_data: dict, max_routes: int = 3) -> list[dict]:
    routes = (route_data.get("route") or {}).get("paths") or []
    summaries = []
    for index, path in enumerate(routes[:max_routes], start=1):
        congestion_counts = {"畅通": 0, "缓行": 0, "拥堵": 0, "严重拥堵": 0, "未知": 0}
        roads = []
        for step in path.get("steps", []):
            road_name = step.get("road_name") or "未命名道路"
            for segment in step.get("tmcs", []):
                status = segment.get("tmc_status", "未知")
                congestion_counts[status if status in congestion_counts else "未知"] += 1
                if status in ("拥堵", "严重拥堵", "缓行"):
                    roads.append(f"{road_name}（{status}）")
        summaries.append({
            "route_number": index,
            "distance_km": round(int(path.get("distance", 0)) / 1000, 1),
            "duration_minutes": round(int((path.get("cost") or {}).get("duration", 0)) / 60),
            "tolls_yuan": (path.get("cost") or {}).get("tolls", "未知"),
            "restriction": path.get("restriction", "未知"),
            "traffic_segment_counts": congestion_counts,
            "slow_or_congested_roads": list(dict.fromkeys(roads))[:12],
        })
    if not summaries:
        raise RuntimeError("高德没有返回可用驾车路线。请检查地点名称或稍后重试。")
    return summaries


def period_rows(forecast: dict, date_text: str, start_hour: int, end_hour: int) -> list[dict]:
    hourly = forecast.get("hourly", {})
    times = hourly.get("time", [])
    rows = []
    for index, stamp in enumerate(times):
        if not stamp.startswith(date_text) or len(stamp) < 13:
            continue
        hour = int(stamp[11:13])
        if not start_hour <= hour < end_hour:
            continue
        row = {"time": stamp[11:16]}
        for field in (
            "temperature_2m", "precipitation_probability", "precipitation",
            "weather_code", "visibility", "wind_speed_10m", "wind_gusts_10m",
        ):
            values = hourly.get(field, [])
            row[field] = values[index] if index < len(values) else None
        code = row.get("weather_code")
        row["weather"] = WEATHER_CODES.get(code, "天气状况未知") if code is not None else "天气状况未知"
        rows.append(row)
    return rows


def make_brief(config: dict, forecast: dict, route_data: dict, date_text: str, query_time: str) -> str:
    morning = period_rows(
        forecast, date_text, int(config["morning_start"]), int(config["morning_end"])
    )
    evening = period_rows(
        forecast, date_text, int(config["evening_start"]), int(config["evening_end"])
    )
    routes = summarize_routes(route_data)
    if not morning and not evening:
        raise RuntimeError("天气数据里没有找到今天设置的通勤时段；可以稍后重试或检查时段设置。")

    input_data = {
        "city": config.get("city", "上海"),
        "date": date_text,
        "morning_peak": {"hours": f"{config['morning_start']}:00–{config['morning_end']}:00", "hourly_forecast": morning},
        "evening_peak": {"hours": f"{config['evening_start']}:00–{config['evening_end']}:00", "hourly_forecast": evening},
        "driving_routes_and_traffic_at_query_time": {
            "origin_landmark": config.get("origin", "上海市普陀区真北路与古浪路交叉口"),
            "destination_landmark": config.get("destination", "上海市闵行区上海交通大学闵行校区"),
            "query_time": query_time,
            "routes": routes,
        },
    }
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        try:
            response = OpenAI(timeout=30.0, max_retries=0).responses.create(
                model=MODEL,
                instructions=(
                    "你是中文自驾通勤简报助手。根据给定天气和高德路线数据，写天气提示以及驾车路线/路况摘要。"
                    "可以提示降雨、能见度、阵风、温度等驾驶相关天气因素，并建议预留注意力或查看车窗除雾；"
                    "路线耗时与路段拥堵状态只代表查询时刻，不是未来早晚高峰预测，也不包含事故或封路确认。"
                    "只能使用输入提供的路线和路况数据，不要编造；若返回多条路线，比较耗时、距离和拥堵路段。"
                    "简短清楚，并明确建议出发前用导航复核。"
                ),
                input=json.dumps(input_data, ensure_ascii=False),
            )
            return response.output_text.strip()
        except (APITimeoutError, APIConnectionError) as exc:
            print(f"OpenAI 连接暂不可用，改用本地简报：{type(exc).__name__}", file=sys.stderr)
    else:
        print("未配置 OpenAI Key，改用本地简报。", file=sys.stderr)

    return make_local_brief(input_data)


def make_local_brief(input_data: dict) -> str:
    """Create a factual fallback brief when the OpenAI endpoint is unreachable."""
    lines = ["AI 摘要服务暂时不可用，以下内容根据天气与高德返回数据整理。", ""]
    for label, period in (("早高峰天气", input_data["morning_peak"]), ("晚高峰天气", input_data["evening_peak"])):
        rows = period["hourly_forecast"]
        if not rows:
            lines.append(f"- {label}：当前没有可用的逐小时预报。")
            continue
        weather = list(dict.fromkeys(row.get("weather", "天气状况未知") for row in rows))
        temperatures = [row.get("temperature_2m") for row in rows if row.get("temperature_2m") is not None]
        rain_chances = [row.get("precipitation_probability") for row in rows if row.get("precipitation_probability") is not None]
        parts = [f"{period['hours']}，天气：{'、'.join(weather)}"]
        if temperatures:
            parts.append(f"气温约 {min(temperatures):g}–{max(temperatures):g}°C")
        if rain_chances:
            parts.append(f"最高降水概率 {max(rain_chances)}%")
        lines.append(f"- {label}：" + "；".join(parts) + "。")

    route_data = input_data["driving_routes_and_traffic_at_query_time"]
    lines.extend(["", f"路线与路况（查询时间：{route_data['query_time']}）："])
    for route in route_data["routes"]:
        counts = route["traffic_segment_counts"]
        congested_count = sum(counts.get(label, 0) for label in ("缓行", "拥堵", "严重拥堵"))
        line = (
            f"- 路线 {route['route_number']}：约 {route['duration_minutes']} 分钟、"
            f"{route['distance_km']} 公里，收费 {route['tolls_yuan']} 元；"
            f"路况分段中缓行/拥堵 {congested_count} 段。"
        )
        if route["slow_or_congested_roads"]:
            line += "需留意：" + "、".join(route["slow_or_congested_roads"][:6]) + "。"
        lines.append(line)
    lines.extend(["", "提示：路况仅代表查询时刻，不是未来高峰预测；出发前请用导航复核。"])
    return "\n".join(lines)


def main() -> int:
    try:
        config = load_config()
        forecast = fetch_forecast(config)
        now = datetime.now()
        date_text = now.strftime("%Y-%m-%d")
        route_data = fetch_driving_routes(config)
        brief = make_brief(config, forecast, route_data, date_text, now.strftime("%H:%M"))
        REPORT_DIR.mkdir(exist_ok=True)
        report = REPORT_DIR / f"commute_{now.strftime('%Y-%m-%d_%H%M')}.md"
        lines = [
            f"# {config.get('city', '上海')}自驾通勤简报｜{date_text}",
            "",
            f"生成时间：{now.strftime('%H:%M')}（北京时间）",
            f"天气查询区域：{config.get('weather_area', '上海市区级近似位置')}（非精确路线天气）",
            f"路线：{config.get('origin', '上海市普陀区真北路与古浪路交叉口')} → {config.get('destination', '上海市闵行区上海交通大学闵行校区')}（公开地标近似点）",
            "",
            brief,
            "",
            "---",
            f"数据来源：Open-Meteo Forecast API；高德地理编码与驾车路线规划 API（查询时间：{now.strftime('%Y-%m-%d %H:%M')}）",
            "说明：路况仅代表查询时刻，不能代表未来早晚高峰；不提供事故/封路核验。路线起终点是地标近似点。出发前请用导航复核。",
        ]
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"已生成通勤天气简报：{report}")
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
