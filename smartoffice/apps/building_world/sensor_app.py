"""Agent-facing read-only tools for Building World sensor observations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from smartoffice.apps.app import App
from smartoffice.apps.building_world.sensor_hub import SensorHub
from smartoffice.physics.building.sensor_api import SensorQuantity, SensorReadRequest
from smartoffice.tool_utils import OperationType, app_tool, data_tool
from smartoffice.types import event_registered
from smartoffice.utils.type_utils import type_check


class BuildingSensorApp(App):
    """Expose canonical observations without leaking simulator truth."""

    def __init__(self, sensor_hub: SensorHub) -> None:
        super().__init__(name="BuildingSensorApp")
        self.sensor_hub = sensor_hub

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def read_zone_sensors(self, zone_id: str) -> dict[str, Any]:
        """Return all currently available readings for one building zone."""

        if zone_id not in self.sensor_hub.zone_ids:
            return {"error": f"unknown zone_id {zone_id!r}"}
        now = self._current_datetime()
        readings = self.sensor_hub.read(
            SensorReadRequest(
                at_time=now,
                zone_ids=(zone_id,),
                quantities=tuple(SensorQuantity),
            )
        )
        return {
            "at_time": now.isoformat(),
            "zone_id": zone_id,
            "readings": [_reading_to_dict(reading) for reading in readings],
        }

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def read_all_sensors(self) -> dict[str, Any]:
        """Return the latest available reading for every known zone/quantity."""

        now = self._current_datetime()
        return {
            "at_time": now.isoformat(),
            "readings": [
                _reading_to_dict(reading)
                for reading in self.sensor_hub.read_all(now)
            ],
        }

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_sensor_health(
        self, stale_after_seconds: Annotated[int, {"minimum": 0}] = 300
    ) -> dict[str, Any]:
        """Report quality and freshness without exposing hidden physics state."""

        if stale_after_seconds < 0:
            return {"error": "stale_after_seconds cannot be negative"}
        now = self._current_datetime()
        rows = self.sensor_hub.health(now, float(stale_after_seconds))
        return {
            "at_time": now.isoformat(),
            "sensors": rows,
            "summary": {
                status: sum(row["status"] == status for row in rows)
                for status in ("healthy", "uncertain", "stale", "bad")
            },
        }

    def _current_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.time_manager.time(), tz=timezone.utc)


def _reading_to_dict(reading) -> dict[str, Any]:
    """Serialize the public reading fields; simulator truth is unavailable here."""

    return {
        "sensor_id": reading.sensor_id,
        "zone_id": reading.zone_id,
        "quantity": reading.quantity.value,
        "value": reading.value,
        "unit": reading.unit,
        "observed_at": reading.observed_at.isoformat(),
        "available_at": (
            reading.available_at.isoformat()
            if reading.available_at is not None
            else None
        ),
        "quality": reading.quality.value,
        "metadata": dict(reading.metadata),
    }
