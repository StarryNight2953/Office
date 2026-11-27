"""Load a declarative room file into business, physics and sensor models."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from smartoffice.apps.building_world.building_world_app import BuildingWorldApp
from smartoffice.apps.building_world.types import (
    DeviceSpec,
    DeviceState,
    DeviceType,
    FunctionalAreaSpec,
    RoomSpec,
    ZoneSpec,
)
from smartoffice.physics.building.models import ZoneParameters, ZoneState
from smartoffice.physics.building.observation_model import SensorSpec
from smartoffice.physics.building.sensor_api import SensorQuality, SensorQuantity


@dataclass(frozen=True)
class LoadedRoomConfiguration:
    """Validated objects needed to assemble one room's simulation runtime."""

    room: RoomSpec
    zones: tuple[ZoneSpec, ...]
    functional_areas: tuple[FunctionalAreaSpec, ...]
    zone_parameters: Mapping[str, ZoneParameters]
    initial_zone_states: Mapping[str, ZoneState]
    devices: tuple[DeviceSpec, ...]
    device_states: Mapping[str, DeviceState]
    sensors: tuple[SensorSpec, ...]

    def install_into(self, world: BuildingWorldApp) -> None:
        """Install canonical business state without starting any physics."""

        world.add_room(self.room)
        for zone in self.zones:
            world.add_zone(zone)
        for area in self.functional_areas:
            world.add_functional_area(area)
        for device in self.devices:
            # Configurations are reusable fixtures; World receives its own
            # mutable command state rather than mutating the loader result.
            world.add_device(device, replace(self.device_states[device.device_id]))


def load_room_configuration(path: str | Path) -> LoadedRoomConfiguration:
    """Load JSON-syntax YAML using only the Python standard library.

    JSON is a valid YAML 1.2 subset.  Keeping the first deployment files in
    this subset avoids making simulation startup depend on optional packages.
    """

    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{config_path} must use JSON-compatible YAML syntax: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ValueError("room configuration root must be an object")
    return _parse_room_configuration(raw)


def _parse_room_configuration(raw: Mapping[str, Any]) -> LoadedRoomConfiguration:
    if raw.get("schema_version") != 1:
        raise ValueError("unsupported room schema_version")
    room_raw = _mapping(raw, "room")
    room = RoomSpec(
        room_id=_required_string(room_raw, "room_id"),
        name=_required_string(room_raw, "name"),
        capacity=_positive_int(room_raw, "capacity"),
        capabilities=frozenset(_string_list(room_raw, "capabilities")),
        room_type=str(room_raw.get("room_type", "general")),
        bookable=_boolean(room_raw, "bookable", default=True),
    )

    zone_rows = _object_list(raw, "zones", require_nonempty=True)
    zones: list[ZoneSpec] = []
    zone_parameters: dict[str, ZoneParameters] = {}
    initial_states: dict[str, ZoneState] = {}
    for row in zone_rows:
        zone_id = _required_string(row, "zone_id")
        if zone_id in zone_parameters:
            raise ValueError(f"duplicate zone_id {zone_id!r}")
        area_m2 = _positive_float(row, "area_m2")
        height_m = _positive_float(row, "height_m")
        zones.append(
            ZoneSpec(
                zone_id=zone_id,
                room_id=room.room_id,
                name=_required_string(row, "name"),
                purpose=str(row.get("purpose", "general")),
                area_m2=area_m2,
                height_m=height_m,
            )
        )
        physics_values = dict(_mapping(row, "physics"))
        physics_values["volume_m3"] = area_m2 * height_m
        try:
            zone_parameters[zone_id] = ZoneParameters(**physics_values)
            initial_states[zone_id] = ZoneState(
                zone_id=zone_id, **dict(_mapping(row, "initial_state"))
            )
        except TypeError as exc:
            raise ValueError(f"invalid zone {zone_id!r} fields: {exc}") from exc

    known_zones = set(zone_parameters)
    functional_areas: list[FunctionalAreaSpec] = []
    known_areas: set[str] = set()
    for row in _object_list(raw, "functional_areas"):
        area_id = _required_string(row, "area_id")
        if area_id in known_areas:
            raise ValueError(f"duplicate functional area {area_id!r}")
        zone_id = _required_string(row, "zone_id")
        if zone_id not in known_zones:
            raise ValueError(f"functional area {area_id!r} references unknown zone")
        capacity = row.get("capacity")
        if capacity is not None and (
            not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0
        ):
            raise ValueError("functional-area capacity must be a positive integer")
        functional_areas.append(
            FunctionalAreaSpec(
                area_id=area_id,
                room_id=room.room_id,
                name=_required_string(row, "name"),
                purpose=_required_string(row, "purpose"),
                zone_id=zone_id,
                capacity=capacity,
            )
        )
        known_areas.add(area_id)

    devices: list[DeviceSpec] = []
    device_states: dict[str, DeviceState] = {}
    for row in _object_list(raw, "devices"):
        device_id = _required_string(row, "device_id")
        if device_id in device_states:
            raise ValueError(f"duplicate device_id {device_id!r}")
        zone_id = _required_string(row, "zone_id")
        if zone_id not in known_zones:
            raise ValueError(f"device {device_id!r} references unknown zone")
        try:
            device_type = DeviceType(_required_string(row, "device_type"))
        except ValueError as exc:
            raise ValueError(f"unsupported device type for {device_id!r}") from exc
        rated_power_w = float(row.get("rated_power_w", 0.0))
        functional_area_id = row.get("functional_area_id")
        if functional_area_id is not None and functional_area_id not in known_areas:
            raise ValueError(f"device {device_id!r} references unknown functional area")
        devices.append(
            DeviceSpec(
                device_id=device_id,
                device_type=device_type,
                room_id=room.room_id,
                zone_id=zone_id,
                functional_area_id=functional_area_id,
                capabilities=frozenset(_string_list(row, "capabilities")),
                rated_power_w=rated_power_w,
                parameters=dict(row.get("parameters", {})),
            )
        )
        device_states[device_id] = DeviceState(device_id=device_id)

    sensors: list[SensorSpec] = []
    sensor_ids: set[str] = set()
    for row in _object_list(raw, "sensors"):
        sensor_id = _required_string(row, "sensor_id")
        if sensor_id in sensor_ids:
            raise ValueError(f"duplicate sensor_id {sensor_id!r}")
        sensor_ids.add(sensor_id)
        zone_id = _required_string(row, "zone_id")
        if zone_id not in known_zones:
            raise ValueError(f"sensor {sensor_id!r} references unknown zone")
        try:
            quantity = SensorQuantity(_required_string(row, "quantity"))
            quality = SensorQuality(str(row.get("quality", "good")))
        except ValueError as exc:
            raise ValueError(f"invalid sensor enum for {sensor_id!r}") from exc
        sensors.append(
            SensorSpec(
                sensor_id=sensor_id,
                zone_id=zone_id,
                quantity=quantity,
                sample_interval_seconds=float(row.get("sample_interval_seconds", 60.0)),
                latency_seconds=float(row.get("latency_seconds", 0.0)),
                noise_standard_deviation=float(
                    row.get("noise_standard_deviation", 0.0)
                ),
                bias=float(row.get("bias", 0.0)),
                missing_probability=float(row.get("missing_probability", 0.0)),
                quality=quality,
            )
        )

    return LoadedRoomConfiguration(
        room=room,
        zones=tuple(zones),
        functional_areas=tuple(functional_areas),
        zone_parameters=zone_parameters,
        initial_zone_states=initial_states,
        devices=tuple(devices),
        device_states=device_states,
        sensors=tuple(sensors),
    )


def _mapping(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _object_list(
    parent: Mapping[str, Any], key: str, *, require_nonempty: bool = False
) -> list[Mapping[str, Any]]:
    value = parent.get(key)
    if not isinstance(value, list) or (require_nonempty and not value):
        qualifier = "a non-empty " if require_nonempty else "an "
        raise ValueError(f"{key} must be {qualifier}array")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"every {key} item must be an object")
    return value


def _required_string(parent: Mapping[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _positive_int(parent: Mapping[str, Any], key: str) -> int:
    value = parent.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _positive_float(parent: Mapping[str, Any], key: str) -> float:
    try:
        value = float(parent[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a positive number") from exc
    if value <= 0.0:
        raise ValueError(f"{key} must be a positive number")
    return value


def _string_list(parent: Mapping[str, Any], key: str) -> list[str]:
    value = parent.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be an array of strings")
    return value


def _boolean(parent: Mapping[str, Any], key: str, *, default: bool) -> bool:
    value = parent.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value
