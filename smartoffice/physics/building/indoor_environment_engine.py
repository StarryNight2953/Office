"""Coupled reduced-order truth model for well-mixed building zones."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from math import exp

from smartoffice.physics.building.models import (
    ComfortResult,
    HvacCommand,
    InternalLoads,
    OutdoorConditions,
    ZoneParameters,
    ZoneState,
    ZoneStepResult,
)


class IndoorEnvironmentEngine:
    """Advance temperature, moisture, CO2 and PM2.5 simulator truth.

    Sensor data is deliberately absent from this interface.  Observations are
    generated after the truth step by ``BuildingObservationModel``.
    """

    AIR_DENSITY_KG_M3 = 1.204
    AIR_HEAT_CAPACITY_J_KG_K = 1006.0

    def __init__(
        self,
        zone_parameters: Mapping[str, ZoneParameters],
        initial_states: Mapping[str, ZoneState] | None = None,
    ) -> None:
        if not zone_parameters:
            raise ValueError("at least one zone is required")
        self.zone_parameters = dict(zone_parameters)
        supplied = initial_states or {}
        unknown = set(supplied) - set(self.zone_parameters)
        if unknown:
            raise ValueError(f"initial states contain unknown zones: {sorted(unknown)}")
        self.states: dict[str, ZoneState] = {
            zone_id: replace(supplied.get(zone_id, ZoneState(zone_id=zone_id)))
            for zone_id in self.zone_parameters
        }

    def step(
        self,
        at_time: datetime,
        timestep_seconds: float,
        outdoor: OutdoorConditions,
        loads_by_zone: Mapping[str, InternalLoads] | None = None,
        hvac_by_zone: Mapping[str, HvacCommand] | None = None,
    ) -> list[ZoneStepResult]:
        """Advance every configured zone by the same deterministic time step.

        Zone iteration follows configuration insertion order, which also keeps
        result and replay traces stable.  Unknown input IDs are rejected rather
        than silently ignored because a misspelled zone would invalidate an
        experiment without an obvious failure.
        """

        if timestep_seconds <= 0.0:
            raise ValueError("timestep_seconds must be positive")
        loads_by_zone = loads_by_zone or {}
        hvac_by_zone = hvac_by_zone or {}
        self._validate_zone_keys(loads_by_zone, "loads")
        self._validate_zone_keys(hvac_by_zone, "HVAC commands")

        return [
            self._advance_zone(
                at_time,
                timestep_seconds,
                outdoor,
                self.states[zone_id],
                self.zone_parameters[zone_id],
                loads_by_zone.get(zone_id, InternalLoads()),
                hvac_by_zone.get(zone_id, HvacCommand()),
            )
            for zone_id in self.states
        ]

    def get_state(self) -> dict[str, ZoneState]:
        """Return defensive copies so callers cannot mutate hidden truth."""

        return {zone_id: replace(state) for zone_id, state in self.states.items()}

    def load_state(self, states: Mapping[str, ZoneState]) -> None:
        """Restore truth while preserving the configured static zone set."""

        if set(states) != set(self.zone_parameters):
            raise ValueError("restored zone IDs must exactly match configured zone IDs")
        self.states = {zone_id: replace(state) for zone_id, state in states.items()}

    def _validate_zone_keys(self, values: Mapping[str, object], label: str) -> None:
        unknown = set(values) - set(self.states)
        if unknown:
            raise ValueError(f"{label} contain unknown zones: {sorted(unknown)}")

    def _advance_zone(
        self,
        at_time: datetime,
        dt: float,
        outdoor: OutdoorConditions,
        state: ZoneState,
        params: ZoneParameters,
        loads: InternalLoads,
        hvac: HvacCommand,
    ) -> ZoneStepResult:
        """Apply the coupled heat, moisture and contaminant balances."""

        # Relative humidity is temperature-dependent, so preserve absolute
        # vapor density before changing temperature and convert back afterward.
        indoor_vapor_g_m3 = _vapor_density_g_m3(
            state.air_temperature_c, state.relative_humidity_pct
        )

        # Convert air changes/hour into a volumetric flow.  Mechanical outdoor
        # air is added to unavoidable envelope infiltration.
        infiltration_m3_s = (
            params.infiltration_air_changes_per_hour * params.volume_m3 / 3600.0
        )
        supply_temp = (
            outdoor.air_temperature_c
            if hvac.supply_air_temperature_c is None
            else hvac.supply_air_temperature_c
        )
        mechanical_outdoor_air_m3_s = max(0.0, hvac.outdoor_airflow_m3_s)
        total_outdoor_air_m3_s = infiltration_m3_s + mechanical_outdoor_air_m3_s
        sensible_recovery = _clip(
            hvac.outdoor_air_sensible_recovery_fraction, 0.0, 1.0
        )
        latent_recovery = _clip(
            hvac.outdoor_air_latent_recovery_fraction, 0.0, 1.0
        )

        # First-order sensible heat balance:
        #   C * dT/dt = envelope + airflow + solar + internal + HVAC.
        q_envelope = params.envelope_conductance_w_k * (
            outdoor.air_temperature_c - state.air_temperature_c
        )
        q_infiltration = (
            self.AIR_DENSITY_KG_M3
            * self.AIR_HEAT_CAPACITY_J_KG_K
            * (
                infiltration_m3_s
                + mechanical_outdoor_air_m3_s * (1.0 - sensible_recovery)
            )
            * (outdoor.air_temperature_c - state.air_temperature_c)
        )
        q_supply = (
            self.AIR_DENSITY_KG_M3
            * self.AIR_HEAT_CAPACITY_J_KG_K
            * max(0.0, hvac.supply_airflow_m3_s)
            * (supply_temp - state.air_temperature_c)
        )
        q_solar = (
            outdoor.solar_irradiance_w_m2
            * params.effective_solar_aperture_m2
            * params.solar_heat_gain_coefficient
        )
        heating_w = max(0.0, hvac.heating_w)
        cooling_w = max(0.0, hvac.cooling_w)
        heat_balance_w = (
            q_envelope
            + q_infiltration
            + q_supply
            + q_solar
            + loads.occupants * loads.sensible_heat_w_per_person
            + loads.equipment_heat_w
            + heating_w
            - cooling_w
        )
        state.air_temperature_c = _clip(
            state.air_temperature_c
            + heat_balance_w * dt / params.thermal_capacitance_j_k,
            params.min_temperature_c,
            params.max_temperature_c,
        )

        # Moisture sources/sinks are expressed as grams of water per second.
        # The cooling term is an intentionally coarse condensate proxy that can
        # later be replaced by a coil/psychrometric equipment model.
        outdoor_vapor_g_m3 = _vapor_density_g_m3(
            outdoor.air_temperature_c, outdoor.relative_humidity_pct
        )
        supply_rh = (
            outdoor.relative_humidity_pct
            if hvac.supply_air_relative_humidity_pct is None
            else hvac.supply_air_relative_humidity_pct
        )
        supply_vapor_g_m3 = _vapor_density_g_m3(supply_temp, supply_rh)
        automatic_cooling_dehumidification_g_s = (
            cooling_w
            * params.cooling_moisture_removal_g_per_kwh
            / 3_600_000.0
        )
        moisture_rate_g_s = (
            (
                infiltration_m3_s
                + mechanical_outdoor_air_m3_s * (1.0 - latent_recovery)
            )
            * (outdoor_vapor_g_m3 - indoor_vapor_g_m3)
            + max(0.0, hvac.supply_airflow_m3_s)
            * (supply_vapor_g_m3 - indoor_vapor_g_m3)
            + loads.occupants * loads.moisture_g_s_per_person
            + max(0.0, hvac.humidification_g_s)
            - max(0.0, hvac.dehumidification_g_s)
            - automatic_cooling_dehumidification_g_s
        )
        indoor_vapor_g_m3 = max(
            0.0, indoor_vapor_g_m3 + moisture_rate_g_s * dt / params.volume_m3
        )
        state.relative_humidity_pct = _clip(
            100.0
            * indoor_vapor_g_m3
            / max(_saturation_vapor_density_g_m3(state.air_temperature_c), 1e-9),
            0.0,
            100.0,
        )

        # CO2 uses a well-mixed mass balance.  Person emissions are provided in
        # L/s, converted to m3/s, then to a zone concentration source in ppm/s.
        co2_source_m3_s = loads.occupants * loads.co2_l_s_per_person / 1000.0
        state.co2_ppm = _well_mixed_concentration_step(
            current=state.co2_ppm,
            outdoor=outdoor.co2_ppm,
            source_per_second=co2_source_m3_s * 1_000_000.0 / params.volume_m3,
            removal_rate_per_second=total_outdoor_air_m3_s / params.volume_m3,
            dt=dt,
        )

        # PM2.5 has three removal paths: outdoor-air dilution, purifier CADR,
        # and passive deposition.  Only outdoor-air exchange imports outdoor PM.
        air_cleaning_rate_s = max(0.0, hvac.clean_air_delivery_m3_s) / params.volume_m3
        pm_deposition_rate_s = params.pm25_deposition_rate_per_hour / 3600.0
        state.pm25_ug_m3 = _well_mixed_concentration_step(
            current=state.pm25_ug_m3,
            outdoor=outdoor.pm25_ug_m3,
            source_per_second=loads.pm25_ug_s / params.volume_m3,
            removal_rate_per_second=(
                total_outdoor_air_m3_s / params.volume_m3
                + air_cleaning_rate_s
                + pm_deposition_rate_s
            ),
            dt=dt,
            outdoor_exchange_rate_per_second=total_outdoor_air_m3_s
            / params.volume_m3,
        )

        # Delivered thermal power is converted through COP.  Fan, humidifier,
        # purifier and similar loads are supplied as auxiliary electric power.
        hvac_power_w = heating_w / params.heating_cop + cooling_w / params.cooling_cop
        electric_power_w = (
            hvac_power_w
            + max(0.0, hvac.auxiliary_electric_power_w)
            + max(0.0, loads.equipment_electric_power_w)
        )
        energy_kwh = electric_power_w * dt / 3_600_000.0
        comfort = _comfort(state)
        tags = _tags(state, comfort)
        return ZoneStepResult(
            at_time=at_time,
            zone_id=state.zone_id,
            state=replace(state),
            sensible_heat_balance_w=heat_balance_w,
            electric_power_w=electric_power_w,
            energy_kwh=energy_kwh,
            comfort=comfort,
            tags=tags,
            power_by_category_w={
                "hvac": hvac_power_w,
                "auxiliary": max(0.0, hvac.auxiliary_electric_power_w),
                "equipment": max(0.0, loads.equipment_electric_power_w),
            },
        )


def _well_mixed_concentration_step(
    *,
    current: float,
    outdoor: float,
    source_per_second: float,
    removal_rate_per_second: float,
    dt: float,
    outdoor_exchange_rate_per_second: float | None = None,
) -> float:
    """Analytic step for ``dC/dt = forcing - removal * C``.

    The exponential solution is stable for larger 1-10 minute simulation
    steps, unlike an unconstrained explicit Euler update.  PM2.5 passes a
    separate outdoor exchange rate because deposition and cleaning remove
    particles without introducing outdoor concentration.
    """

    removal = max(0.0, removal_rate_per_second)
    outdoor_exchange = (
        removal
        if outdoor_exchange_rate_per_second is None
        else max(0.0, outdoor_exchange_rate_per_second)
    )
    forcing = max(0.0, source_per_second) + outdoor_exchange * max(0.0, outdoor)
    if removal <= 0.0:
        return max(0.0, current + forcing * dt)
    equilibrium = forcing / removal
    return max(0.0, equilibrium + (current - equilibrium) * exp(-removal * dt))


def _comfort(state: ZoneState) -> ComfortResult:
    """Compute a transparent heuristic comfort score for ARE evaluation."""

    # Comfort weights are deliberately explicit so BOS sensitivity analysis
    # can later replace or re-weight this policy without changing physics.
    temperature_penalty = _range_penalty(state.air_temperature_c, 21.0, 25.0, 5.0)
    humidity_penalty = _range_penalty(state.relative_humidity_pct, 40.0, 60.0, 30.0)
    co2_penalty = _upper_penalty(state.co2_ppm, 800.0, 1200.0)
    pm25_penalty = _upper_penalty(state.pm25_ug_m3, 15.0, 60.0)
    total = (
        0.45 * temperature_penalty
        + 0.20 * humidity_penalty
        + 0.20 * co2_penalty
        + 0.15 * pm25_penalty
    )
    return ComfortResult(
        score=_clip(1.0 - total, 0.0, 1.0),
        temperature_penalty=temperature_penalty,
        humidity_penalty=humidity_penalty,
        co2_penalty=co2_penalty,
        pm25_penalty=pm25_penalty,
    )


def _tags(state: ZoneState, comfort: ComfortResult) -> tuple[str, ...]:
    """Attach descriptive state labels; event transition logic lives upstream."""

    tags: list[str] = []
    if state.co2_ppm >= 1000.0:
        tags.append("co2_elevated")
    if state.pm25_ug_m3 >= 35.0:
        tags.append("pm25_elevated")
    if not 30.0 <= state.relative_humidity_pct <= 70.0:
        tags.append("humidity_out_of_range")
    if comfort.score < 0.7:
        tags.append("comfort_low")
    return tuple(tags)


def _range_penalty(value: float, low: float, high: float, scale: float) -> float:
    """Return zero inside a comfort band and linear penalty outside it."""

    if low <= value <= high:
        return 0.0
    distance = low - value if value < low else value - high
    return _clip(distance / scale, 0.0, 1.0)


def _upper_penalty(value: float, comfortable: float, severe: float) -> float:
    """Return a clipped linear penalty for upper-bounded quantities."""

    return _clip((value - comfortable) / (severe - comfortable), 0.0, 1.0)


def _saturation_vapor_density_g_m3(temperature_c: float) -> float:
    """Approximate saturated water-vapor density using the Magnus equation."""

    saturation_pressure_pa = 610.78 * exp(
        17.2694 * temperature_c / (temperature_c + 237.29)
    )
    return 216.7 * saturation_pressure_pa / (temperature_c + 273.15) / 100.0


def _vapor_density_g_m3(temperature_c: float, relative_humidity_pct: float) -> float:
    return _saturation_vapor_density_g_m3(temperature_c) * _clip(
        relative_humidity_pct, 0.0, 100.0
    ) / 100.0


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
