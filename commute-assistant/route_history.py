"""Store local map-estimate snapshots and summarize similar historical queries."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Iterator


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "commute_history.sqlite3"
MIN_ROUTE_DAYS = 3


def route_identity(path: dict) -> str | None:
    """Build a strict identity from the returned route geometry, not its list position."""
    polyline = path.get("polyline")
    if not isinstance(polyline, str) or not polyline.strip():
        return None
    points = []
    try:
        for item in polyline.split(";"):
            longitude, latitude = item.split(",", 1)
            point = (round(float(longitude), 5), round(float(latitude), 5))
            if not points or point != points[-1]:
                points.append(point)
    except (TypeError, ValueError):
        return None
    if len(points) < 2:
        return None
    canonical = ";".join(f"{longitude:.5f},{latitude:.5f}" for longitude, latitude in points)
    digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
    return f"geo-v1:{digest}"


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS route_snapshots (
            id INTEGER PRIMARY KEY,
            observed_at TEXT NOT NULL,
            weekday INTEGER NOT NULL,
            minute_of_day INTEGER NOT NULL,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            strategy TEXT NOT NULL,
            routes_json TEXT NOT NULL,
            via_points TEXT NOT NULL DEFAULT '[]'
        )"""
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(route_snapshots)")}
    if "via_points" not in columns:
        # Keep existing route observations as no-stop trips.
        connection.execute("ALTER TABLE route_snapshots ADD COLUMN via_points TEXT NOT NULL DEFAULT '[]'")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_route_history_match "
        "ON route_snapshots(origin, destination, strategy, weekday, observed_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_route_history_via_match "
        "ON route_snapshots(origin, destination, strategy, weekday, via_points, observed_at)"
    )
    return connection


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    connection = _connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_snapshot(origin: str, destination: str, strategy: str, paths: list[dict], observed_at: datetime | None = None, via_points: list[str] | None = None) -> None:
    """Save map-returned route estimates locally; actual trip duration is not collected."""
    observed_at = observed_at or datetime.now().astimezone()
    routes = []
    for path in paths[:3]:
        cost = path.get("cost") or {}
        try:
            duration = float(cost.get("duration")) / 60
        except (TypeError, ValueError):
            duration = None
        try:
            distance = float(path.get("distance")) / 1000
        except (TypeError, ValueError):
            distance = None
        try:
            tolls = float(cost.get("tolls"))
        except (TypeError, ValueError):
            tolls = None
        congestion = {"畅通": 0, "缓行": 0, "拥堵": 0, "严重拥堵": 0, "未知": 0}
        roads = []
        for step in path.get("steps") or []:
            road = str(step.get("road_name") or "").strip()
            for segment in step.get("tmcs") or []:
                status = segment.get("tmc_status") or "未知"
                congestion[status if status in congestion else "未知"] += 1
                if road and status in ("缓行", "拥堵", "严重拥堵"):
                    roads.append(road)
        routes.append({
            "route_id": route_identity(path),
            "duration_minutes": duration,
            "distance_km": distance,
            "tolls_yuan": tolls,
            "congestion": congestion,
            "slow_roads": list(dict.fromkeys(roads))[:8],
        })
    if not routes:
        return
    with _database() as connection:
        connection.execute(
            "INSERT INTO route_snapshots "
            "(observed_at, weekday, minute_of_day, origin, destination, strategy, routes_json, via_points) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (observed_at.isoformat(), observed_at.weekday(), observed_at.hour * 60 + observed_at.minute,
             origin, destination, str(strategy), json.dumps(routes, ensure_ascii=False),
             json.dumps(via_points or [], ensure_ascii=False, separators=(",", ":"))),
        )


def estimate(origin: str, destination: str, strategy: str, departure_at: datetime, via_points: list[str] | None = None) -> dict:
    """Summarize historical map estimates for the same weekday and nearby time."""
    since = (datetime.now().date() - timedelta(days=90)).isoformat()
    with _database() as connection:
        rows = connection.execute(
            "SELECT observed_at, minute_of_day, routes_json FROM route_snapshots "
            "WHERE origin=? AND destination=? AND strategy=? AND weekday=? AND observed_at>=? AND via_points=?",
            (origin, destination, str(strategy), departure_at.weekday(), since,
             json.dumps(via_points or [], ensure_ascii=False, separators=(",", ":"))),
        ).fetchall()
    target_minute = departure_at.hour * 60 + departure_at.minute
    eligible = []
    for observed_at, minute_of_day, routes_json in rows:
        gap = abs(int(minute_of_day) - target_minute)
        gap = min(gap, 1440 - gap)
        if gap <= 90:
            eligible.append((observed_at[:10], json.loads(routes_json)))
    distinct_days = sorted({day for day, _ in eligible})
    grouped: dict[str, dict[str, list[dict]]] = {}
    unmatched_routes = 0
    for day, routes in eligible:
        for route in routes:
            route_id = route.get("route_id")
            if not route_id:
                unmatched_routes += 1
                continue
            grouped.setdefault(str(route_id), {}).setdefault(day, []).append(route)

    route_days = [
        {"route_id": route_id, "sample_days": len(days)}
        for route_id, days in grouped.items()
    ]
    route_days.sort(key=lambda item: (-item["sample_days"], item["route_id"]))
    qualifying = {
        route_id: days for route_id, days in grouped.items()
        if len(days) >= MIN_ROUTE_DAYS
    }
    if not qualifying:
        return {
            "enough_data": False,
            "sample_days": len(distinct_days),
            "needed_days": MIN_ROUTE_DAYS,
            "route_days": route_days,
            "unmatched_routes": unmatched_routes,
        }

    summaries = []
    for route_id, days in sorted(qualifying.items()):
        samples = [sample for day_samples in days.values() for sample in day_samples]
        daily_values: dict[str, dict[str, list[float]]] = {}
        for field in ("duration_minutes", "distance_km", "tolls_yuan"):
            daily_values[field] = {
                day: [float(sample[field]) for sample in day_samples if sample.get(field) is not None]
                for day, day_samples in days.items()
            }
        summary = {
            "route_id": route_id,
            "route_label": route_id[-8:],
            "sample_count": len(samples),
            "sample_days": len(days),
        }
        for field in ("duration_minutes", "distance_km", "tolls_yuan"):
            day_medians = [median(values) for values in daily_values[field].values() if values]
            summary[field] = median(day_medians) if day_medians else None
        congestion_labels = ("畅通", "缓行", "拥堵", "严重拥堵", "未知")
        daily_congestion = {
            label: [
                median([sample.get("congestion", {}).get(label, 0) for sample in day_samples])
                for day_samples in days.values()
            ]
            for label in congestion_labels
        }
        summary["congestion"] = {
            label: round(median(values))
            for label, values in daily_congestion.items()
        }
        summary["slow_roads"] = list(dict.fromkeys(
            road for sample in samples for road in sample.get("slow_roads", [])
        ))[:8]
        summaries.append(summary)
    return {
        "enough_data": bool(summaries),
        "sample_days": len(distinct_days),
        "routes": summaries,
        "route_days": route_days,
        "unmatched_routes": unmatched_routes,
    }
