"""Domain-neutral data models for reduced-order building physics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class HvacMode(str, Enum):
    """High-level operating mode reported by the equipment layer."""

    OFF = "off"
    HEATING = "heating"
    COOLING = "cooling"
    FAN = "fan"


@dataclass(frozen=True)
class ZoneParameters:
    """Calibratable constants for one well-mixed thermal/air-quality zone.

    Unit suffixes are part of field names so configuration files and trace
    records remain self-describing.  The defaults are stable simulation
    starting points, not measured K1324 parameters.
    """

    # Geometry and first-order RC thermal parameters.
    volume_m3: float = 150.0
    thermal_capacitance_j_k: float = 3_000_000.0
    envelope_conductance_w_k: float = 90.0
    infiltration_air_changes_per_hour: float = 0.25

    # Solar heat gain = irradiance * aperture * gain coefficient.
    effective_solar_aperture_m2: float = 3.0
    solar_heat_gain_coefficient: float = 0.55

    # First-order pollutant removal and coarse cooling-coil condensation.
    pm25_deposition_rate_per_hour: float = 0.15
    cooling_moisture_removal_g_per_kwh: float = 600.0

    # COP converts delivered thermal power into estimated electric power.
    heating_cop: float = 3.0
    cooling_cop: float = 3.2

    # Numerical safety bounds; normal comfort limits are intentionally tighter.
    min_temperature_c: float = -30.0
    max_temperature_c: float = 60.0

    def __post_init__(self) -> None:
        if self.volume_m3 <= 0.0:
            raise ValueError("volume_m3 must be positive")
        if self.thermal_capacitance_j_k <= 0.0:
            raise ValueError("thermal_capacitance_j_k must be positive")
        if self.envelope_conductance_w_k < 0.0:
            raise ValueError("envelope_conductance_w_k cannot be negative")
        if self.infiltration_air_changes_per_hour < 0.0:
            raise ValueError("infiltration_air_changes_per_hour cannot be negative")
        if self.pm25_deposition_rate_per_hour < 0.0:
            raise ValueError("pm25_deposition_rate_per_hour cannot be negative")
        if self.cooling_moisture_removal_g_per_kwh < 0.0:
            raise ValueError("cooling moisture removal cannot be negative")
        if self.heating_cop <= 0.0 or self.cooling_cop <= 0.0:
            raise ValueError("HVAC COP values must be positive")


@dataclass(frozen=True)
class OutdoorConditions:
    """Outdoor boundary conditions shared by all zones for one time step."""

    air_temperature_c: float
    relative_humidity_pct: float = 50.0
    co2_ppm: float = 420.0
    pm25_ug_m3: float = 15.0
    solar_irradiance_w_m2: float = 0.0


@dataclass(frozen=True)
class InternalLoads:
    """Occupant and non-HVAC loads applied to one zone.

    ``equipment_heat_w`` is heat released into the room, whereas
    ``equipment_electric_power_w`` is metered electricity.  They are kept
    separate because not every consumed watt becomes an immediate zone load.
    """

    occupants: float = 0.0
    sensible_heat_w_per_person: float = 75.0
    moisture_g_s_per_person: float = 0.012
    co2_l_s_per_person: float = 0.0045
    pm25_ug_s: float = 0.0
    equipment_heat_w: float = 0.0
    equipment_electric_power_w: float = 0.0

    def __post_init__(self) -> None:
        if self.occupants < 0.0:
            raise ValueError("occupants cannot be negative")


@dataclass(frozen=True)
class HvacCommand:
    """Physical effects produced by HVAC and air-cleaning equipment.

    Device apps own user-facing setpoints and fan levels.  Their equipment
    model converts those states into this hardware-independent physics input.
    Airflow is in m3/s, moisture flow in g/s, and thermal/electric power in W.
    """

    mode: HvacMode = HvacMode.OFF
    heating_w: float = 0.0
    cooling_w: float = 0.0
    outdoor_airflow_m3_s: float = 0.0
    outdoor_air_sensible_recovery_fraction: float = 0.0
    outdoor_air_latent_recovery_fraction: float = 0.0
    supply_airflow_m3_s: float = 0.0
    supply_air_temperature_c: float | None = None
    supply_air_relative_humidity_pct: float | None = None
    humidification_g_s: float = 0.0
    dehumidification_g_s: float = 0.0
    clean_air_delivery_m3_s: float = 0.0
    auxiliary_electric_power_w: float = 0.0


@dataclass
class ZoneState:
    """Simulator truth.  Agent-facing code must use sensor observations."""

    zone_id: str
    air_temperature_c: float = 22.0
    relative_humidity_pct: float = 50.0
    co2_ppm: float = 500.0
    pm25_ug_m3: float = 10.0


@dataclass(frozen=True)
class ComfortResult:
    """Normalized comfort score and its independently auditable penalties.

    All values are in [0, 1]; score 1 is best and penalty 1 is most severe.
    """

    score: float
    temperature_penalty: float
    humidity_penalty: float
    co2_penalty: float
    pm25_penalty: float


@dataclass(frozen=True)
class ZoneStepResult:
    """Immutable record of one zone after a completed physics time step.

    ``energy_kwh`` is energy used during this step, not cumulative energy.
    The orchestrator owns accumulation across the complete scenario.
    """

    at_time: datetime
    zone_id: str
    state: ZoneState
    sensible_heat_balance_w: float
    electric_power_w: float
    energy_kwh: float
    comfort: ComfortResult
    tags: tuple[str, ...] = ()
    power_by_category_w: Mapping[str, float] = field(default_factory=dict)

    @property
    def air_temperature_c(self) -> float:
        """Convenience access that keeps call sites independent of nesting."""

        return self.state.air_temperature_c

    @property
    def relative_humidity_pct(self) -> float:
        return self.state.relative_humidity_pct

    @property
    def co2_ppm(self) -> float:
        return self.state.co2_ppm

    @property
    def pm25_ug_m3(self) -> float:
        return self.state.pm25_ug_m3
