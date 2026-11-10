"""Create a Shanghai driving-commute brief with weather and route conditions."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from openai import OpenAI
from route_history import record_snapshot


ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "commute_config.json"
REPORT_DIR = ROOT / "commute_reports"
WEATHER_API = "https://api.open-meteo.com/v1/forecast"
AMAP_GEOCODE_API = "https://restapi.amap.com/v3/geocode/geo"
AMAP_CONVERT_API = "https://restapi.amap.com/v3/assistant/coordinate/convert"
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
    route_data = get_json(
        AMAP_DRIVING_API,
        {
            "key": amap_key,
            "origin": origin,
            "destination": destination,
            "strategy": config.get("route_strategy", 0),
            "show_fields": "cost,tmcs,polyline",
        },
        "高德驾车路线服务",
    )
    try:
        record_snapshot(
            origin,
            destination,
            str(config.get("route_strategy", 0)),
            (route_data.get("route") or {}).get("paths") or [],
        )
    except sqlite3.Error as exc:
        print(f"路线已查询成功，但本机历史记录未能保存：{type(exc).__name__}", file=sys.stderr)
        route_data["_history_saved"] = False
    else:
        route_data["_history_saved"] = True
    return route_data


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


def make_brief(
    config: dict,
    forecast: dict | None,
    route_data: dict | None,
    date_text: str,
    query_time: str,
    service_errors: dict[str, str] | None = None,
) -> str:
    service_errors = service_errors or {}
    morning = []
    evening = []
    if forecast is not None:
        try:
            morning = period_rows(
                forecast, date_text, int(config["morning_start"]), int(config["morning_end"])
            )
            evening = period_rows(
                forecast, date_text, int(config["evening_start"]), int(config["evening_end"])
            )
            if not morning and not evening:
                service_errors.setdefault("weather", "天气服务没有返回所设通勤时段的数据。")
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            print(f"天气数据格式不可用：{type(exc).__name__}", file=sys.stderr)
            service_errors.setdefault("weather", "天气服务返回的数据暂时无法整理。")
            morning = []
            evening = []
    routes = []
    if route_data is not None:
        try:
            routes = summarize_routes(route_data)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            print(f"路线数据格式不可用：{type(exc).__name__}", file=sys.stderr)
            service_errors.setdefault("routes", "高德没有返回可用路线数据。")

    input_data = {
        "city": config.get("city", "上海"),
        "date": date_text,
        "service_status": {
            "weather_available": bool(morning or evening),
            "weather_issue": service_errors.get("weather", ""),
            "routes_available": bool(routes),
            "routes_issue": service_errors.get("routes", ""),
            "route_history_saved": route_data.get("_history_saved") if route_data else None,
        },
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
    has_source_data = bool(morning or evening or routes)
    if api_key and has_source_data:
        try:
            response = OpenAI(timeout=30.0, max_retries=0).responses.create(
                model=MODEL,
                instructions=(
                    "你是中文自驾通勤简报助手。根据给定天气和高德路线数据，写天气提示以及驾车路线/路况摘要。"
                    "可以提示降雨、能见度、阵风、温度等驾驶相关天气因素，并建议预留注意力或查看车窗除雾；"
                    "路线耗时与路段拥堵状态只代表查询时刻，不是未来早晚高峰预测，也不包含事故或封路确认。"
                    "只能使用输入提供的路线和路况数据，不要编造；若返回多条路线，比较耗时、距离和拥堵路段。"
                    "输入中的 service_status 会说明天气或路线数据是否缺失；缺失时要明确告知用户，不要猜测或编造。"
                    "若路线查询成功但本机历史记录保存失败，也要明确说明路线结果仍可用，但本次不会用于后续历史估算。"
                    "简短清楚，并明确建议出发前用导航复核。"
                ),
                input=json.dumps(input_data, ensure_ascii=False),
            )
            summary = response.output_text.strip()
            if summary:
                return summary
            raise RuntimeError("AI 服务没有返回摘要文本。")
        except Exception as exc:
            print(f"OpenAI 摘要未能生成，改用本地简报：{type(exc).__name__}", file=sys.stderr)
    else:
        reason = "未配置 OpenAI Key" if not api_key else "天气和路线数据均不可用"
        print(f"{reason}，改用本地简报。", file=sys.stderr)

    return make_local_brief(input_data)


def make_local_brief(input_data: dict) -> str:
    """Create a factual fallback brief when the OpenAI endpoint is unreachable."""
    lines = ["以下内容由本地规则根据当前可用数据整理。"]
    status = input_data.get("service_status", {})
    if status.get("weather_available"):
        lines.append("- 天气数据：可用。")
    else:
        lines.append(f"- 天气数据：不可用。{status.get('weather_issue') or '本次没有可用的天气预报。'}")
    if status.get("routes_available"):
        lines.append("- 路线与路况数据：可用（仅代表查询时刻）。")
        if status.get("route_history_saved") is False:
            lines.append("- 本机历史样本：保存失败；本次路线结果可用，但不会进入后续历史估算。")
    else:
        lines.append(f"- 路线与路况数据：不可用。{status.get('routes_issue') or '本次没有可用的路线信息。'}")
    if not os.getenv("OPENAI_API_KEY"):
        lines.append("- AI 摘要：未配置 API Key，使用本地规则整理。")
    lines.append("")
    for label, period in (("早高峰天气", input_data["morning_peak"]), ("晚高峰天气", input_data["evening_peak"])):
        rows = period["hourly_forecast"]
        if not rows:
            lines.append(f"- {label}：当前没有可用的逐小时预报。")
            continue
        weather = list(dict.fromkeys(row.get("weather", "天气状况未知") for row in rows))
        def numeric_values(field: str) -> list[float]:
            values = []
            for row in rows:
                try:
                    value = float(row[field])
                except (KeyError, TypeError, ValueError):
                    continue
                values.append(value)
            return values

        temperatures = numeric_values("temperature_2m")
        rain_chances = numeric_values("precipitation_probability")
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
        now = datetime.now()
        date_text = now.strftime("%Y-%m-%d")
        service_errors: dict[str, str] = {}
        forecast = None
        route_data = None
        try:
            forecast = fetch_forecast(config)
            if not period_rows(forecast, date_text, int(config["morning_start"]), int(config["morning_end"])) \
                    and not period_rows(forecast, date_text, int(config["evening_start"]), int(config["evening_end"])):
                raise RuntimeError("天气服务没有返回所设通勤时段的数据。")
        except Exception as exc:
            print(f"天气服务不可用：{type(exc).__name__}", file=sys.stderr)
            forecast = None
            service_errors["weather"] = "天气服务暂时不可用或没有返回所需时段的数据。"
        try:
            route_data = fetch_driving_routes(config)
            summarize_routes(route_data)
        except Exception as exc:
            print(f"路线服务不可用：{type(exc).__name__}", file=sys.stderr)
            route_data = None
            service_errors["routes"] = "高德路线服务暂时不可用或没有返回可用路线。"
        brief = make_brief(config, forecast, route_data, date_text, now.strftime("%H:%M"), service_errors)
        REPORT_DIR.mkdir(exist_ok=True)
        report = REPORT_DIR / f"commute_{now.strftime('%Y-%m-%d_%H%M')}.md"
        sources = []
        if forecast is not None:
            sources.append("Open-Meteo Forecast API 天气数据")
        if route_data is not None:
            sources.append("高德地理编码与驾车路线规划 API")
        source_text = "；".join(sources) if sources else "本次天气和路线服务均未提供可用数据"
        route_history_note = (
            "；本机历史样本保存失败（本次路线结果仍可用）"
            if route_data is not None and route_data.get("_history_saved") is False else ""
        )
        lines = [
            f"# {config.get('city', '上海')}自驾通勤简报｜{date_text}",
            "",
            f"生成时间：{now.strftime('%H:%M')}（北京时间）",
            f"数据状态：天气{'已获取' if forecast is not None else '暂不可用'}；驾车路线和查询时路况{'已获取' if route_data is not None else '暂不可用'}{route_history_note}。",
            f"天气查询区域：{config.get('weather_area', '上海市区级近似位置')}（非精确路线天气）",
            f"路线：{config.get('origin', '上海市普陀区真北路与古浪路交叉口')} → {config.get('destination', '上海市闵行区上海交通大学闵行校区')}（公开地标近似点）",
            "",
            brief,
            "",
            "---",
            f"数据来源：{source_text}（查询时间：{now.strftime('%Y-%m-%d %H:%M')}）",
            "说明：路况仅代表查询时刻，不能代表未来早晚高峰；不提供事故/封路核验。路线起终点是地标近似点。出发前请用导航复核。",
        ]
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"已生成通勤天气简报：{report}")
        if forecast is None and route_data is None:
            print("天气和路线服务本次均不可用。已保存错误说明报告，请查看稍后重试。", file=sys.stderr)
            return 1
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
