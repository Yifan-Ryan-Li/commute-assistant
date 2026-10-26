"""Store local route observations and estimate future conditions from similar trips."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Iterator


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "commute_history.sqlite3"


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
    """Save a compact observation; coordinates and route metrics stay on this computer."""
    observed_at = observed_at or datetime.now().astimezone()
    routes = []
    for index, path in enumerate(paths[:3], start=1):
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
            "index": index,
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
    """Return median route metrics from the same weekday and nearby time in the last 90 days."""
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
    if len(distinct_days) < 3:
        return {"enough_data": False, "sample_days": len(distinct_days), "needed_days": 3}

    grouped: dict[int, list[dict]] = {}
    for _, routes in eligible:
        for route in routes:
            grouped.setdefault(int(route["index"]), []).append(route)
    summaries = []
    for index, samples in sorted(grouped.items()):
        summary = {"index": index, "sample_count": len(samples)}
        for field in ("duration_minutes", "distance_km", "tolls_yuan"):
            values = [sample[field] for sample in samples if sample.get(field) is not None]
            summary[field] = median(values) if values else None
        summary["congestion"] = {
            label: round(median([sample["congestion"].get(label, 0) for sample in samples]))
            for label in ("畅通", "缓行", "拥堵", "严重拥堵", "未知")
        }
        summary["slow_roads"] = list(dict.fromkeys(
            road for sample in samples for road in sample.get("slow_roads", [])
        ))[:8]
        summaries.append(summary)
    return {"enough_data": bool(summaries), "sample_days": len(distinct_days), "routes": summaries}
