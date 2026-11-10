"""Offline checks for the commute assistant's historical and failure behavior.

Run from this directory with the project's Python environment:
    python .\verify_task2.py

All route history and generated reports use temporary directories. No network calls
or API keys are required.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import commute_assistant
import commute_web
import route_history


def matching_days() -> list[date]:
    today = date.today()
    return [
        today - timedelta(days=offset)
        for offset in range(1, 90)
        if (today - timedelta(days=offset)).weekday() == today.weekday()
    ][:3]


def observed_at(day: date) -> datetime:
    return datetime.combine(day, time(8), tzinfo=timezone(timedelta(hours=8)))


def route(polyline: str, duration_seconds: int, distance_m: int = 10000) -> dict:
    return {
        "distance": str(distance_m),
        "cost": {"duration": str(duration_seconds), "tolls": "0"},
        "polyline": polyline,
        "steps": [],
    }


def test_config() -> dict:
    return {
        "city": "上海",
        "origin": "测试起点",
        "destination": "测试终点",
        "morning_start": 7,
        "morning_end": 10,
        "evening_start": 17,
        "evening_end": 20,
    }


class TaskTwoOfflineChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = route_history.DB_PATH
        route_history.DB_PATH = Path(self.temp_dir.name) / "history.sqlite3"

    def tearDown(self) -> None:
        route_history.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_same_day_repeats_do_not_outweigh_other_days(self) -> None:
        days = matching_days()
        self.assertEqual(len(days), 3)
        for day, seconds, count in ((days[0], 1200, 1), (days[1], 1500, 1), (days[2], 3600, 100)):
            for _ in range(count):
                route_history.record_snapshot(
                    "O", "D", "test", [route("121,31;121.001,31.001", seconds)], observed_at=observed_at(day)
                )
        result = route_history.estimate(
            "O", "D", "test", observed_at(date.today())
        )
        self.assertEqual(result["routes"][0]["sample_days"], 3)
        self.assertEqual(result["routes"][0]["sample_count"], 102)
        self.assertEqual(result["routes"][0]["duration_minutes"], 25)

    def test_route_order_does_not_mix_paths_and_sparse_route_is_excluded(self) -> None:
        days = matching_days()
        self.assertEqual(len(days), 3)
        route_a = "121.00000,31.00000;121.00010,31.00010"
        route_b = "121.10000,31.10000;121.10010,31.10010"
        route_c = "121.20000,31.20000;121.20010,31.20010"
        durations_a = (1200, 1320, 1440)
        durations_b = (3600, 3720, 3840)
        for index, day in enumerate(days):
            paths = [route(route_a, durations_a[index]), route(route_b, durations_b[index])]
            if index == 0:
                paths.append(route(route_c, 6000))
            if index % 2:
                paths.reverse()
            route_history.record_snapshot("O", "D", "test", paths, observed_at=observed_at(day))
        result = route_history.estimate("O", "D", "test", observed_at(date.today()))
        by_id = {item["route_id"]: item for item in result["routes"]}
        self.assertEqual(by_id[route_history.route_identity(route(route_a, 1200))]["duration_minutes"], 22)
        self.assertEqual(by_id[route_history.route_identity(route(route_b, 3600))]["duration_minutes"], 62)
        route_days = {item["route_id"]: item["sample_days"] for item in result["route_days"]}
        self.assertEqual(route_days[route_history.route_identity(route(route_c, 6000))], 1)
        self.assertNotIn(route_history.route_identity(route(route_c, 6000)), by_id)

    def test_weather_failure_keeps_route_data(self) -> None:
        route_data = {"route": {"paths": [route("121,31;121.001,31.001", 1200)]}}
        with patch.dict(os.environ, {}, clear=True):
            brief = commute_assistant.make_brief(
                test_config(), None, route_data, date.today().isoformat(), "08:00",
                {"weather": "天气暂不可用。"},
            )
        self.assertIn("天气数据：不可用", brief)
        self.assertIn("20 分钟", brief)
        self.assertIn("10.0 公里", brief)

    def test_route_failure_keeps_weather_data(self) -> None:
        today = date.today().isoformat()
        forecast = {
            "hourly": {
                "time": [today + "T08:00", today + "T18:00"],
                "temperature_2m": [18, 16],
                "precipitation_probability": [10, 20],
                "weather_code": [1, 61],
            }
        }
        with patch.dict(os.environ, {}, clear=True):
            brief = commute_assistant.make_brief(
                test_config(), forecast, None, today, "08:00",
                {"routes": "路线暂不可用。"},
            )
        self.assertIn("路线与路况数据：不可用", brief)
        self.assertIn("最高降水概率 10.0%", brief)
        self.assertIn("最高降水概率 20.0%", brief)

    def test_model_failure_uses_local_fallback_and_keeps_route(self) -> None:
        class BrokenClient:
            def __init__(self, **kwargs: object) -> None:
                self.responses = self

            def create(self, **kwargs: object) -> object:
                raise RuntimeError("simulated model failure")

        route_data = {"route": {"paths": [route("121,31;121.001,31.001", 1200)]}}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-offline-key"}), \
                patch.object(commute_assistant, "OpenAI", BrokenClient):
            brief = commute_assistant.make_brief(
                test_config(), None, route_data, date.today().isoformat(), "08:00"
            )
        self.assertIn("以下内容由本地规则", brief)
        self.assertIn("20 分钟", brief)

    def test_no_route_message_and_facility_failure_message(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "没有返回可用路线.*附近的地标"):
            commute_web.render_routes({"route": {"paths": []}}, "O", "D", "32")
        form = commute_web.search_form("测试起点", "测试终点")
        self.assertIn('name="origin"', form)
        self.assertIn('name="destination"', form)

        page = commute_web.render_facilities(
            [], "parking", "O", "D", "32", "", "", "shanghai", "",
            "121,31", "121,31", "121,31", "停车场", [], [], [], [], "destination",
            error_message="simulated POI failure",
        )
        self.assertIn("暂不可用", page)
        self.assertNotIn("没有找到相关地点", page)
        self.assertIn("稍后重试", page)

    def test_both_services_fail_create_explicit_report_and_nonzero_status(self) -> None:
        config = test_config()
        with tempfile.TemporaryDirectory() as report_dir, \
                patch.object(commute_assistant, "load_config", return_value=config), \
                patch.object(commute_assistant, "fetch_forecast", side_effect=RuntimeError("simulated weather failure")), \
                patch.object(commute_assistant, "fetch_driving_routes", side_effect=RuntimeError("simulated route failure")), \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(commute_assistant, "REPORT_DIR", Path(report_dir)):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                exit_code = commute_assistant.main()
            reports = list(Path(report_dir).glob("*.md"))
            self.assertEqual(exit_code, 1)
            self.assertEqual(len(reports), 1)
            content = reports[0].read_text(encoding="utf-8")
            self.assertIn("天气暂不可用", content)
            self.assertIn("驾车路线和查询时路况暂不可用", content)
            self.assertIn("本次天气和路线服务均未提供可用数据", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
