"""Unified deterministic runtime for Building World, physics and sensing."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fairy.apps.building_world.building_world_app import BuildingWorldApp
from fairy.apps.building_world.room_loader import LoadedRoomConfiguration
from fairy.apps.building_world.sensor_hub import SensorHub
from fairy.apps.building_world.sensor_shadow import SensorShadowEvaluator
from fairy.apps.building_world.types import (
    BuildingEvent,
    BuildingEventType,
    DeviceHealth,
    DeviceType,
)
from fairy.controllers.building.event_queue import (
    BuildingEventQueue,
    ScheduledBuildingEvent,
)
from fairy.controllers.building.trigger_policy import (
    BuildingTriggerPolicy,
    TriggerDecision,
)
from fairy.physics.building.equipment_model import BuildingEquipmentModel
from fairy.physics.building.indoor_environment_engine import IndoorEnvironmentEngine
from fairy.physics.building.models import InternalLoads, OutdoorConditions
from fairy.physics.building.observation_model import BuildingObservationModel
from fairy.physics.building.orchestrator import (
    BuildingPhysicsOrchestrator,
    BuildingPhysicsStepResult,
    PhysicsEventType,
)

OutdoorProvider = Callable[[datetime], OutdoorConditions]
ScheduledEventHandler = Callable[[ScheduledBuildingEvent, BuildingWorldApp], None]
TriggerListener = Callable[[BuildingEvent, TriggerDecision], None]


@dataclass(frozen=True)
class RuntimeAdvanceResult:
    """Aggregate all fixed-size physics ticks used to reach a target time."""

    started_at: datetime
    ended_at: datetime
    steps: tuple[BuildingPhysicsStepResult, ...]
    processed_events: tuple[BuildingEvent, ...] = ()
    trigger_decisions: tuple[TriggerDecision, ...] = ()


class BuildingWorldRuntime:
    """Own cross-layer time advancement without exposing simulator truth."""

    def __init__(
        self,
        *,
        world: BuildingWorldApp,
        physics: BuildingPhysicsOrchestrator,
        sensors: SensorHub,
        start_at: datetime,
        outdoor_provider: OutdoorProvider,
        equipment_model: BuildingEquipmentModel | None = None,
        event_queue: BuildingEventQueue | None = None,
        trigger_policy: BuildingTriggerPolicy | None = None,
        max_timestep_seconds: float = 300.0,
    ) -> None:
        if start_at.tzinfo is None:
            raise ValueError("start_at must be timezone-aware")
        if max_timestep_seconds <= 0.0:
            raise ValueError("max_timestep_seconds must be positive")
        self.world = world
        self.physics = physics
        self.sensors = sensors
        self.current_time = start_at
        self.outdoor_provider = outdoor_provider
        self.equipment_model = equipment_model or BuildingEquipmentModel()
        self.event_queue = event_queue or BuildingEventQueue()
        self.trigger_policy = trigger_policy or BuildingTriggerPolicy()
        self.max_timestep_seconds = float(max_timestep_seconds)
        self.trace: list[dict[str, object]] = []
        self.sensor_shadow: SensorShadowEvaluator | None = None
        # Handlers apply external facts to canonical state before those facts
        # are published.  Scenario-specific callbacks stay outside the queue,
        # keeping the queue serializable and replay-stable.
        self._event_handlers: dict[BuildingEventType, list[ScheduledEventHandler]] = {}
        self._trigger_listeners: list[TriggerListener] = []
        self._world_event_cursor = len(self.world.events)

    @classmethod
    def from_room_configuration(
        cls,
        configuration: LoadedRoomConfiguration,
        *,
        start_at: datetime,
        outdoor_provider: OutdoorProvider,
        world: BuildingWorldApp | None = None,
        observation_seed: int = 0,
        max_timestep_seconds: float = 300.0,
    ) -> BuildingWorldRuntime:
        return cls.from_room_configurations(
            (configuration,),
            start_at=start_at,
            outdoor_provider=outdoor_provider,
            world=world,
            observation_seed=observation_seed,
            max_timestep_seconds=max_timestep_seconds,
        )

    @classmethod
    def from_room_configurations(
        cls,
        configurations: Sequence[LoadedRoomConfiguration],
        *,
        start_at: datetime,
        outdoor_provider: OutdoorProvider,
        world: BuildingWorldApp | None = None,
        observation_seed: int = 0,
        max_timestep_seconds: float = 300.0,
    ) -> BuildingWorldRuntime:
        """Assemble multiple declarative rooms into one shared runtime."""

        if not configurations:
            raise ValueError("at least one room configuration is required")
        world = world or BuildingWorldApp()
        zone_parameters = {}
        initial_zone_states = {}
        sensor_specs = []
        sensor_ids: set[str] = set()
        for configuration in configurations:
            configuration.install_into(world)
            duplicate_zones = set(zone_parameters) & set(configuration.zone_parameters)
            if duplicate_zones:
                raise ValueError(
                    f"duplicate zones across room configurations: {sorted(duplicate_zones)}"
                )
            zone_parameters.update(configuration.zone_parameters)
            initial_zone_states.update(configuration.initial_zone_states)
            for sensor in configuration.sensors:
                if sensor.sensor_id in sensor_ids:
                    raise ValueError(
                        f"duplicate sensor across room configurations: {sensor.sensor_id!r}"
                    )
                sensor_ids.add(sensor.sensor_id)
                sensor_specs.append(sensor)
        sensors = SensorHub(zone_parameters)
        observation = BuildingObservationModel(
            tuple(sensor_specs), seed=observation_seed
        )
        physics = BuildingPhysicsOrchestrator(
            IndoorEnvironmentEngine(
                zone_parameters,
                initial_zone_states,
            ),
            observation_model=observation,
            observation_sink=sensors,
        )
        return cls(
            world=world,
            physics=physics,
            sensors=sensors,
            start_at=start_at,
            outdoor_provider=outdoor_provider,
            max_timestep_seconds=max_timestep_seconds,
        )

    def advance_by(self, seconds: float) -> RuntimeAdvanceResult:
        if seconds <= 0.0:
            raise ValueError("advance duration must be positive")
        return self.advance_to(self.current_time + timedelta(seconds=seconds))

    def schedule_event(
        self,
        *,
        execute_at: datetime,
        event_type: BuildingEventType,
        source: str,
        subject_id: str,
        payload: Mapping[str, object] | None = None,
        parent_event_id: str | None = None,
        scheduled_id: str | None = None,
    ) -> ScheduledBuildingEvent:
        """Schedule a future fact without publishing it ahead of simulation time."""

        if execute_at.tzinfo is None:
            raise ValueError("execute_at must be timezone-aware")
        if execute_at < self.current_time:
            raise ValueError("cannot schedule a building event in the past")
        return self.event_queue.schedule(
            execute_at=execute_at,
            event_type=event_type,
            source=source,
            subject_id=subject_id,
            payload=payload,
            parent_event_id=parent_event_id,
            scheduled_id=scheduled_id,
        )

    def cancel_scheduled_event(self, scheduled_id: str) -> bool:
        """Cancel an event that has not yet become a domain fact."""

        return self.event_queue.cancel(scheduled_id)

    def configure_sensor_shadow(
        self,
        *,
        reference_source: str,
        candidate_source: str = "published",
        max_alignment_seconds: float = 300.0,
    ) -> SensorShadowEvaluator:
        """Enable passive observation comparison without changing physics truth."""

        self.sensor_shadow = SensorShadowEvaluator(
            reference_source=reference_source,
            candidate_source=candidate_source,
            max_alignment_seconds=max_alignment_seconds,
        )
        return self.sensor_shadow

    def register_event_handler(
        self,
        event_type: BuildingEventType,
        handler: ScheduledEventHandler,
    ) -> None:
        """Register deterministic state application for one external event type."""

        handlers = self._event_handlers.setdefault(event_type, [])
        if handler not in handlers:
            handlers.append(handler)

    def register_trigger_listener(self, listener: TriggerListener) -> None:
        """Observe committed events after deterministic trigger classification.

        Listener failures are isolated and recorded in ``trace`` so model-facing
        control layers cannot interrupt physics or event-queue advancement.
        """

        if listener not in self._trigger_listeners:
            self._trigger_listeners.append(listener)

    def initialize_observations(self) -> int:
        """Publish a time-zero sensor sample after scenario truth is configured."""

        observations = self.physics.sample_observations(self.current_time)
        self.trace.append(
            {
                "kind": "initial_observation",
                "at_time": self.current_time.isoformat(),
                "observation_count": len(observations),
            }
        )
        if observations:
            self.world.publish_building_event(
                BuildingEventType.SENSOR_UPDATED,
                source=self.__class__.__name__,
                subject_id="building-sensors",
                occurred_at=self.current_time,
                payload={"sample_count": len(observations), "initial": True},
            )
        return len(observations)

    def advance_to(
        self, target_time: datetime, *, stop_on_wakeup: bool = False
    ) -> RuntimeAdvanceResult:
        if target_time.tzinfo is None:
            raise ValueError("target_time must be timezone-aware")
        if target_time <= self.current_time:
            raise ValueError("target_time must be later than current runtime time")
        started_at = self.current_time
        steps: list[BuildingPhysicsStepResult] = []
        processed_events: list[BuildingEvent] = []
        trigger_decisions: list[TriggerDecision] = []
        while True:
            # Consume events exactly at the current boundary before choosing
            # the next physics interval.  This also handles events scheduled
            # for the final target timestamp.
            self._consume_due_events()
            events, decisions = self._classify_new_world_events()
            processed_events.extend(events)
            trigger_decisions.extend(decisions)
            # Interactive Agent calls stop at the first plan-invalidating
            # event. Oracle/replay callers retain full deterministic advances
            # by leaving ``stop_on_wakeup`` disabled.
            if stop_on_wakeup and any(item.should_wake_agent for item in decisions):
                break
            if self.current_time >= target_time:
                break
            remaining = (target_time - self.current_time).total_seconds()
            timestep = min(self.max_timestep_seconds, remaining)
            next_event_time = self.event_queue.next_time()
            if next_event_time is not None:
                seconds_to_event = (next_event_time - self.current_time).total_seconds()
                if seconds_to_event < 0.0:
                    raise RuntimeError("event queue returned an event in the past")
                timestep = min(timestep, seconds_to_event)
            if timestep <= 0.0:
                # A due event will be consumed at the top of the next loop.
                continue
            step_at = self.current_time + timedelta(seconds=timestep)
            truth = self.physics.engine.get_state()
            commands = self.equipment_model.commands_by_zone(
                self.world.devices, self.world.device_states, truth
            )
            step = self.physics.step(
                at_time=step_at,
                timestep_seconds=timestep,
                outdoor=self.outdoor_provider(step_at),
                loads_by_zone=self._loads_by_zone(),
                hvac_by_zone=commands,
            )
            self.current_time = step_at
            self._publish_step_events(step)
            self._record_trace(step, timestep)
            self._record_sensor_shadow()
            steps.append(step)
        return RuntimeAdvanceResult(
            started_at,
            self.current_time,
            tuple(steps),
            tuple(processed_events),
            tuple(trigger_decisions),
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "current_time": self.current_time,
            "world": self.world.snapshot(),
            "physics": self.physics.snapshot(),
            "sensors": self.sensors.snapshot(),
            "event_queue": self.event_queue.snapshot(),
            "world_event_cursor": self._world_event_cursor,
            "trace": list(self.trace),
            "sensor_shadow": (
                self.sensor_shadow.snapshot() if self.sensor_shadow is not None else None
            ),
        }

    def restore(self, snapshot: Mapping[str, object]) -> None:
        current_time = snapshot.get("current_time")
        if not isinstance(current_time, datetime):
            raise ValueError("runtime snapshot has invalid current_time")
        self.world.restore(snapshot["world"])  # type: ignore[arg-type]
        self.physics.restore(snapshot["physics"])  # type: ignore[arg-type]
        self.sensors.restore(snapshot["sensors"])  # type: ignore[arg-type]
        self.event_queue.restore(snapshot.get("event_queue", {}))  # type: ignore[arg-type]
        self.current_time = current_time
        self._world_event_cursor = int(
            snapshot.get("world_event_cursor", len(self.world.events))
        )
        if not 0 <= self._world_event_cursor <= len(self.world.events):
            raise ValueError("runtime snapshot has invalid world_event_cursor")
        self.trace = list(snapshot.get("trace", []))  # type: ignore[arg-type]
        shadow_snapshot = snapshot.get("sensor_shadow")
        if self.sensor_shadow is not None and isinstance(shadow_snapshot, Mapping):
            self.sensor_shadow.restore(shadow_snapshot)

    def _consume_due_events(self) -> None:
        """Apply and publish all scheduled facts due at the current time."""

        for scheduled in self.event_queue.pop_due(self.current_time):
            for handler in self._event_handlers.get(scheduled.event_type, []):
                handler(scheduled, self.world)
            self.world.publish_building_event(
                scheduled.event_type,
                source=scheduled.source,
                subject_id=scheduled.subject_id,
                payload=dict(scheduled.payload),
                parent_event_id=scheduled.parent_event_id,
                occurred_at=scheduled.execute_at,
            )

    def _classify_new_world_events(
        self,
    ) -> tuple[list[BuildingEvent], list[TriggerDecision]]:
        """Classify each newly published event exactly once and audit the result."""

        events = list(self.world.events[self._world_event_cursor :])
        decisions = [self.trigger_policy.decide(event) for event in events]
        self._world_event_cursor = len(self.world.events)
        for event, decision in zip(events, decisions, strict=True):
            self.trace.append(
                {
                    "kind": "trigger_decision",
                    "event_id": decision.event_id,
                    "event_type": decision.event_type.value,
                    "should_wake_agent": decision.should_wake_agent,
                    "reason": decision.reason,
                }
            )
            for listener in tuple(self._trigger_listeners):
                try:
                    listener(event, decision)
                # Event subscribers are external control/observability layers;
                # none may stop deterministic Runtime advancement.
                except Exception as exc:  # noqa: BLE001
                    self.trace.append(
                        {
                            "kind": "trigger_listener_error",
                            "event_id": event.event_id,
                            "event_type": event.event_type.value,
                            "listener": getattr(
                                listener, "__qualname__", listener.__class__.__name__
                            ),
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                        }
                    )
        return events, decisions

    def _loads_by_zone(self) -> dict[str, InternalLoads]:
        located_people: dict[str, int] = {zone_id: 0 for zone_id in self.world.zones}
        for person in self.world.people.values():
            if not person.on_campus or person.current_zone_id is None:
                continue
            if person.current_zone_id in located_people:
                located_people[person.current_zone_id] += 1
                continue
            # Older fixtures store a room ID as location. Map it to the first
            # declared zone so they remain compatible with zone-level physics.
            matching_zones = [
                zone.zone_id
                for zone in self.world.zones.values()
                if zone.room_id == person.current_zone_id
            ]
            if matching_zones:
                located_people[matching_zones[0]] += 1
        loads: dict[str, InternalLoads] = {}
        for zone_id, zone in self.world.zones.items():
            explicit_room_count = self.world.room_states[zone.room_id].occupancy_count
            occupants = max(located_people[zone_id], explicit_room_count)
            equipment_power = 0.0
            for device_id, spec in self.world.devices.items():
                state = self.world.device_states[device_id]
                if (
                    spec.zone_id == zone_id
                    and state.power_on
                    and state.health == DeviceHealth.ONLINE
                    and spec.device_type
                    not in {
                        DeviceType.HVAC,
                        DeviceType.HUMIDIFIER,
                        DeviceType.AIR_PURIFIER,
                        DeviceType.VENTILATION,
                    }
                ):
                    power_fraction = 1.0
                    if spec.device_type == DeviceType.LIGHTING:
                        power_fraction = (
                            float(state.settings.get("brightness_pct", 100.0)) / 100.0
                        )
                    equipment_power += spec.rated_power_w * power_fraction
            loads[zone_id] = InternalLoads(
                occupants=float(occupants),
                equipment_heat_w=equipment_power,
                equipment_electric_power_w=equipment_power,
            )
        return loads

    def _publish_step_events(self, step: BuildingPhysicsStepResult) -> None:
        if step.observations:
            self.world.publish_building_event(
                BuildingEventType.SENSOR_UPDATED,
                source=self.__class__.__name__,
                subject_id="building-sensors",
                occurred_at=step.at_time,
                payload={"sample_count": len(step.observations)},
            )
        event_types = {
            PhysicsEventType.COMFORT_THRESHOLD_VIOLATED: (
                BuildingEventType.COMFORT_THRESHOLD_VIOLATED
            ),
            PhysicsEventType.AIR_QUALITY_THRESHOLD_VIOLATED: (
                BuildingEventType.AIR_QUALITY_THRESHOLD_VIOLATED
            ),
            PhysicsEventType.THRESHOLD_RECOVERED: BuildingEventType.THRESHOLD_RECOVERED,
        }
        for event in step.events:
            self.world.publish_building_event(
                event_types[event.event_type],
                source="BuildingPhysicsOrchestrator",
                subject_id=event.zone_id,
                occurred_at=event.occurred_at,
                payload={"condition": event.condition, **dict(event.payload)},
            )

    def _record_trace(
        self, step: BuildingPhysicsStepResult, timestep_seconds: float
    ) -> None:
        self.trace.append(
            {
                "kind": "physics_step",
                "at_time": step.at_time.isoformat(),
                "timestep_seconds": timestep_seconds,
                "zones": [
                    {
                        "zone_id": result.zone_id,
                        "occupancy_count": self.world.room_states[
                            self.world.zones[result.zone_id].room_id
                        ].occupancy_count,
                        "air_temperature_c": result.air_temperature_c,
                        "relative_humidity_pct": result.relative_humidity_pct,
                        "co2_ppm": result.co2_ppm,
                        "pm25_ug_m3": result.pm25_ug_m3,
                        "electric_power_w": result.electric_power_w,
                        "energy_kwh": result.energy_kwh,
                        "comfort_score": result.comfort.score,
                    }
                    for result in step.zones
                ],
                "physics_events": [event.event_type.value for event in step.events],
                "observation_count": len(step.observations),
            }
        )

    def _record_sensor_shadow(self) -> None:
        """Record any newly aligned real/simulated samples in the runtime trace."""

        if self.sensor_shadow is None:
            return
        request = self.sensors.read_all_request(self.current_time)
        for comparison in self.sensor_shadow.sample(self.sensors, request):
            self.trace.append(
                {
                    "kind": "sensor_shadow",
                    "at_time": comparison.compared_at.isoformat(),
                    "zone_id": comparison.zone_id,
                    "quantity": comparison.quantity.value,
                    "reference_sensor_id": comparison.reference_sensor_id,
                    "candidate_sensor_id": comparison.candidate_sensor_id,
                    "reference_value": comparison.reference_value,
                    "candidate_value": comparison.candidate_value,
                    "signed_error": comparison.signed_error,
                    "absolute_error": comparison.absolute_error,
                    "time_offset_seconds": comparison.time_offset_seconds,
                }
            )


def constant_outdoor_provider(
    conditions: OutdoorConditions,
) -> OutdoorProvider:
    """Return a deterministic provider useful before adding a weather API."""

    return lambda _at_time: conditions


def utc_datetime(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)
