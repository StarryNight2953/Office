"""Normal K1316 small-seminar lifecycle with environmental feedback."""

from __future__ import annotations

from datetime import datetime

from smartoffice.apps.agent_user_interface import AgentUserInterface
from smartoffice.apps.building_world import (
    BuildingEventType,
    BuildingSensorApp,
    BuildingWorldApp,
    HvacApp,
    LightingApp,
    MeetingEquipmentApp,
    MeetingStatus,
    ReservationStatus,
    ResourceReservation,
    ScheduleEntry,
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
    event_types_in_order,
    install_normal_lifecycle,
)
from smartoffice.scenarios.registry import register_scenario
from smartoffice.scenarios.validation_result import ScenarioValidationResult
from smartoffice.types import EventRegisterer

ROOM_ID = "k1316"
ZONE_ID = "k1316_seminar_zone"
MEETING_ID = "seminar-standard-001"
RESERVATION_ID = "seminar-standard-reservation-001"


@register_scenario("scenario_building_kechuang_k1316_seminar_standard")
class ScenarioBuildingKechuangK1316SeminarStandard(KechuangBuildingScenario):
    """Prepare, run and close a normal eight-person seminar in K1316."""

    start_time: float | None = local_timestamp(2026, 9, 11, 9)
    time_increment_in_seconds: int = 60
    scenario_input = """
K1316 研讨室将在今天 10:00 至 11:00 举行 8 人组会。请从 09:30 开始准备：检查
环境，开启通风和制冷，设置研讨照明并准备投影。会前必须复查设备和环境。会议期间
在 10:30 再次检查 CO₂，并根据满员状态加强通风。会议正常结束后关闭本次启用的
空调、通风、灯光和投影，释放房间资源。设备命令被接受不等于环境已经达标。
""".strip()

    def initiate_scenario(self) -> None:
        super().initiate_scenario()
        world = self.get_typed_app(BuildingWorldApp)
        start = datetime(2026, 9, 11, 10, 0, tzinfo=CST)
        end = datetime(2026, 9, 11, 11, 0, tzinfo=CST)
        world.reservations[RESERVATION_ID] = ResourceReservation(
            reservation_id=RESERVATION_ID,
            resource_id=ROOM_ID,
            owner_id=MEETING_ID,
            start_at=start,
            end_at=end,
        )
        world.add_schedule_entry(
            ScheduleEntry(
                meeting_id=MEETING_ID,
                room_id=ROOM_ID,
                organizer_id="professor-01",
                participant_ids=("student-01",),
                start_at=start,
                end_at=end,
                expected_attendees=8,
                title="Weekly research seminar",
                required_capabilities=("projector", "whiteboard"),
                reservation_id=RESERVATION_ID,
            )
        )
        install_normal_lifecycle(
            self.building_runtime,
            (
                MeetingPhase(
                    "seminar-preparation",
                    datetime(2026, 9, 11, 9, 30, tzinfo=CST),
                    BuildingEventType.MEETING_PREPARATION_DUE,
                    MEETING_ID,
                    {"room_id": ROOM_ID},
                ),
                MeetingPhase(
                    "seminar-arrival",
                    datetime(2026, 9, 11, 9, 55, tzinfo=CST),
                    BuildingEventType.OCCUPANCY_CHANGED,
                    ROOM_ID,
                    {"room_id": ROOM_ID, "occupancy_count": 8},
                ),
                MeetingPhase(
                    "seminar-start",
                    start,
                    BuildingEventType.MEETING_STARTED,
                    MEETING_ID,
                    {"occupancy_count": 8},
                ),
                MeetingPhase(
                    "seminar-midpoint-check",
                    datetime(2026, 9, 11, 10, 30, tzinfo=CST),
                    BuildingEventType.ENVIRONMENT_CHECK_DUE,
                    MEETING_ID,
                    {"room_id": ROOM_ID},
                ),
                MeetingPhase(
                    "seminar-end",
                    end,
                    BuildingEventType.MEETING_ENDED,
                    MEETING_ID,
                ),
            ),
        )

    def build_events_flow(self) -> None:
        aui = self.get_typed_app(AgentUserInterface)
        system = self.get_typed_app(SystemApp)
        sensors = self.get_typed_app(BuildingSensorApp)
        hvac = self.get_typed_app(HvacApp)
        ventilation = self.get_typed_app(VentilationApp)
        lighting = self.get_typed_app(LightingApp)
        equipment = self.get_typed_app(MeetingEquipmentApp)

        with EventRegisterer.capture_mode():
            briefing = (
                aui.send_message_to_agent(content=self.scenario_input)
                .with_id("briefing")
                .depends_on(None, delay_seconds=1)
            )
            wait_for_preparation = (
                system.advance_time(minutes=30, stop_on_event=False)
                .oracle()
                .with_id("wait_for_seminar_preparation")
                .depends_on(briefing, delay_seconds=1)
            )
            initial_environment = (
                sensors.read_zone_sensors(ZONE_ID)
                .oracle()
                .with_id("read_initial_seminar_environment")
                .depends_on(wait_for_preparation, delay_seconds=1)
            )
            start_ventilation = (
                ventilation.set_ventilation("k1316_ventilation_01", True, 2)
                .oracle()
                .with_id("start_seminar_ventilation")
                .depends_on(initial_environment, delay_seconds=1)
            )
            start_hvac = (
                hvac.set_hvac("k1316_hvac_01", True, "cooling", 24.0, 2)
                .oracle()
                .with_id("start_seminar_hvac")
                .depends_on(start_ventilation, delay_seconds=1)
            )
            set_lighting = (
                lighting.set_lighting(
                    "k1316_lighting_01", True, 70, 4000, "seminar"
                )
                .oracle()
                .with_id("set_seminar_lighting")
                .depends_on(start_hvac, delay_seconds=1)
            )
            start_projector = (
                equipment.set_projector("k1316_projector_01", True, "hdmi")
                .oracle()
                .with_id("prepare_seminar_projector")
                .depends_on(set_lighting, delay_seconds=1)
            )
            precondition = (
                system.advance_time(minutes=25, stop_on_event=False)
                .oracle()
                .with_id("precondition_seminar_room")
                .depends_on(start_projector, delay_seconds=1)
            )
            readiness = (
                equipment.get_meeting_equipment_readiness(ROOM_ID)
                .oracle()
                .with_id("verify_seminar_equipment")
                .depends_on(precondition, delay_seconds=1)
            )
            before_meeting = (
                sensors.read_zone_sensors(ZONE_ID)
                .oracle()
                .with_id("verify_seminar_environment")
                .depends_on(readiness, delay_seconds=1)
            )
            wait_for_start = (
                system.advance_time(minutes=5, stop_on_event=False)
                .oracle()
                .with_id("wait_for_seminar_start")
                .depends_on(before_meeting, delay_seconds=1)
            )
            run_first_half = (
                system.advance_time(minutes=30, stop_on_event=False)
                .oracle()
                .with_id("run_seminar_first_half")
                .depends_on(wait_for_start, delay_seconds=1)
            )
            midpoint_environment = (
                sensors.read_zone_sensors(ZONE_ID)
                .oracle()
                .with_id("read_seminar_midpoint_environment")
                .depends_on(run_first_half, delay_seconds=1)
            )
            boost_ventilation = (
                ventilation.set_ventilation("k1316_ventilation_01", True, 3)
                .oracle()
                .with_id("boost_full_room_ventilation")
                .depends_on(midpoint_environment, delay_seconds=1)
            )
            run_second_half = (
                system.advance_time(minutes=30, stop_on_event=False)
                .oracle()
                .with_id("run_seminar_second_half")
                .depends_on(boost_ventilation, delay_seconds=1)
            )
            end_environment = (
                sensors.read_zone_sensors(ZONE_ID)
                .oracle()
                .with_id("read_seminar_end_environment")
                .depends_on(run_second_half, delay_seconds=1)
            )
            stop_hvac = (
                hvac.set_hvac("k1316_hvac_01", False, "off", 24.0, 0)
                .oracle()
                .with_id("stop_seminar_hvac")
                .depends_on(end_environment, delay_seconds=1)
            )
            stop_ventilation = (
                ventilation.set_ventilation("k1316_ventilation_01", False, 0)
                .oracle()
                .with_id("stop_seminar_ventilation")
                .depends_on(stop_hvac, delay_seconds=1)
            )
            stop_projector = (
                equipment.set_projector("k1316_projector_01", False, "off")
                .oracle()
                .with_id("stop_seminar_projector")
                .depends_on(stop_ventilation, delay_seconds=1)
            )
            stop_lighting = (
                lighting.set_lighting(
                    "k1316_lighting_01", False, 0, 4000, "off"
                )
                .oracle()
                .with_id("stop_seminar_lighting")
                .depends_on(stop_projector, delay_seconds=1)
            )
            report = (
                aui.send_message_to_user(
                    content="K1316 小型研讨已结束，设备关闭且房间资源已释放。"
                )
                .oracle()
                .with_id("report_seminar_complete")
                .depends_on(stop_lighting, delay_seconds=1)
            )
            self.events = collect_event_graph(briefing)
            assert any(event is report for event in self.events)

    def validate(self, env) -> ScenarioValidationResult:
        world = self.get_typed_app(BuildingWorldApp)
        meeting = world.schedule[MEETING_ID]
        reservation = world.reservations[RESERVATION_ID]
        device_ids = (
            "k1316_hvac_01",
            "k1316_ventilation_01",
            "k1316_projector_01",
            "k1316_lighting_01",
        )
        all_devices_off = all(
            not world.device_states[device_id].power_on for device_id in device_ids
        )
        ordered = event_types_in_order(world.events)
        expected = [
            BuildingEventType.MEETING_PREPARATION_DUE,
            BuildingEventType.OCCUPANCY_CHANGED,
            BuildingEventType.MEETING_STARTED,
            BuildingEventType.ENVIRONMENT_CHECK_DUE,
            BuildingEventType.MEETING_ENDED,
        ]
        zone_steps = [
            zone
            for item in self.building_runtime.trace
            if item.get("kind") == "physics_step"
            for zone in item.get("zones", [])
            if zone["zone_id"] == ZONE_ID
        ]
        peak_co2 = max(float(step["co2_ppm"]) for step in zone_steps)
        end_co2 = float(zone_steps[-1]["co2_ppm"])
        success = (
            meeting.status == MeetingStatus.COMPLETED
            and reservation.status == ReservationStatus.RELEASED
            and world.room_states[ROOM_ID].occupancy_count == 0
            and all_devices_off
            and ordered == expected
            and peak_co2 < 1200.0
            and self.building_runtime.physics.cumulative_energy_kwh_by_zone[ZONE_ID]
            > 0.0
        )
        return ScenarioValidationResult(
            success=success,
            rationale=(
                "small seminar lifecycle completed"
                if success
                else "seminar lifecycle, environment or cleanup is incomplete"
            ),
            metadata={
                "lifecycle_events": [item.value for item in ordered],
                "meeting_status": meeting.status.value,
                "reservation_status": reservation.status.value,
                "all_controlled_devices_off": all_devices_off,
                "peak_co2_ppm": peak_co2,
                "end_co2_ppm": end_co2,
                "energy_kwh": self.building_runtime.physics.cumulative_energy_kwh_by_zone[
                    ZONE_ID
                ],
            },
        )
