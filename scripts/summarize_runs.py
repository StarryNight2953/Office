from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

FIELDS = [
    "run_dir",
    "run_type",
    "scenario_id",
    "controller_id",
    "system_prompt_mode",
    "model",
    "status",
    "stopped_reason",
    "success",
    "wall_time_seconds",
    "workflow_steps",
    "tool_call_count",
    "total_tokens",
    "cached_tokens",
    "building_energy_kwh",
    "comfort_violation_zone_minutes",
    "occupied_comfort_violation_zone_minutes",
    "air_quality_violation_zone_minutes",
    "unoccupied_energy_kwh",
    "building_agent_wake_count",
    "building_active_device_count",
    "building_active_reservation_count",
]


def nested(data: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = data
    for key in path.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def row_from_report(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    building_zones = nested(report, "outcome.building.environment.by_zone", [])
    comfort_seconds = _sum_zone_values(
        building_zones, "comfort_violation_seconds"
    )
    occupied_comfort_seconds = _sum_zone_values(
        building_zones, "occupied_comfort_violation_seconds"
    )
    air_quality_seconds = _sum_zone_values(
        building_zones, "air_quality_violation_seconds"
    )
    return {
        "run_dir": str(path.parent),
        "run_type": report.get("run_type"),
        "scenario_id": report.get("scenario_id"),
        "controller_id": report.get("controller_id"),
        "system_prompt_mode": report.get("system_prompt_mode"),
        "model": report.get("model"),
        "status": report.get("status"),
        "stopped_reason": report.get("stopped_reason"),
        "success": nested(report, "evaluation.validation.success"),
        "wall_time_seconds": nested(report, "metrics.wall_time_seconds"),
        "workflow_steps": nested(report, "metrics.workflow_steps"),
        "tool_call_count": nested(report, "metrics.tool_call_count"),
        "total_tokens": nested(report, "metrics.token_usage.total_tokens"),
        "cached_tokens": nested(report, "metrics.token_usage.cached_tokens"),
        "building_energy_kwh": nested(
            report, "outcome.building.energy.total_kwh"
        ),
        "comfort_violation_zone_minutes": (
            comfort_seconds / 60.0 if comfort_seconds is not None else None
        ),
        "occupied_comfort_violation_zone_minutes": (
            occupied_comfort_seconds / 60.0
            if occupied_comfort_seconds is not None
            else None
        ),
        "air_quality_violation_zone_minutes": (
            air_quality_seconds / 60.0
            if air_quality_seconds is not None
            else None
        ),
        "unoccupied_energy_kwh": _sum_zone_values(
            building_zones, "unoccupied_energy_kwh"
        ),
        "building_agent_wake_count": nested(
            report, "outcome.building.events.agent_wake_count"
        ),
        "building_active_device_count": len(
            nested(report, "outcome.building.resources.active_device_ids", [])
        ),
        "building_active_reservation_count": len(
            nested(report, "outcome.building.resources.active_reservation_ids", [])
        ),
    }


def _sum_zone_values(zones: Any, key: str) -> float | None:
    if not isinstance(zones, list):
        return None
    matching = [zone for zone in zones if isinstance(zone, dict) and key in zone]
    if not matching:
        return None
    return sum(
        float(zone.get(key, 0.0))
        for zone in matching
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize smart-building run_report.json files")
    parser.add_argument("runs_dir", nargs="?", default="runs")
    parser.add_argument("--csv", help="Optional CSV output path")
    args = parser.parse_args()

    rows = [
        row_from_report(path)
        for path in sorted(Path(args.runs_dir).glob("**/*.run_report.json"))
    ]
    if args.csv:
        output = Path(args.csv)
        output.parent.mkdir(parents=True, exist_ok=True)
        with open(output, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    print("\t".join(FIELDS))
    for row in rows:
        print("\t".join("" if row.get(field) is None else str(row.get(field)) for field in FIELDS))


if __name__ == "__main__":
    main()
