"""Translate device command state into hardware-independent physics inputs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from smartoffice.apps.building_world.types import (
    DeviceHealth,
    DeviceSpec,
    DeviceState,
    DeviceType,
)
from smartoffice.physics.building.models import HvacCommand, HvacMode, ZoneState


class BuildingEquipmentModel:
    """Aggregate all active devices in a zone into one ``HvacCommand``."""

    def commands_by_zone(
        self,
        devices: Mapping[str, DeviceSpec],
        states: Mapping[str, DeviceState],
        zone_truth: Mapping[str, ZoneState],
    ) -> dict[str, HvacCommand]:
        values: dict[str, dict[str, float | HvacMode]] = defaultdict(
            lambda: {
                "mode": HvacMode.OFF,
                "heating_w": 0.0,
                "cooling_w": 0.0,
                "outdoor_airflow_m3_s": 0.0,
                "sensible_recovered_airflow_m3_s": 0.0,
                "latent_recovered_airflow_m3_s": 0.0,
                "humidification_g_s": 0.0,
                "clean_air_delivery_m3_s": 0.0,
                "auxiliary_electric_power_w": 0.0,
            }
        )
        for device_id, spec in devices.items():
            state = states[device_id]
            if not state.power_on or state.health != DeviceHealth.ONLINE:
                continue
            level_fraction = max(1, min(3, state.level)) / 3.0
            row = values[spec.zone_id]
            if spec.device_type == DeviceType.HVAC:
                mode = HvacMode(state.mode)
                row["mode"] = mode
                capacity = _number(spec, "thermal_capacity_w", 0.0)
                target = state.target_temperature_c
                truth = zone_truth[spec.zone_id].air_temperature_c
                demand = 1.0
                if target is not None:
                    delta = (
                        truth - target if mode == HvacMode.COOLING else target - truth
                    )
                    demand = max(0.0, min(1.0, delta / 3.0))
                delivered = capacity * level_fraction * demand
                if mode == HvacMode.COOLING:
                    row["cooling_w"] = float(row["cooling_w"]) + delivered
                elif mode == HvacMode.HEATING:
                    row["heating_w"] = float(row["heating_w"]) + delivered
                airflow = (
                    _number(spec, "outdoor_airflow_m3_s", 0.0) * level_fraction
                )
                _add_outdoor_air(row, spec, airflow)
                row["auxiliary_electric_power_w"] = (
                    float(row["auxiliary_electric_power_w"])
                    + _number(spec, "fan_power_w", spec.rated_power_w) * level_fraction
                )
            elif spec.device_type == DeviceType.HUMIDIFIER:
                row["humidification_g_s"] = (
                    float(row["humidification_g_s"])
                    + _number(spec, "humidification_g_s_max", 0.0) * level_fraction
                )
                row["auxiliary_electric_power_w"] = (
                    float(row["auxiliary_electric_power_w"])
                    + spec.rated_power_w * level_fraction
                )
            elif spec.device_type == DeviceType.AIR_PURIFIER:
                row["clean_air_delivery_m3_s"] = (
                    float(row["clean_air_delivery_m3_s"])
                    + _number(spec, "clean_air_delivery_m3_s_max", 0.0) * level_fraction
                )
                row["auxiliary_electric_power_w"] = (
                    float(row["auxiliary_electric_power_w"])
                    + spec.rated_power_w * level_fraction
                )
            elif spec.device_type == DeviceType.VENTILATION:
                # Dedicated ventilation supplies outdoor air independently of
                # HVAC thermal mode, allowing CO2 control to be tested directly.
                airflow = (
                    _number(spec, "outdoor_airflow_m3_s_max", 0.0) * level_fraction
                )
                _add_outdoor_air(row, spec, airflow)
                row["auxiliary_electric_power_w"] = (
                    float(row["auxiliary_electric_power_w"])
                    + spec.rated_power_w * level_fraction
                )

        commands = {}
        for zone_id, row in values.items():
            airflow = float(row.pop("outdoor_airflow_m3_s"))
            sensible_recovered = float(
                row.pop("sensible_recovered_airflow_m3_s")
            )
            latent_recovered = float(row.pop("latent_recovered_airflow_m3_s"))
            commands[zone_id] = HvacCommand(
                **row,  # type: ignore[arg-type]
                outdoor_airflow_m3_s=airflow,
                outdoor_air_sensible_recovery_fraction=(
                    sensible_recovered / airflow if airflow > 0.0 else 0.0
                ),
                outdoor_air_latent_recovery_fraction=(
                    latent_recovered / airflow if airflow > 0.0 else 0.0
                ),
            )
        return commands


def _number(spec: DeviceSpec, name: str, default: float) -> float:
    value = spec.parameters.get(name, default)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"device {spec.device_id!r} parameter {name!r} is invalid"
        ) from exc


def _add_outdoor_air(
    row: dict[str, float | HvacMode],
    spec: DeviceSpec,
    airflow_m3_s: float,
) -> None:
    """Accumulate airflow and its flow-weighted recovery effectiveness."""

    sensible = _number(spec, "sensible_heat_recovery_fraction", 0.0)
    latent = _number(spec, "latent_moisture_recovery_fraction", 0.0)
    if not 0.0 <= sensible <= 1.0 or not 0.0 <= latent <= 1.0:
        raise ValueError(f"device {spec.device_id!r} recovery fraction is invalid")
    row["outdoor_airflow_m3_s"] = (
        float(row["outdoor_airflow_m3_s"]) + airflow_m3_s
    )
    row["sensible_recovered_airflow_m3_s"] = (
        float(row["sensible_recovered_airflow_m3_s"]) + airflow_m3_s * sensible
    )
    row["latent_recovered_airflow_m3_s"] = (
        float(row["latent_recovered_airflow_m3_s"]) + airflow_m3_s * latent
    )
