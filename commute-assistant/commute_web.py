"""A small local web UI for querying current AMap driving routes."""

from __future__ import annotations

import html
import json
import os
import re
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from commute_assistant import AMAP_DRIVING_API, AMAP_GEOCODE_API, get_json


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8765
STRATEGIES = {
    "32": "高德默认推荐",
    "33": "躲避拥堵",
    "34": "高速优先",
    "35": "不走高速",
    "36": "少收费",
}


def candidates_for(address: str, amap_key: str) -> list[dict]:
    data = get_json(
        AMAP_GEOCODE_API,
        # Search nationwide so the user can enter places outside Shanghai too.
        {"key": amap_key, "address": address, "output": "JSON"},
        "高德地点查询",
    )
    candidates = []
    for item in data.get("geocodes") or []:
        location = item.get("location")
        if not location:
            continue
        candidates.append({
            "location": location,
            "name": item.get("formatted_address") or address,
            "level": item.get("level") or "匹配地点",
            "district": item.get("district") or "",
        })
    if not candidates:
        raise ValueError(f"没有找到“{address}”。试试补充区、道路或附近地标。")
    return candidates[:6]


def valid_location(value: str) -> bool:
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,8})?,\d{1,2}(?:\.\d{1,8})?", value):
        return False
    longitude, latitude = map(float, value.split(","))
    return 73 <= longitude <= 135 and 3 <= latitude <= 54


def query_routes(origin: str, destination: str, strategy: str, amap_key: str) -> dict:
    if not valid_location(origin) or not valid_location(destination):
        raise ValueError("地点坐标无效。请返回重新选择起点和目的地。")
    if strategy not in STRATEGIES:
        strategy = "32"
    return get_json(
        AMAP_DRIVING_API,
        {
            "key": amap_key,
            "origin": origin,
            "destination": destination,
            "strategy": strategy,
            "alternative_route": 3,
            "show_fields": "cost,tmcs",
        },
        "高德驾车路线服务",
    )


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def page(content: str, title: str = "上海自驾路线助手") -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>
:root{{color-scheme:light;--ink:#17202a;--muted:#607080;--line:#dce3e8;--blue:#1769aa;--bg:#f2f6f8}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 "Segoe UI","Microsoft YaHei",sans-serif}}
main{{max-width:900px;margin:34px auto;padding:0 18px}}h1{{font-size:30px;margin:0 0 4px}}h2{{font-size:21px;margin:0 0 10px}}p{{margin:5px 0}}.muted{{color:var(--muted)}}
.card{{background:white;border:1px solid var(--line);border-radius:14px;padding:20px;margin:16px 0;box-shadow:0 2px 10px #1730440a}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}label{{display:block;font-weight:650;margin:10px 0 5px}}input,select{{width:100%;border:1px solid #b9c6d0;border-radius:9px;padding:11px 12px;font:inherit;background:white}}
button,.button{{display:inline-block;border:0;border-radius:9px;padding:11px 17px;background:var(--blue);color:white;font:inherit;font-weight:650;cursor:pointer;text-decoration:none;margin-top:15px}}
.route-head{{display:flex;justify-content:space-between;gap:15px;align-items:center;flex-wrap:wrap}}.metric{{font-size:22px;font-weight:700}}.badge{{display:inline-block;background:#eaf4fb;color:#165d91;padding:3px 9px;border-radius:99px;margin:3px;font-size:14px}}
.traffic{{color:#a44321}}details{{border-top:1px solid var(--line);margin-top:14px;padding-top:10px}}summary{{cursor:pointer;font-weight:650}}ol{{padding-left:24px}}li{{margin:7px 0}}.error{{border-color:#e2aaa5;background:#fff7f6;color:#8f2f25}}.notice{{background:#eef6fb;border-left:4px solid var(--blue);padding:10px 13px;margin-top:14px;color:#38566b}}
@media(max-width:650px){{.grid{{grid-template-columns:1fr}}main{{margin:18px auto}}h1{{font-size:25px}}}}
</style></head><body><main>{content}</main></body></html>"""


def strategy_options(selected: str) -> str:
    return "".join(
        f'<option value="{key}"{" selected" if key == selected else ""}>{esc(name)}</option>'
        for key, name in STRATEGIES.items()
    )


def search_form(origin: str = "", destination: str = "", strategy: str = "32") -> str:
    return f"""<section class="card"><form method="post" action="/">
<div class="grid"><div><label for="origin">出发地</label><input id="origin" name="origin" required maxlength="120" value="{esc(origin)}" placeholder="例如：上海市普陀区真北路与古浪路"></div>
<div><label for="destination">目的地</label><input id="destination" name="destination" required maxlength="120" value="{esc(destination)}" placeholder="例如：上海交通大学闵行校区"></div></div>
<label for="strategy">路线偏好</label><select id="strategy" name="strategy">{strategy_options(strategy)}</select>
<button type="submit">查询当前路线和路况</button></form></section>"""


def location_picker(origin: str, destination: str, strategy: str, origin_options: list[dict], destination_options: list[dict]) -> str:
    def options(items: list[dict], field: str) -> str:
        return "".join(
            f'<option value="{esc(item["location"])}">{esc(item["name"])} · {esc(item["district"])} · {esc(item["level"])}</option>'
            for item in items
        )

    return f"""<section class="card"><h2>确认地点</h2><p class="muted">高德可能把同一名称匹配到不同位置。请确认下面的起点和目的地后再算路。</p>
<form method="post" action="/route"><input type="hidden" name="origin" value="{esc(origin)}"><input type="hidden" name="destination" value="{esc(destination)}"><input type="hidden" name="strategy" value="{esc(strategy)}">
<label for="origin_location">出发地：{esc(origin)}</label><select id="origin_location" name="origin_location">{options(origin_options, "origin_location")}</select>
<label for="destination_location">目的地：{esc(destination)}</label><select id="destination_location" name="destination_location">{options(destination_options, "destination_location")}</select>
<button type="submit">确认地点并查询路线</button></form></section>"""


def render_routes(data: dict, origin_name: str, destination_name: str, strategy: str) -> str:
    paths = (data.get("route") or {}).get("paths") or []
    if not paths:
        raise RuntimeError("高德没有返回可用路线，请换一个附近的地标再试。")
    cards = []
    for index, path in enumerate(paths[:3], start=1):
        cost = path.get("cost") or {}
        duration = cost.get("duration")
        try:
            duration_text = f"{round(int(duration) / 60)} 分钟" if duration is not None else "暂无"
        except (TypeError, ValueError):
            duration_text = "暂无"
        try:
            distance_text = f"{int(path.get('distance', 0)) / 1000:.1f} 公里"
        except (TypeError, ValueError):
            distance_text = "暂无"
        tolls = cost.get("tolls", "未知")
        if str(tolls) not in ("", "未知", "None"):
            tolls = f"{tolls} 元"

        steps_html = []
        congested = []
        for step in path.get("steps") or []:
            road = step.get("road_name") or "道路"
            instruction = step.get("instruction") or "沿该道路行驶"
            traffic = []
            for tmc in step.get("tmcs") or []:
                status = tmc.get("tmc_status") or "路况未知"
                if status in ("缓行", "拥堵", "严重拥堵"):
                    traffic.append(status)
                    congested.append(f"{road}（{status}）")
            traffic_badges = "".join(f'<span class="badge traffic">{esc(item)}</span>' for item in dict.fromkeys(traffic))
            steps_html.append(f"<li><strong>{esc(road)}</strong>：{esc(instruction)} {traffic_badges}</li>")
        congested = list(dict.fromkeys(congested))
        traffic_summary = (
            "<p><strong>需留意路段：</strong>" + "、".join(esc(item) for item in congested[:10]) + "</p>"
            if congested else '<p class="muted">返回数据中没有标出缓行或拥堵路段。</p>'
        )
        step_details = "".join(steps_html) or '<li class="muted">高德没有返回分段指引。</li>'
        cards.append(f"""<section class="card"><div class="route-head"><h2>路线 {index}</h2><span class="badge">{esc(STRATEGIES.get(strategy, "高德推荐"))}</span></div>
<p class="metric">{esc(duration_text)} <span class="muted">· {esc(distance_text)}</span></p>
<p>预计收费：{esc(tolls)}</p>{traffic_summary}<details><summary>查看逐段路线指引</summary><ol>{step_details}</ol></details></section>""")
    # Use the Windows machine's local clock; Windows Python may not bundle IANA tzdata.
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")
    return f"""<section class="card"><h2>查询结果</h2><p><strong>起点：</strong>{esc(origin_name)}</p><p><strong>终点：</strong>{esc(destination_name)}</p><p class="muted">查询时间：{timestamp}（本机时间）</p>
<div class="notice">这是查询时高德返回的路线与路况，不代表你未来出发时的路况。出发前请打开导航复核。</div></section>{''.join(cards)}"""


class Handler(BaseHTTPRequestHandler):
    server_version = "CommuteAssistant/1.0"

    def send_page(self, body: str, status: int = 200) -> None:
        encoded = page(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path != "/":
            self.send_page('<section class="card"><h2>页面不存在</h2><a href="/">返回首页</a></section>', 404)
            return
        config_path = ROOT / "commute_config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        self.send_page(
            '<h1>上海自驾路线助手</h1><p class="muted">输入地点，查询当前驾车路线、预计耗时和沿途路况。</p>'
            + search_form(config.get("origin", ""), config.get("destination", ""))
            + '<p class="muted">查询通过高德 Web 服务完成；你的 Key 保存在本机，不会发送到网页浏览器。</p>'
        )

    def read_form(self) -> dict[str, str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= 16_384:
            raise ValueError("表单内容无效，请刷新页面后重试。")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return {key: values[0] for key, values in parse_qs(raw, keep_blank_values=True).items()}

    def do_POST(self) -> None:
        try:
            form = self.read_form()
            origin = form.get("origin", "").strip()
            destination = form.get("destination", "").strip()
            strategy = form.get("strategy", "32")
            if not origin or not destination:
                raise ValueError("请填写出发地和目的地。")
            if len(origin) > 120 or len(destination) > 120:
                raise ValueError("地点名称请控制在 120 个字符以内。")
            amap_key = os.getenv("AMAP_WEB_KEY")
            if not amap_key:
                raise RuntimeError("没有读取到已加密保存的高德 Web 服务 Key。请重新运行本机启动脚本。")

            if self.path == "/":
                origin_options = candidates_for(origin, amap_key)
                destination_options = candidates_for(destination, amap_key)
                self.send_page(
                    '<h1>上海自驾路线助手</h1>'
                    + location_picker(origin, destination, strategy, origin_options, destination_options)
                    + search_form(origin, destination, strategy)
                )
                return
            if self.path == "/route":
                origin_location = form.get("origin_location", "")
                destination_location = form.get("destination_location", "")
                data = query_routes(origin_location, destination_location, strategy, amap_key)
                result = render_routes(data, origin, destination, strategy)
                self.send_page(
                    '<h1>上海自驾路线助手</h1>' + result + search_form(origin, destination, strategy)
                )
                return
            self.send_page('<section class="card"><h2>页面不存在</h2><a href="/">返回首页</a></section>', 404)
        except (ValueError, RuntimeError) as exc:
            self.send_page(
                '<h1>上海自驾路线助手</h1><section class="card error"><h2>查询没有完成</h2><p>'
                + esc(exc) + "</p></section>" + search_form(),
                400,
            )
        except Exception:
            import sys
            import traceback

            exc = sys.exception()
            detail = f"{type(exc).__name__}: {exc}" if exc else "Unknown error"
            detail = re.sub(r"(?i)([?&]key=)[^&\s]+", r"\1[hidden]", detail)
            traceback_text = traceback.format_exc()
            traceback_text = re.sub(r"(?i)([?&]key=)[^&\s]+", r"\1[hidden]", traceback_text)
            print("Unexpected query error:\n" + traceback_text)
            self.send_page(
                '<h1>上海自驾路线助手</h1><section class="card error"><h2>暂时无法查询</h2>'
                f'<p>错误详情：{esc(detail)}</p><p>如需继续排查，请把启动窗口中的报错发给我；发送前确认没有包含任何 Key。</p></section>'
                + search_form(),
                500,
            )

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep local logs free of submitted addresses and API credentials.
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {fmt % args}")


def main() -> None:
    if not os.getenv("AMAP_WEB_KEY"):
        raise SystemExit("没有检测到 AMAP_WEB_KEY。请通过 run_commute_web.ps1 启动网页助手。")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"通勤路线助手已启动：{url}")
    print("保持这个窗口打开即可使用；按 Ctrl+C 停止服务。")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("正在停止通勤路线助手……")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
