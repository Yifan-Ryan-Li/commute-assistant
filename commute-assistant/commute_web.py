"""A small local web UI for querying current AMap driving routes."""

from __future__ import annotations

import html
import json
import math
import os
import re
import ssl
import threading
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from commute_assistant import AMAP_DRIVING_API, AMAP_GEOCODE_API, get_json
from commute_assistant import AMAP_CONVERT_API
from route_history import estimate as estimate_route_history, record_snapshot


ROOT = Path(__file__).resolve().parent
# Listen on the local network so a phone on the same Wi-Fi can connect.
# Do not expose this unauthenticated development server to the public internet.
HOST = "0.0.0.0"
PORT = 8765
AMAP_INPUTTIPS_API = "https://restapi.amap.com/v3/assistant/inputtips"
AMAP_POI_TEXT_API = "https://restapi.amap.com/v5/place/text"
AMAP_POI_AROUND_API = "https://restapi.amap.com/v5/place/around"
FACILITY_CATEGORIES = {
    "parking": ("停车场", "停车场"),
    "gas": ("加油站", "加油站"),
    "charging": ("充电站 / 充电桩", "充电站|充电桩"),
    "toilet": ("公共厕所", "公共厕所|卫生间|厕所"),
    "pharmacy": ("药房", "药店|药房"),
    "convenience": ("便利店", "便利店|便利商店"),
    "hospital": ("医院", "医院"),
    "repair": ("汽车维修", "汽车维修|汽车修理"),
    "food": ("餐饮 / 快餐", "餐饮|快餐"),
}
STRATEGIES = {
    "0": "时间优先（与定时简报一致）",
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


def destination_candidates_for(address: str, city: str, amap_key: str) -> list[dict]:
    """Find destination POI suggestions, strictly limited to the chosen city."""
    data = get_json(
        AMAP_INPUTTIPS_API,
        {
            "key": amap_key,
            "keywords": address,
            "city": city,
            "citylimit": "true",
            "datatype": "poi",
            "output": "JSON",
        },
        "高德目的地联想搜索",
    )
    candidates = []
    seen = set()
    for tip in data.get("tips") or []:
        location = tip.get("location") or ""
        if not valid_location(location) or location in seen:
            continue
        seen.add(location)
        address_detail = tip.get("address") or ""
        district = tip.get("district") or ""
        if address_detail and address_detail not in district:
            district = f"{district} {address_detail}".strip()
        candidates.append({
            "location": location,
            "name": tip.get("name") or address,
            "level": "高德地点建议",
            "district": district,
        })
    if not candidates:
        raise ValueError(f"在“{city}”范围内没有找到“{address}”的可用地点。试试补充区名、道路或更完整的地标名。")
    return candidates[:6]


def _distance_km(first: str, second: str) -> float:
    lon1, lat1 = (math.radians(float(part)) for part in first.split(","))
    lon2, lat2 = (math.radians(float(part)) for part in second.split(","))
    delta_lon, delta_lat = lon2 - lon1, lat2 - lat1
    value = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(value))


def search_destination_parking(destination: str, destination_location: str, city: str, amap_key: str) -> list[dict]:
    """Find likely destination-associated parking POIs; the user confirms the match."""
    data = get_json(
        AMAP_POI_TEXT_API,
        {
            "key": amap_key,
            "keywords": f"{destination} 停车场",
            "city": city,
            "citylimit": "true",
            "page_size": 20,
            "page_num": 1,
            "show_fields": "navi",
        },
        "高德目的地停车场搜索",
    )
    candidates = []
    for item in data.get("pois") or []:
        location = item.get("location") or ""
        if not valid_location(location):
            continue
        distance = _distance_km(destination_location, location)
        # Text matches can include same-name facilities elsewhere in the city.
        if distance > 1.5:
            continue
        navi = item.get("navi") or {}
        entrance = navi.get("entr_location") or location
        candidates.append({
            "location": entrance if valid_location(entrance) else location,
            "poi_location": location,
            "name": item.get("name") or "停车场",
            "address": item.get("address") or "",
            "distance_km": distance,
            "parking_type": item.get("parking_type") or "",
        })
    candidates.sort(key=lambda item: item["distance_km"])
    return candidates[:6]


def search_nearby_facilities(center: str, category: str, amap_key: str) -> list[dict]:
    if category not in FACILITY_CATEGORIES:
        raise ValueError("请选择有效的周边设施类型。")
    label, keywords = FACILITY_CATEGORIES[category]
    data = get_json(
        AMAP_POI_AROUND_API,
        {
            "key": amap_key,
            "location": center,
            "keywords": keywords,
            "radius": 5000,
            "sortrule": "distance",
            "page_size": 10,
            "page_num": 1,
            "show_fields": "navi,business",
        },
        f"高德附近{label}搜索",
    )
    facilities = []
    for item in data.get("pois") or []:
        location = item.get("location") or ""
        if not valid_location(location):
            continue
        try:
            distance_m = int(item.get("distance"))
        except (TypeError, ValueError):
            distance_m = round(_distance_km(center, location) * 1000)
        facilities.append({
            "location": location,
            "name": item.get("name") or label,
            "address": " ".join(part for part in (item.get("adname"), item.get("address")) if part),
            "city": item.get("cityname") or "",
            "distance_m": distance_m,
            "parking_type": item.get("parking_type") or "",
        })
    return facilities


def valid_location(value: str) -> bool:
    # Browser GPS and AMap may format the same coordinate with different
    # precision or harmless whitespace. Parse numeric values instead of
    # rejecting valid coordinates based on a fixed number of decimals.
    if not isinstance(value, str) or value.count(",") != 1:
        return False
    try:
        longitude, latitude = (float(part.strip()) for part in value.strip().split(","))
    except ValueError:
        return False
    return (
        math.isfinite(longitude)
        and math.isfinite(latitude)
        and 73 <= longitude <= 135
        and 3 <= latitude <= 54
    )


def query_routes(origin: str, destination: str, strategy: str, amap_key: str, via_points: list[str] | None = None) -> dict:
    if not valid_location(origin) or not valid_location(destination):
        raise ValueError("地点坐标无效。请返回重新选择起点和目的地。")
    via_points = via_points or []
    if len(via_points) > 16 or any(not valid_location(point) for point in via_points):
        raise ValueError("途经点坐标无效；最多可添加 16 个途经点。请返回重新选择。")
    if strategy not in STRATEGIES:
        strategy = "32"
    params = {
            "key": amap_key,
            "origin": origin,
            "destination": destination,
            "strategy": strategy,
            "alternative_route": 3,
            "show_fields": "cost,tmcs,navi",
    }
    if via_points:
        params["waypoints"] = ";".join(via_points)
    return get_json(AMAP_DRIVING_API, params, "高德驾车路线服务")


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def page(content: str, title: str = "上海自驾路线助手") -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>
:root{{color-scheme:light;--ink:#15283b;--muted:#607286;--line:#dce5ec;--blue:#1769aa;--blue-dark:#105587;--bg:#f3f7fa;--surface:#fff;--shadow:0 12px 34px #203c5210}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(ellipse at 50% -18%,#e1f0f8 0,transparent 55%),var(--bg);color:var(--ink);font:16px/1.65 "Segoe UI","Microsoft YaHei",sans-serif}}
main{{max-width:1040px;margin:42px auto;padding:0 22px}}h1{{font-size:clamp(28px,4vw,38px);letter-spacing:-.035em;margin:0 0 5px;font-weight:760}}h1+p{{color:var(--muted);margin:0 0 22px}}h2{{font-size:21px;line-height:1.35;margin:0 0 12px;letter-spacing:-.015em}}p{{margin:6px 0}}.muted{{color:var(--muted)}}
.card{{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:24px;margin:18px 0;box-shadow:var(--shadow)}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}label{{display:block;font-weight:680;margin:12px 0 6px;color:#233b50}}input,select{{width:100%;min-height:48px;border:1px solid #c7d4df;border-radius:11px;padding:10px 14px;font:inherit;color:var(--ink);background:#fff;transition:border-color .16s,box-shadow .16s}}input::placeholder{{color:#8797a6}}input:hover,select:hover{{border-color:#91aabe}}input:focus,select:focus{{outline:none;border-color:var(--blue);box-shadow:0 0 0 4px #1769aa1c}}
button,.button{{display:inline-flex;align-items:center;justify-content:center;min-height:44px;border:0;border-radius:11px;padding:10px 18px;background:var(--blue);color:white;font:inherit;font-weight:680;cursor:pointer;text-decoration:none;margin-top:16px;transition:background .16s,transform .16s,box-shadow .16s}}button:hover,.button:hover{{background:var(--blue-dark);box-shadow:0 5px 14px #1769aa2b}}button:active,.button:active{{transform:translateY(1px)}}button:focus-visible,.button:focus-visible{{outline:3px solid #64a9d8;outline-offset:3px}}
.via-point-row{{display:grid;grid-template-columns:minmax(0,2fr) minmax(130px,1fr) minmax(130px,1fr) auto;gap:10px;align-items:end;margin:9px 0}}.via-point-row input,.via-point-row select{{min-width:0}}.via-point-row label{{font-size:14px;margin:3px 0 5px}}.via-point-row button,.secondary{{background:#eaf1f6;color:#29465d;padding:8px 13px;margin-top:0;min-height:40px}}.via-point-row button:hover,.secondary:hover{{background:#dce9f2;color:#173a56;box-shadow:none}}
.facility-buttons{{display:flex;gap:9px;flex-wrap:wrap}}.facility-search-form button{{margin-top:5px;min-height:40px;padding:8px 13px}}.facility-row{{display:flex;align-items:center;justify-content:space-between;gap:12px;border-top:1px solid var(--line);padding:12px 0}}.facility-row button{{white-space:nowrap;margin:0}}
.facility-group{{border-top:1px solid var(--line);padding-top:16px;margin-top:16px}}.facility-status{{font-size:14px}}
.route-head{{display:flex;justify-content:space-between;gap:15px;align-items:center;flex-wrap:wrap}}.metric{{font-size:25px;font-weight:760;letter-spacing:-.025em;color:#123f61}}.badge{{display:inline-block;background:#eaf4fb;color:#165d91;padding:4px 10px;border-radius:99px;margin:3px;font-size:13px;font-weight:650}}
.traffic{{color:#a44321}}details{{border-top:1px solid var(--line);margin-top:18px;padding-top:13px}}summary{{cursor:pointer;font-weight:680;color:#245675}}summary:hover{{color:var(--blue-dark)}}ol{{padding-left:25px}}li{{margin:9px 0;padding-left:3px}}li::marker{{color:var(--blue);font-weight:700}}.error{{border-color:#e9aaa4;background:#fff7f6;color:#8f2f25}}.notice{{background:#eef6fb;border:1px solid #d9eaf5;border-left:4px solid var(--blue);border-radius:8px;padding:12px 15px;margin-top:16px;color:#38566b}}
@media(max-width:680px){{main{{margin:24px auto;padding:0 15px}}.card{{padding:18px;border-radius:15px;margin:14px 0}}.grid{{grid-template-columns:1fr;gap:4px}}.via-point-row{{grid-template-columns:1fr 1fr;gap:8px}}.via-point-row button{{justify-self:start}}.facility-row{{align-items:flex-start;flex-direction:column}}h1{{font-size:28px}}}}
</style></head><body><main>{content}</main></body></html>"""


def strategy_options(selected: str) -> str:
    return "".join(
        f'<option value="{key}"{" selected" if key == selected else ""}>{esc(name)}</option>'
        for key, name in STRATEGIES.items()
    )


def search_form(origin: str = "", destination: str = "", strategy: str = "32", departure_time: str = "", origin_coords: str = "", destination_scope: str = "shanghai", destination_city: str = "", via_points: list[tuple[str, str, str]] | None = None) -> str:
    if destination_scope not in {"shanghai", "other"}:
        destination_scope = "shanghai"
    city_required = " required" if destination_scope == "other" else ""
    via_points = via_points or []
    via_rows = "".join(via_point_row(*point) for point in via_points) or via_point_row()
    return f"""<section class="card"><form method="post" action="/">
<div class="grid"><div><label for="origin">出发地</label><input id="origin" name="origin" required maxlength="120" value="{esc(origin)}" placeholder="例如：上海市普陀区真北路与古浪路">
<input type="hidden" id="origin_coords" name="origin_coords" value="{esc(origin_coords)}"><button type="button" id="use-current-location">使用当前位置</button><p id="location-status" class="muted" aria-live="polite">点击后浏览器会请求定位权限；坐标仅用于本次查询，会发送给高德，不写入本机历史样本。</p></div>
<div><label for="destination_scope">目的地城市范围</label><select id="destination_scope" name="destination_scope"><option value="shanghai"{" selected" if destination_scope == "shanghai" else ""}>上海市内</option><option value="other"{" selected" if destination_scope == "other" else ""}>其他城市</option></select>
<div id="destination_city_wrap"{" hidden" if destination_scope == "shanghai" else ""}><label for="destination_city">目的地城市</label><input id="destination_city" name="destination_city" maxlength="60" value="{esc(destination_city)}" placeholder="例如：苏州市、杭州市"{city_required}></div>
<label for="destination">目的地</label><input id="destination" name="destination" required maxlength="120" value="{esc(destination)}" placeholder="可输入简称，例如：港汇"></div></div>
<div id="via-points"><label>途经点（可选，按顺序经过）</label>{via_rows}</div><button type="button" id="add-via-point" class="secondary">+ 添加途经点</button>
<label for="strategy">路线偏好</label><select id="strategy" name="strategy">{strategy_options(strategy)}</select>
<label for="departure_time">计划出发时间（留空查询当前路况）</label><input id="departure_time" name="departure_time" type="datetime-local" value="{esc(departure_time)}">
<p class="muted">选择未来时间后，助手会根据本机积累的相近星期和时段记录估算；样本不足时会明确提示。</p>
<button type="submit">查询路线</button></form></section>{DESTINATION_SCOPE_SCRIPT}{LOCATION_BUTTON_SCRIPT}{VIA_POINTS_SCRIPT}"""


VIA_POINTS_SCRIPT = """<script>
(() => {
  const container = document.getElementById('via-points');
  const add = document.getElementById('add-via-point');
  if (!container || !add) return;
  const bindRow = (row) => {
    const scope = row.querySelector('.via-point-scope');
    const cityWrap = row.querySelector('.via-point-city-wrap');
    const city = row.querySelector('.via-point-city');
    const updateCity = () => {
      const other = scope.value === 'other';
      cityWrap.hidden = !other;
      city.required = other;
    };
    scope.addEventListener('change', updateCity);
    updateCity();
    row.querySelector('.remove-via-point').addEventListener('click', () => {
    const rows = container.querySelectorAll('.via-point-row');
      if (rows.length > 1) row.remove();
      else {
        row.querySelector('[name="via_points"]').value = '';
        scope.value = 'shanghai';
        city.value = '';
        updateCity();
      }
    });
  };
  container.querySelectorAll('.via-point-row').forEach(bindRow);
  add.addEventListener('click', () => {
    if (container.querySelectorAll('.via-point-row').length >= 16) return;
    const row = document.createElement('div');
    row.className = 'via-point-row';
    row.innerHTML = '<div><label>途经点</label><input name="via_points" maxlength="120" placeholder="输入要经过的地点"></div><div><label>城市范围</label><select name="via_point_scopes" class="via-point-scope"><option value="shanghai">上海市内</option><option value="other">其他城市</option></select></div><div class="via-point-city-wrap" hidden><label>所在城市</label><input name="via_point_cities" class="via-point-city" maxlength="60" placeholder="如：苏州市"></div><button type="button" class="remove-via-point">删除</button>';
    bindRow(row);
    container.appendChild(row);
  });
})();
</script>"""


def via_point_row(query: str = "", scope: str = "shanghai", city: str = "") -> str:
    if scope not in {"shanghai", "other"}:
        scope = "shanghai"
    city_required = " required" if scope == "other" else ""
    return (
        '<div class="via-point-row"><div><label>途经点</label>'
        f'<input name="via_points" maxlength="120" value="{esc(query)}" placeholder="例如：港汇恒隆广场"></div>'
        '<div><label>城市范围</label><select name="via_point_scopes" class="via-point-scope">'
        f'<option value="shanghai"{" selected" if scope == "shanghai" else ""}>上海市内</option>'
        f'<option value="other"{" selected" if scope == "other" else ""}>其他城市</option></select></div>'
        f'<div class="via-point-city-wrap"{" hidden" if scope == "shanghai" else ""}><label>所在城市</label>'
        f'<input name="via_point_cities" class="via-point-city" maxlength="60" value="{esc(city)}" placeholder="如：苏州市"{city_required}></div>'
        '<button type="button" class="remove-via-point">删除</button></div>'
    )


DESTINATION_SCOPE_SCRIPT = """<script>
(() => {
  const scope = document.getElementById('destination_scope');
  const cityWrap = document.getElementById('destination_city_wrap');
  const city = document.getElementById('destination_city');
  if (!scope || !cityWrap || !city) return;
  const updateCityField = () => {
    const otherCity = scope.value === 'other';
    cityWrap.hidden = !otherCity;
    city.required = otherCity;
  };
  scope.addEventListener('change', updateCityField);
  updateCityField();
})();
</script>"""


LOCATION_BUTTON_SCRIPT = """<script>
(() => {
  const button = document.getElementById('use-current-location');
  if (!button) return;
  const input = document.getElementById('origin');
  const coordinateField = document.getElementById('origin_coords');
  const status = document.getElementById('location-status');
  input.addEventListener('input', () => { coordinateField.value = ''; });
  button.addEventListener('click', () => {
    if (!window.isSecureContext) {
      status.textContent = '此页面不是安全连接，浏览器会阻止定位。请在电脑上用 localhost 验证；手机经局域网访问需要 HTTPS。';
      return;
    }
    if (!navigator.geolocation) {
      status.textContent = '当前浏览器不支持定位，请手动填写起点。';
      return;
    }
    button.disabled = true;
    status.textContent = '正在获取当前位置，请稍候……';
    navigator.geolocation.getCurrentPosition((position) => {
      const longitude = position.coords.longitude.toFixed(6);
      const latitude = position.coords.latitude.toFixed(6);
      coordinateField.value = `${longitude},${latitude}`;
      const accuracy = Math.max(1, Math.round(position.coords.accuracy));
      input.value = `当前位置（定位精度约 ${accuracy} 米）`;
      status.textContent = '已获取位置。确认起点无误后提交；如位置不准，可手动修改起点。';
      button.disabled = false;
    }, (error) => {
      const messages = {
        1: '你拒绝了定位权限。可在浏览器设置中允许定位，或手动填写起点。',
        2: '设备暂时无法确定位置，请检查系统定位设置，或手动填写起点。',
        3: '定位超时，请移到信号较好的位置重试，或手动填写起点。'
      };
      status.textContent = messages[error.code] || '定位失败，请手动填写起点。';
      button.disabled = false;
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
  });
})();
</script>"""


def location_picker(origin: str, destination: str, strategy: str, departure_time: str, origin_coords: str, destination_scope: str, destination_city: str, origin_options: list[dict], destination_options: list[dict], via_options: list[tuple[str, list[dict], str, str]]) -> str:
    def options(items: list[dict], field: str) -> str:
        return "".join(
            f'<option value="{esc(item["location"])}">{esc(item["name"])} · {esc(item["district"])} · {esc(item["level"])}</option>'
            for item in items
        )

    via_fields = []
    for index, (query, items, scope, city) in enumerate(via_options):
        via_fields.append(
            f'<input type="hidden" name="via_point_name_{index}" value="{esc(query)}">'
            f'<input type="hidden" name="via_point_scope_{index}" value="{esc(scope)}">'
            f'<input type="hidden" name="via_point_city_{index}" value="{esc(city)}">'
            f'<label for="via_point_location_{index}">途经点 {index + 1}（{esc(city or "上海")}）：{esc(query)}</label>'
            f'<select id="via_point_location_{index}" name="via_point_location_{index}">{options(items, f"via_point_location_{index}")}</select>'
        )
    return f"""<section class="card"><h2>确认地点</h2><p class="muted">高德可能把同一名称匹配到不同位置。请确认起点、途经点和目的地；途经点会按列表顺序经过。</p>
<form method="post" action="/route"><input type="hidden" name="origin" value="{esc(origin)}"><input type="hidden" name="destination" value="{esc(destination)}"><input type="hidden" name="strategy" value="{esc(strategy)}"><input type="hidden" name="departure_time" value="{esc(departure_time)}"><input type="hidden" name="origin_coords" value="{esc(origin_coords)}"><input type="hidden" name="destination_scope" value="{esc(destination_scope)}"><input type="hidden" name="destination_city" value="{esc(destination_city)}">
<label for="origin_location">出发地：{esc(origin)}</label><select id="origin_location" name="origin_location">{options(origin_options, "origin_location")}</select>
<label for="destination_location">目的地：{esc(destination)}</label><select id="destination_location" name="destination_location">{options(destination_options, "destination_location")}</select>
<input type="hidden" name="via_point_count" value="{len(via_options)}">{''.join(via_fields)}
<button type="submit">确认地点并查询路线</button></form></section>"""


def trip_context_fields(origin: str, destination: str, strategy: str, departure_time: str, origin_coords: str,
                        destination_scope: str, destination_city: str, origin_location: str,
                        destination_location: str, via_locations: list[str], via_names: list[str],
                        via_scopes: list[str], via_cities: list[str], route_destination: str = "",
                        route_destination_name: str = "") -> str:
    fields = [
        ("origin", origin), ("destination", destination), ("strategy", strategy),
        ("departure_time", departure_time), ("origin_coords", origin_coords),
        ("destination_scope", destination_scope), ("destination_city", destination_city),
        ("origin_location", origin_location), ("destination_location", destination_location),
        ("via_point_count", str(len(via_locations))),
    ]
    if route_destination:
        fields.extend([
            ("arrival_selected", "1"),
            ("route_destination_location", route_destination),
            ("route_destination_name", route_destination_name),
        ])
    fields_html = "".join(
        f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'
        for name, value in fields
    )
    for index, location in enumerate(via_locations):
        for name, value in (
            (f"via_point_location_{index}", location),
            (f"via_point_name_{index}", via_names[index]),
            (f"via_point_scope_{index}", via_scopes[index]),
            (f"via_point_city_{index}", via_cities[index]),
        ):
            fields_html += f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'
    return fields_html


def arrival_picker(origin: str, destination: str, strategy: str, departure_time: str, origin_coords: str,
                   destination_scope: str, destination_city: str, origin_location: str,
                   destination_location: str, via_locations: list[str], via_names: list[str],
                   via_scopes: list[str], via_cities: list[str], parking: list[dict],
                   search_message: str = "") -> str:
    direct_fields = trip_context_fields(
        origin, destination, strategy, departure_time, origin_coords, destination_scope, destination_city,
        origin_location, destination_location, via_locations, via_names, via_scopes, via_cities,
        destination_location, destination,
    )
    options = [
        '<form method="post" action="/route" class="card"><h2>到目的地</h2>'
        '<p>按已选地点作为路线终点。</p>' + direct_fields
        + '<button type="submit">规划到目的地</button></form>'
    ]
    for item in parking:
        context = trip_context_fields(
            origin, destination, strategy, departure_time, origin_coords, destination_scope, destination_city,
            origin_location, destination_location, via_locations, via_names, via_scopes, via_cities,
            item["location"], item["name"],
        )
        distance = f'{item["distance_km"]:.2f} 公里直线距离'
        details = " · ".join(part for part in (item.get("parking_type"), item.get("address"), distance) if part)
        options.append(
            '<form method="post" action="/route" class="card"><h2>停车后到达</h2>'
            f'<p><strong>{esc(item["name"])}</strong></p><p class="muted">{esc(details)}</p>'
            + context + '<button type="submit">导航到这个停车场</button></form>'
        )
    if parking:
        note = '<p class="muted">以下是目的地附近、名称匹配的停车场候选，请确认是否属于该目的地。</p>'
    else:
        note = '<p class="muted">暂未找到可信的目的地相关停车场。你仍可先规划到目的地，之后再搜索附近停车场。</p>'
    if search_message:
        note += f'<p class="muted">停车场候选暂不可用：{esc(search_message)}</p>'
    return (
        '<section class="card"><h2>选择实际到达位置</h2>'
        f'<p><strong>目的地：</strong>{esc(destination)}</p>' + note + '</section>'
        + "".join(options)
    )


def facility_search_controls(origin: str, destination: str, strategy: str, departure_time: str, origin_coords: str,
                             destination_scope: str, destination_city: str, origin_location: str,
                             destination_location: str, route_destination: str, route_destination_name: str,
                             via_locations: list[str], via_names: list[str], via_scopes: list[str],
                             via_cities: list[str]) -> str:
    destination_buttons = []
    current_buttons = []
    for category, (label, _) in FACILITY_CATEGORIES.items():
        context = trip_context_fields(
            origin, destination, strategy, departure_time, origin_coords, destination_scope, destination_city,
            origin_location, destination_location, via_locations, via_names, via_scopes, via_cities,
            route_destination, route_destination_name,
        )
        destination_buttons.append(
            f'<form method="post" action="/facilities" class="facility-search-form">{context}'
            f'<input type="hidden" name="facility_center_source" value="destination">'
            f'<input type="hidden" name="facility_category" value="{esc(category)}">'
            f'<button type="submit">附近{esc(label)}</button></form>'
        )
        current_context = trip_context_fields(
            origin, destination, strategy, departure_time, origin_coords, destination_scope, destination_city,
            origin_location, destination_location, via_locations, via_names, via_scopes, via_cities,
            route_destination, route_destination_name,
        )
        current_buttons.append(
            f'<form method="post" action="/facilities" class="facility-search-form current-location-facility-form">{current_context}'
            '<input type="hidden" name="facility_center_source" value="current">'
            f'<input type="hidden" name="facility_category" value="{esc(category)}">'
            '<input type="hidden" name="facility_gps_location" value="">'
            f'<button type="button" class="current-location-facility-button">当前位置附近{esc(label)}</button>'
            '<p class="muted facility-status" aria-live="polite"></p></form>'
        )
    return (
        '<section class="card"><h2>搜索周边配套</h2>'
        '<div class="facility-group"><strong>目的地附近</strong><p class="muted">以实际导航终点为中心，搜索 5 公里内地点。</p><div class="facility-buttons">'
        + "".join(destination_buttons) + '</div></div>'
        '<div class="facility-group"><strong>当前位置附近</strong><p class="muted">点击后浏览器会请求定位权限，按你此刻的位置搜索；适合途中找加油站或充电站。</p><div class="facility-buttons">'
        + "".join(current_buttons) + '</div></div></section>' + CURRENT_FACILITY_LOCATION_SCRIPT
    )


CURRENT_FACILITY_LOCATION_SCRIPT = """<script>
(() => {
  document.querySelectorAll('.current-location-facility-button').forEach((button) => {
    button.addEventListener('click', () => {
      const form = button.closest('form');
      const status = form.querySelector('.facility-status');
      const location = form.querySelector('[name="facility_gps_location"]');
      if (!window.isSecureContext) {
        status.textContent = '浏览器阻止了定位。电脑请使用 localhost；手机经局域网访问需先启用 HTTPS。';
        return;
      }
      if (!navigator.geolocation) {
        status.textContent = '此浏览器不支持定位，请换支持定位的浏览器或设备。';
        return;
      }
      button.disabled = true;
      status.textContent = '正在获取当前位置……';
      navigator.geolocation.getCurrentPosition((position) => {
        location.value = `${position.coords.longitude.toFixed(6)},${position.coords.latitude.toFixed(6)}`;
        status.textContent = '位置已获取，正在搜索附近地点……';
        form.submit();
      }, (error) => {
        const messages = {
          1: '你拒绝了定位权限，请在浏览器设置中允许定位后重试。',
          2: '设备暂时无法确定位置，请检查系统定位设置。',
          3: '定位超时，请重试。'
        };
        status.textContent = messages[error.code] || '定位失败，请重试。';
        button.disabled = false;
      }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 30000 });
    });
  });
})();
</script>"""


def render_facilities(facilities: list[dict], category: str, origin: str, destination: str, strategy: str,
                      departure_time: str, origin_coords: str, destination_scope: str, destination_city: str,
                      origin_location: str, destination_location: str, route_destination: str,
                      route_destination_name: str, via_locations: list[str], via_names: list[str],
                      via_scopes: list[str], via_cities: list[str], center_source: str,
                      current_center: str = "", current_gps: str = "") -> str:
    label = FACILITY_CATEGORIES[category][0]
    center_label = "当前位置" if center_source == "current" else "目的地附近"
    if not facilities:
        return f'<section class="card"><h2>{esc(center_label)}的{esc(label)}</h2><p>5 公里范围内没有找到相关地点。</p></section>'
    rows = []
    for item in facilities:
        distance = f'{item["distance_m"]} 米直线距离'
        details = " · ".join(part for part in (item.get("parking_type"), item.get("address"), distance) if part)
        if center_source == "current":
            # Replan from the just-confirmed current location directly to the chosen facility.
            context = trip_context_fields(
                "当前位置", item["name"], strategy, "", current_gps, "other", item.get("city") or "上海",
                current_center, item["location"], [], [], [], [], item["location"], item["name"],
            )
        else:
            context = trip_context_fields(
                origin, destination, strategy, departure_time, origin_coords, destination_scope, destination_city,
                origin_location, destination_location, via_locations, via_names, via_scopes, via_cities,
                item["location"], item["name"],
            )
        rows.append(
            '<form method="post" action="/route" class="facility-row"><div>'
            f'<strong>{esc(item["name"])}</strong><p class="muted">{esc(details)}</p></div>'
            + context + '<button type="submit">改为路线终点</button></form>'
        )
    return f'<section class="card"><h2>{esc(center_label)}的{esc(label)}</h2><p class="muted">结果按距离排序；所示为直线距离。</p>{"".join(rows)}</section>'


def parse_departure_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        selected = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("出发时间格式无效，请重新选择。") from exc
    if selected <= datetime.now():
        raise ValueError("所选时间已经过去。查询当前路况请留空；预估请选一个未来时间。")
    if selected > datetime.now() + timedelta(days=14):
        raise ValueError("当前历史估算仅支持未来 14 天内的出发时间。")
    return selected


def simplify_step_instruction(step: dict) -> str:
    """Prefer AMap's explicit maneuver over compass bearings in step text."""
    instruction = step.get("instruction") or "沿该道路行驶"
    navi = step.get("navi") or {}
    action = navi.get("action") or step.get("action")
    assistant_action = navi.get("assistant_action") or step.get("assistant_action")
    maneuver = action or assistant_action
    if not maneuver:
        # A compass bearing alone cannot safely be translated into left/right.
        return instruction

    simplified = re.sub(
        r"向(?:东北|东南|西北|西南|东|西|南|北)(?:方向)?行驶",
        "继续行驶",
        instruction,
    )
    if maneuver not in simplified:
        simplified = f"{simplified.rstrip('。')}，然后{maneuver}"
    return simplified


def render_history_estimate(data: dict, origin_name: str, destination_name: str, strategy: str, departure_at: datetime, via_names: list[str] | None = None, route_destination_name: str = "") -> str:
    heading = (f"<section class=\"card\"><h2>未来路况估算</h2><p><strong>计划出发：</strong>"
               f"{esc(departure_at.strftime('%Y-%m-%d %H:%M'))}</p><p><strong>起点：</strong>{esc(origin_name)}</p>"
               f"<p><strong>目的地：</strong>{esc(destination_name)}</p>"
               + (f"<p><strong>实际导航终点：</strong>{esc(route_destination_name)}</p>"
                  if route_destination_name and route_destination_name != destination_name else "")
               + (f"<p><strong>途经：</strong>{' → '.join(esc(name) for name in via_names)}</p>" if via_names else "")
               + f"<p>路线偏好：{esc(STRATEGIES.get(strategy, '高德推荐'))}</p>")
    if not data.get("enough_data"):
        count = data.get("sample_days", 0)
        return heading + (
            f"<p>目前只有 {count} 个符合条件的历史日期，至少需要 3 个不同日期的记录才能给出估算。</p>"
            "<div class=\"notice\">请继续在出发时查询实时路线，或运行早晚定时简报积累样本。记录只保存在这台电脑。"
            "在样本足够前，助手不会用当前路况冒充未来预测。</div></section>"
        )
    cards = []
    for route in data.get("routes", []):
        duration = route.get("duration_minutes")
        duration_text = f"约 {round(duration)} 分钟" if duration is not None else "耗时样本不足"
        distance = route.get("distance_km")
        distance_text = f" · {distance:.1f} 公里" if distance is not None else ""
        toll = route.get("tolls_yuan")
        toll_text = f"约 {toll:.0f} 元" if toll is not None else "未知"
        congestion = route.get("congestion", {})
        slow_count = sum(congestion.get(label, 0) for label in ("缓行", "拥堵", "严重拥堵"))
        roads = route.get("slow_roads") or []
        roads_text = "、".join(esc(road) for road in roads[:6]) if roads else "历史样本没有稳定记录到固定拥堵路段"
        cards.append(
            f"<section class=\"card\"><h2>历史路线 {route['index']}</h2>"
            f"<p class=\"metric\">{esc(duration_text)}<span class=\"muted\">{esc(distance_text)}</span></p>"
            f"<p>历史收费中位数：{esc(toll_text)}</p><p>历史缓行/拥堵路段中位数：{slow_count} 段</p>"
            f"<p>常见需留意道路：{roads_text}</p><p class=\"muted\">参考 {route['sample_count']} 条相近时段记录</p></section>"
        )
    return heading + (
        f"<p>估算基于 {data['sample_days']} 个不同日期的记录；耗时与收费显示历史中位数，不是高德实时预测。</p></section>"
        + "".join(cards)
        + '<section class="card notice">交通会受事故、天气、施工和临时管制影响。请在出发前再查实时导航。</section>'
    )


def render_routes(data: dict, origin_name: str, destination_name: str, strategy: str, via_names: list[str] | None = None, route_destination_name: str = "") -> str:
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
            instruction = simplify_step_instruction(step)
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
    via_summary = f"<p><strong>途经：</strong>{' → '.join(esc(name) for name in via_names)}</p>" if via_names else ""
    arrival_summary = f"<p><strong>实际导航终点：</strong>{esc(route_destination_name)}</p>" if route_destination_name and route_destination_name != destination_name else ""
    return f"""<section class="card"><h2>查询结果</h2><p><strong>起点：</strong>{esc(origin_name)}</p>{via_summary}<p><strong>目的地：</strong>{esc(destination_name)}</p>{arrival_summary}<p class="muted">查询时间：{timestamp}（本机时间）</p>
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
            '<h1>上海自驾路线助手</h1><p class="muted">可查询当前路线和路况，也可根据本机历史记录估算未来出发时段。</p>'
            + search_form(config.get("origin", ""), config.get("destination", ""))
            + '<p class="muted">查询通过高德 Web 服务完成；你的 Key 保存在本机，不会发送到网页浏览器。</p>'
        )

    def read_form(self) -> dict[str, str | list[str]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= 16_384:
            raise ValueError("表单内容无效，请刷新页面后重试。")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        parsed = parse_qs(raw, keep_blank_values=True)
        repeated_fields = {"via_points", "via_point_scopes", "via_point_cities"}
        return {key: (values if key in repeated_fields else values[0]) for key, values in parsed.items()}

    def do_POST(self) -> None:
        origin = ""
        destination = ""
        strategy = "32"
        departure_time = ""
        origin_coords = ""
        destination_scope = "shanghai"
        destination_city = ""
        via_specs: list[tuple[str, str, str]] = []
        try:
            form = self.read_form()
            origin = form.get("origin", "").strip()
            destination = form.get("destination", "").strip()
            strategy = form.get("strategy", "32")
            departure_time = form.get("departure_time", "").strip()
            origin_coords = form.get("origin_coords", "").strip()
            destination_scope = form.get("destination_scope", "shanghai").strip()
            destination_city = form.get("destination_city", "").strip()
            def field_values(name: str) -> list[str]:
                value = form.get(name, [])
                return value if isinstance(value, list) else [value]

            submitted_via = field_values("via_points")
            submitted_scopes = field_values("via_point_scopes")
            submitted_cities = field_values("via_point_cities")
            for index, raw_query in enumerate(submitted_via):
                query = raw_query.strip()
                if not query:
                    continue
                scope = submitted_scopes[index].strip() if index < len(submitted_scopes) else "shanghai"
                city = submitted_cities[index].strip() if index < len(submitted_cities) else ""
                if scope not in {"shanghai", "other"}:
                    raise ValueError(f"途经点 {index + 1} 的城市范围无效。")
                if scope == "other" and not city:
                    raise ValueError(f"请填写途经点“{query}”所在的城市。")
                if len(city) > 60:
                    raise ValueError(f"途经点“{query}”的城市名称请控制在 60 个字符以内。")
                via_specs.append((query, scope, city if scope == "other" else ""))
            if len(via_specs) > 16:
                raise ValueError("最多可以添加 16 个途经点。")
            if any(len(point[0]) > 120 for point in via_specs):
                raise ValueError("每个途经点名称请控制在 120 个字符以内。")
            if not origin or not destination:
                raise ValueError("请填写出发地和目的地。")
            if len(origin) > 120 or len(destination) > 120:
                raise ValueError("地点名称请控制在 120 个字符以内。")
            if destination_scope not in {"shanghai", "other"}:
                raise ValueError("请选择有效的目的地城市范围。")
            if destination_scope == "other":
                if not destination_city:
                    raise ValueError("请先填写目的地所在城市。")
                if len(destination_city) > 60:
                    raise ValueError("目的地城市请控制在 60 个字符以内。")
                destination_search_city = destination_city
            else:
                destination_city = ""
                destination_search_city = "上海"
            amap_key = os.getenv("AMAP_WEB_KEY")
            if not amap_key:
                raise RuntimeError("没有读取到已加密保存的高德 Web 服务 Key。请重新运行本机启动脚本。")

            if self.path == "/":
                if origin_coords:
                    if not valid_location(origin_coords):
                        raise ValueError("设备定位坐标无效，请重新定位或手动填写起点。")
                    conversion_data = get_json(
                        AMAP_CONVERT_API,
                        {"key": amap_key, "locations": origin_coords, "coordsys": "gps", "output": "JSON"},
                        "高德 GPS 坐标转换服务",
                    )
                    converted_locations = conversion_data.get("locations")
                    converted = (
                        converted_locations.strip().split(";", 1)[0].strip()
                        if isinstance(converted_locations, str)
                        else ""
                    )
                    if not valid_location(converted):
                        response_info = str(conversion_data.get("info") or "未提供详情")[:120]
                        raise RuntimeError(
                            "高德坐标转换接口未返回可识别的经纬度。"
                            f"接口状态：{response_info}。请重新定位或手动填写起点。"
                        )
                    origin_options = [{"location": converted, "name": origin, "district": "", "level": "设备定位"}]
                else:
                    origin_options = candidates_for(origin, amap_key)
                destination_options = destination_candidates_for(destination, destination_search_city, amap_key)
                via_options = [
                    (query, destination_candidates_for(query, city or "上海", amap_key), scope, city)
                    for query, scope, city in via_specs
                ]
                self.send_page(
                    '<h1>上海自驾路线助手</h1>'
                    + location_picker(origin, destination, strategy, departure_time, origin_coords, destination_scope, destination_city, origin_options, destination_options, via_options)
                    + search_form(origin, destination, strategy, departure_time, origin_coords, destination_scope, destination_city, via_specs)
                )
                return
            if self.path in {"/route", "/facilities"}:
                origin_location = form.get("origin_location", "")
                destination_location = form.get("destination_location", "")
                if not valid_location(str(origin_location)) or not valid_location(str(destination_location)):
                    raise ValueError("起点或目的地坐标无效，请返回重新选择地点。")
                try:
                    via_count = int(form.get("via_point_count", "0"))
                except ValueError as exc:
                    raise ValueError("途经点信息无效，请重新选择地点。") from exc
                if not 0 <= via_count <= 16:
                    raise ValueError("途经点数量无效；最多可以添加 16 个途经点。")
                via_locations = [str(form.get(f"via_point_location_{index}", "")).strip() for index in range(via_count)]
                via_names = [str(form.get(f"via_point_name_{index}", "")).strip() for index in range(via_count)]
                via_scopes = [str(form.get(f"via_point_scope_{index}", "shanghai")).strip() for index in range(via_count)]
                via_cities = [str(form.get(f"via_point_city_{index}", "")).strip() for index in range(via_count)]
                if any(not valid_location(point) for point in via_locations):
                    raise ValueError("有途经点坐标无效，请返回重新选择地点。")
                if any(scope not in {"shanghai", "other"} or (scope == "other" and not city)
                       for scope, city in zip(via_scopes, via_cities)):
                    raise ValueError("途经点城市范围信息无效，请返回重新选择地点。")
                via_specs = list(zip(via_names, via_scopes, via_cities))
                arrival_selected = form.get("arrival_selected", "") == "1"
                route_destination = str(form.get("route_destination_location", destination_location)).strip()
                route_destination_name = str(form.get("route_destination_name", destination)).strip()
                if not valid_location(route_destination):
                    raise ValueError("实际导航终点坐标无效，请重新选择地点。")

                if self.path == "/route" and not arrival_selected:
                    parking = []
                    parking_error = ""
                    try:
                        parking = search_destination_parking(
                            destination,
                            str(destination_location),
                            destination_search_city,
                            amap_key,
                        )
                    except RuntimeError as exc:
                        parking_error = str(exc)
                    body = arrival_picker(
                        origin, destination, strategy, departure_time, origin_coords,
                        destination_scope, destination_city, str(origin_location),
                        str(destination_location), via_locations, via_names, via_scopes,
                        via_cities, parking, parking_error,
                    )
                    self.send_page('<h1>上海自驾路线助手</h1>' + body + search_form(
                        origin, destination, strategy, departure_time, origin_coords,
                        destination_scope, destination_city, via_specs,
                    ))
                    return

                if self.path == "/facilities":
                    category = str(form.get("facility_category", ""))
                    center_source = str(form.get("facility_center_source", "destination"))
                    current_gps = str(form.get("facility_gps_location", "")).strip()
                    facility_center = route_destination
                    facilities = []
                    facility_error = ""
                    try:
                        if center_source == "current":
                            if not valid_location(current_gps):
                                raise ValueError("当前位置坐标无效，请重新定位。")
                            conversion = get_json(
                                AMAP_CONVERT_API,
                                {"key": amap_key, "locations": current_gps, "coordsys": "gps", "output": "JSON"},
                                "高德 GPS 坐标转换服务",
                            )
                            converted = str(conversion.get("locations") or "").split(";", 1)[0].strip()
                            if not valid_location(converted):
                                raise RuntimeError("高德未能转换当前位置坐标，请重新定位后再试。")
                            facility_center = converted
                        elif center_source != "destination":
                            raise ValueError("周边搜索位置来源无效，请重新选择。")
                        facilities = search_nearby_facilities(facility_center, category, amap_key)
                    except RuntimeError as exc:
                        facility_error = str(exc)
                    result = render_facilities(
                        facilities, category if category in FACILITY_CATEGORIES else "parking",
                        origin, destination, strategy, departure_time, origin_coords,
                        destination_scope, destination_city, str(origin_location),
                        str(destination_location), route_destination, route_destination_name,
                        via_locations, via_names, via_scopes, via_cities, center_source,
                        facility_center if center_source == "current" else "", current_gps,
                    )
                    if facility_error:
                        result += f'<section class="card error"><p>周边设施暂时无法查询：{esc(facility_error)}</p><p>请确认高德 Web 服务 Key 已开通 POI 搜索权限。</p></section>'
                    result += facility_search_controls(
                        origin, destination, strategy, departure_time, origin_coords,
                        destination_scope, destination_city, str(origin_location),
                        str(destination_location), route_destination, route_destination_name,
                        via_locations, via_names, via_scopes, via_cities,
                    )
                    self.send_page('<h1>上海自驾路线助手</h1>' + result + search_form(
                        origin, destination, strategy, departure_time, origin_coords,
                        destination_scope, destination_city, via_specs,
                    ))
                    return

                selected_departure = parse_departure_time(departure_time)
                if selected_departure:
                    history = estimate_route_history(origin_location, route_destination, strategy, selected_departure, via_locations)
                    result = render_history_estimate(history, origin, destination, strategy, selected_departure, via_names, route_destination_name)
                else:
                    data = query_routes(origin_location, route_destination, strategy, amap_key, via_locations)
                    paths = (data.get("route") or {}).get("paths") or []
                    if not origin_coords:
                        record_snapshot(origin_location, route_destination, strategy, paths, via_points=via_locations)
                    result = render_routes(data, origin, destination, strategy, via_names, route_destination_name)
                result += facility_search_controls(
                    origin, destination, strategy, departure_time, origin_coords,
                    destination_scope, destination_city, str(origin_location),
                    str(destination_location), route_destination, route_destination_name,
                    via_locations, via_names, via_scopes, via_cities,
                )
                self.send_page(
                    '<h1>上海自驾路线助手</h1>' + result + search_form(origin, destination, strategy, departure_time, origin_coords, destination_scope, destination_city, via_specs)
                )
                return
            self.send_page('<section class="card"><h2>页面不存在</h2><a href="/">返回首页</a></section>', 404)
        except (ValueError, RuntimeError) as exc:
            self.send_page(
                '<h1>上海自驾路线助手</h1><section class="card error"><h2>查询没有完成</h2><p>'
                + esc(exc) + "</p></section>" + search_form(origin, destination, strategy, departure_time, origin_coords, destination_scope, destination_city, via_specs),
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
                + search_form(origin, destination, strategy, departure_time, origin_coords, destination_scope, destination_city, via_specs),
                500,
            )

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep local logs free of submitted addresses and API credentials.
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {fmt % args}")


def main() -> None:
    if not os.getenv("AMAP_WEB_KEY"):
        raise SystemExit("没有检测到 AMAP_WEB_KEY。请通过 run_commute_web.ps1 启动网页助手。")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    cert_path = ROOT / "commute-local-cert.pem"
    key_path = ROOT / "commute-local-key.pem"
    if cert_path.is_file() and key_path.is_file():
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
        print("已启用本地 HTTPS；手机定位需要在 iPhone 上信任 commute-root-ca.pem。")
    else:
        scheme = "http"
        print("尚未配置本地 HTTPS；手机浏览器可能会阻止定位。")
    url = f"{scheme}://localhost:{PORT}"
    print(f"通勤路线助手已启动（本机）：{url}")
    print(f"手机同一 Wi-Fi 访问：打开 {scheme}://电脑IPv4地址:{PORT}")
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
