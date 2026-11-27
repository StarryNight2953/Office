"""Normal staged occupancy and indoor-air response in K1324."""

from __future__ import annotations

from datetime import datetime, timedelta

from smartoffice.apps.agent_user_interface import AgentUserInterface
from smartoffice.apps.building_world import (
    BuildingEventType,
    BuildingSensorApp,
    BuildingWorldApp,
    OccupancyApp,
    VentilationApp,
)
from smartoffice.apps.system import SystemApp
from smartoffice.scenarios.building_kechuang.base import (
    CST,
    KechuangBuildingScenario,
    collect_event_graph,
    local_timestamp,
)
from smartoffice.scenarios.building_kechuang.meeting_lifecycle import (
    MeetingPhase,
    install_normal_lifecycle,
)
from smartoffice.scenarios.registry import register_scenario
from smartoffice.scenarios.validation_result import ScenarioValidationResult
from smartoffice.types import EventRegisterer

ZONE_ID = "k1324_office_zone"
OCCUPANCY_TIMELINE = ((10, 5), (20, 10), (30, 15), (45, 10), (55, 5), (65, 0))


@register_scenario("scenario_building_kechuang_k1324_occupancy_ramp")
class ScenarioBuildingKechuangK1324OccupancyRamp(KechuangBuildingScenario):
    """Observe CO2 as attendees enter and leave in normal planned batches."""

    start_time: float | None = local_timestamp(2026, 9, 10, 9)
    time_increment_in_seconds: int = 60
    scenario_input = """
K1324 研究生办公室从 09:00 开始按计划变化人数：09:10 为 5 人、09:20 为 10 人、
09:30 为 15 人、09:45 为 10 人、09:55 为 5 人、10:05 为 0 人。请开启
`k1324_ventilation_01` 正常通风，并按上述时间边界推进；每次先读取房间人数，再检查
环境传感器。全部人员离开后继续通风 15 分钟，确认 CO₂ 从峰值开始恢复，然后关闭
独立通风设备。不要推进到计划之外的时间。
""".strip()

    def initiate_scenario(self) -> None:
        super().initiate_scenario()
        self.initial_co2_ppm = self.building_runtime.physics.engine.get_state()[
            ZONE_ID
        ].co2_ppm
        install_normal_lifecycle(
            self.building_runtime,
            tuple(
                MeetingPhase(
                    f"occupancy-{minute:02d}-{count:02d}",
                    datetime(2026, 9, 10, 9, tzinfo=CST) + timedelta(minutes=minute),
                    BuildingEventType.OCCUPANCY_CHANGED,
                    "k1324",
                    {"room_id": "k1324", "occupancy_count": count},
                )
                for minute, count in OCCUPANCY_TIMELINE
            ),
        )

    def build_events_flow(self) -> None:
        aui = self.get_typed_app(AgentUserInterface)
        system = self.get_typed_app(SystemApp)
        sensors = self.get_typed_app(BuildingSensorApp)
        occupancy = self.get_typed_app(OccupancyApp)
        ventilation = self.get_typed_app(VentilationApp)

        with EventRegisterer.capture_mode():
            briefing = (
                aui.send_message_to_agent(content=self.scenario_input)
                .with_id("briefing")
                .depends_on(None, delay_seconds=1)
            )
            previous = (
                ventilation.set_ventilation("k1324_ventilation_01", True, 2)
                .oracle()
                .with_id("start_normal_ventilation")
                .depends_on(briefing, delay_seconds=1)
            )
            previous_minute = 0
            for minute, count in OCCUPANCY_TIMELINE:
                wait = (
                    system.advance_time(
                        minutes=minute - previous_minute,
                        stop_on_event=False,
                    )
                    .oracle()
                    .with_id(f"wait_to_minute_{minute:02d}_occupancy_{count:02d}")
                    .depends_on(previous, delay_seconds=1)
                )
                occupancy_reading = (
                    occupancy.get_room_occupancy("k1324")
                    .oracle()
                    .with_id(f"read_occupancy_minute_{minute:02d}")
                    .depends_on(wait, delay_seconds=1)
                )
                previous = (
                    sensors.read_zone_sensors(ZONE_ID)
                    .oracle()
                    .with_id(f"read_minute_{minute:02d}_occupancy_{count:02d}")
                    .depends_on(occupancy_reading, delay_seconds=1)
                )
                previous_minute = minute
            recover = (
                system.advance_time(minutes=15, stop_on_event=False)
                .oracle()
                .with_id("wait_for_empty_room_recovery")
                .depends_on(previous, delay_seconds=1)
            )
            recovered = (
                sensors.read_zone_sensors(ZONE_ID)
                .oracle()
                .with_id("read_after_empty_room_recovery")
                .depends_on(recover, delay_seconds=1)
            )
            stop = (
                ventilation.set_ventilation("k1324_ventilation_01", False, 0)
                .oracle()
                .with_id("stop_normal_ventilation")
                .depends_on(recovered, delay_seconds=1)
            )
            report = (
                aui.send_message_to_user(
                    content="人员已按计划全部离开，空气恢复阶段完成，通风设备已关闭。"
                )
                .oracle()
                .with_id("report_occupancy_complete")
                .depends_on(stop, delay_seconds=1)
            )
            self.events = collect_event_graph(briefing)
            assert any(event is report for event in self.events)

    def validate(self, env) -> ScenarioValidationResult:
        world = self.get_typed_app(BuildingWorldApp)
        occupancy_events = [
            event
            for event in world.events
            if event.event_type == BuildingEventType.OCCUPANCY_CHANGED
        ]
        observed_counts = [
            int(event.payload["occupancy_count"]) for event in occupancy_events
        ]
        co2_series = [
            float(zone["co2_ppm"])
            for item in self.building_runtime.trace
            if item.get("kind") == "physics_step"
            for zone in item["zones"]
            if zone["zone_id"] == ZONE_ID
        ]
        peak_co2 = max(co2_series, default=self.initial_co2_ppm)
        final_co2 = co2_series[-1] if co2_series else self.initial_co2_ppm
        success = (
            observed_counts == [count for _, count in OCCUPANCY_TIMELINE]
            and world.room_states["k1324"].occupancy_count == 0
            and peak_co2 > self.initial_co2_ppm
            and final_co2 < peak_co2
            and not world.device_states["k1324_ventilation_01"].power_on
        )
        return ScenarioValidationResult(
            success=success,
            rationale=(
                "occupancy-driven CO2 rise and empty-room recovery were observed"
                if success
                else "occupancy or CO2 response did not follow the planned ramp"
            ),
            metadata={
                "occupancy_counts": observed_counts,
                "initial_co2_ppm": self.initial_co2_ppm,
                "peak_co2_ppm": peak_co2,
                "final_co2_ppm": final_co2,
            },
        )
