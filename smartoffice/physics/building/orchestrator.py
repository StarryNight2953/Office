"""Single deterministic integration point for building physics and sensing."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping

from smartoffice.physics.building.indoor_environment_engine import IndoorEnvironmentEngine
from smartoffice.physics.building.models import (
    HvacCommand,
    InternalLoads,
    OutdoorConditions,
    ZoneState,
    ZoneStepResult,
)
from smartoffice.physics.building.observation_model import (
    BuildingObservationModel,
    ObservationSample,
)
from smartoffice.physics.building.sensor_api import SensorPublisher


# Public early-warning thresholds shared with the Agent-facing operating
# contract. Scenario hard limits can be looser, but warnings still require
# preventive control while a room is occupied.
CO2_WARNING_THRESHOLD_PPM = 1000.0
PM25_WARNING_THRESHOLD_UG_M3 = 35.0


class PhysicsEventType(str, Enum):
    """Physics-originated transitions consumed by the future event runtime."""

    COMFORT_THRESHOLD_VIOLATED = "comfort_threshold_violated"
    AIR_QUALITY_THRESHOLD_VIOLATED = "air_quality_threshold_violated"
    THRESHOLD_RECOVERED = "threshold_recovered"


@dataclass(frozen=True)
class PhysicsEvent:
    """Small domain event emitted only when a threshold condition changes."""

    event_type: PhysicsEventType
    occurred_at: datetime
    zone_id: str
    condition: str
    payload: Mapping[str, float | str]


@dataclass(frozen=True)
class BuildingPhysicsStepResult:
    """Complete auditable output of one orchestrated building time step."""

    at_time: datetime
    zones: tuple[ZoneStepResult, ...]
    observations: tuple[ObservationSample, ...]
    events: tuple[PhysicsEvent, ...]
    cumulative_energy_kwh_by_zone: Mapping[str, float]


class BuildingPhysicsOrchestrator:
    """Coordinate truth, sensing, energy accounting and threshold transitions.

    This is the only integration point that should be called by
    ``BuildingWorldApp.advance_time``.  Keeping ordering here makes replay
    deterministic and prevents device apps from advancing individual models.
    """

    def __init__(
        self,
        engine: IndoorEnvironmentEngine,
        observation_model: BuildingObservationModel | None = None,
        observation_sink: SensorPublisher | None = None,
    ) -> None:
        self.engine = engine
        self.observation_model = observation_model
        # The sink is normally SensorHub.  It converts generated observations
        # into the same pull interface used by real MQTT/BACnet providers.
        self.observation_sink = observation_sink
        self.cumulative_energy_kwh_by_zone = {
            zone_id: 0.0 for zone_id in engine.states
        }
        # A set, rather than a repeated alarm counter, lets us emit only edge
        # transitions: normal -> violated and violated -> recovered.
        self._active_conditions: set[tuple[str, str]] = set()
        self.last_step_at: datetime | None = None

    def step(
        self,
        at_time: datetime,
        timestep_seconds: float,
        outdoor: OutdoorConditions,
        loads_by_zone: Mapping[str, InternalLoads] | None = None,
        hvac_by_zone: Mapping[str, HvacCommand] | None = None,
    ) -> BuildingPhysicsStepResult:
        """Advance truth, account energy, sample sensors, then derive events."""

        # Strict monotonicity catches accidental double ticks and out-of-order
        # event replay before either can corrupt cumulative energy.
        if self.last_step_at is not None and at_time <= self.last_step_at:
            raise ValueError("physics step time must be strictly increasing")

        # Ordering is part of the public contract: observations and threshold
        # checks must describe the newly advanced state, never the prior state.
        zone_results = self.engine.step(
            at_time=at_time,
            timestep_seconds=timestep_seconds,
            outdoor=outdoor,
            loads_by_zone=loads_by_zone,
            hvac_by_zone=hvac_by_zone,
        )
        for result in zone_results:
            self.cumulative_energy_kwh_by_zone[result.zone_id] += result.energy_kwh
        observations = (
            self.observation_model.sample(at_time, self.engine.get_state())
            if self.observation_model is not None
            else []
        )
        if self.observation_sink is not None:
            self.observation_sink.publish_many(
                sample.reading for sample in observations
            )
        events = self._threshold_events(at_time, zone_results)
        self.last_step_at = at_time
        return BuildingPhysicsStepResult(
            at_time=at_time,
            zones=tuple(zone_results),
            observations=tuple(observations),
            events=tuple(events),
            cumulative_energy_kwh_by_zone=dict(self.cumulative_energy_kwh_by_zone),
        )

    def sample_observations(
        self, at_time: datetime
    ) -> tuple[ObservationSample, ...]:
        """Sample the current truth without advancing physics or energy.

        This bootstrap path makes sensors useful at scenario time zero while
        preserving the invariant that only ``step`` evolves simulator truth.
        """

        if self.observation_model is None:
            return ()
        observations = tuple(
            self.observation_model.sample(at_time, self.engine.get_state())
        )
        if self.observation_sink is not None:
            self.observation_sink.publish_many(
                sample.reading for sample in observations
            )
        return observations

    def snapshot(self) -> dict[str, object]:
        """Capture all mutable state needed for bit-for-bit continuation."""

        return {
            "states": {
                zone_id: asdict(state)
                for zone_id, state in self.engine.get_state().items()
            },
            "cumulative_energy_kwh_by_zone": dict(self.cumulative_energy_kwh_by_zone),
            "active_conditions": sorted(self._active_conditions),
            "last_step_at": self.last_step_at,
            "observation_model": (
                self.observation_model.snapshot()
                if self.observation_model is not None
                else None
            ),
        }

    def restore(self, snapshot: Mapping[str, object]) -> None:
        """Restore truth, meters, threshold memory, clock and sensor RNG."""

        raw_states = snapshot["states"]
        states = {
            zone_id: ZoneState(**values)
            for zone_id, values in dict(raw_states).items()  # type: ignore[arg-type]
        }
        self.engine.load_state(states)
        self.cumulative_energy_kwh_by_zone = {
            str(zone_id): float(value)
            for zone_id, value in dict(
                snapshot["cumulative_energy_kwh_by_zone"]  # type: ignore[arg-type]
            ).items()
        }
        self._active_conditions = {
            (str(zone_id), str(condition))
            for zone_id, condition in snapshot.get("active_conditions", [])  # type: ignore[union-attr]
        }
        self.last_step_at = snapshot.get("last_step_at")  # type: ignore[assignment]
        observation_snapshot = snapshot.get("observation_model")
        if self.observation_model is not None and observation_snapshot is not None:
            self.observation_model.restore(observation_snapshot)  # type: ignore[arg-type]

    def _threshold_events(
        self, at_time: datetime, results: list[ZoneStepResult]
    ) -> list[PhysicsEvent]:
        """Compare current violations with the previous active-condition set."""

        current: set[tuple[str, str]] = set()
        event_by_key: dict[tuple[str, str], PhysicsEvent] = {}
        for result in results:
            conditions: list[tuple[str, PhysicsEventType, float]] = []
            if result.co2_ppm >= CO2_WARNING_THRESHOLD_PPM:
                conditions.append(
                    (
                        "co2_high",
                        PhysicsEventType.AIR_QUALITY_THRESHOLD_VIOLATED,
                        result.co2_ppm,
                    )
                )
            if result.pm25_ug_m3 >= PM25_WARNING_THRESHOLD_UG_M3:
                conditions.append(
                    (
                        "pm25_high",
                        PhysicsEventType.AIR_QUALITY_THRESHOLD_VIOLATED,
                        result.pm25_ug_m3,
                    )
                )
            if result.comfort.score < 0.7:
                conditions.append(
                    (
                        "comfort_low",
                        PhysicsEventType.COMFORT_THRESHOLD_VIOLATED,
                        result.comfort.score,
                    )
                )
            for condition, event_type, value in conditions:
                key = (result.zone_id, condition)
                current.add(key)
                event_by_key[key] = PhysicsEvent(
                    event_type=event_type,
                    occurred_at=at_time,
                    zone_id=result.zone_id,
                    condition=condition,
                    payload={
                        "value": value,
                        "severity": "warning",
                        "threshold": (
                            CO2_WARNING_THRESHOLD_PPM
                            if condition == "co2_high"
                            else PM25_WARNING_THRESHOLD_UG_M3
                            if condition == "pm25_high"
                            else 0.7
                        ),
                    },
                )

        # Set differences are the edges of the threshold state machine.  Sort
        # them so simultaneous multi-zone events have deterministic order.
        newly_active = current - self._active_conditions
        recovered = self._active_conditions - current
        events = [event_by_key[key] for key in sorted(newly_active)]
        for zone_id, condition in sorted(recovered):
            events.append(
                PhysicsEvent(
                    event_type=PhysicsEventType.THRESHOLD_RECOVERED,
                    occurred_at=at_time,
                    zone_id=zone_id,
                    condition=condition,
                    payload={"status": "recovered"},
                )
            )
        self._active_conditions = current
        return events
