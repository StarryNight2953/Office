"""Normal K1315 conference lifecycle with environment preparation."""

from __future__ import annotations

from datetime import datetime

from smartoffice.apps.agent_user_interface import AgentUserInterface
from smartoffice.apps.building_world import (
    AirDeviceApp,
    BuildingEventType,
    BuildingSensorApp,
    BuildingWorldApp,
    HvacApp,
    LightingApp,
    MeetingEquipmentApp,
    MeetingStatus,
    PrintingApp,
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

ROOM_ID = "k1315"
ZONE_ID = "k1315_conference_zone"
MEETING_ID = "conference-standard-001"
RESERVATION_ID = "conference-standard-reservation-001"


@register_scenario("scenario_building_kechuang_k1315_conference_standard")
class ScenarioBuildingKechuangK1315ConferenceStandard(KechuangBuildingScenario):
    """Prepare, host and close a scheduled K1315 conference without exceptions."""

    start_time: float | None = local_timestamp(2026, 9, 10, 9)
    time_increment_in_seconds: int = 60
    scenario_input = """
K1315 会议室将在今天 10:00 至 11:00 举行 12 人正式会议。请在 09:15 开始准备：
打印 12 份、每份 2 页的会议材料，开启独立通风、制冷和空气净化，设置演示区及
观众区灯光，准备投影与麦克风音响。会前检查打印任务、会议设备和环境读数。
会议正常结束后关闭所有本次启用的设备。不得跳过物理等待，也不得把设备命令被
接受视为环境目标已经达到。
""".strip()

    def initiate_scenario(self) -> None:
        super().initiate_scenario()
        world = self.get_typed_app(BuildingWorldApp)
        start = datetime(2026, 9, 10, 10, 0, tzinfo=CST)
        end = datetime(2026, 9, 10, 11, 0, tzinfo=CST)
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
                organizer_id="staff-01",
                participant_ids=("professor-01",),
                start_at=start,
                end_at=end,
                expected_attendees=12,
                title="Standard research conference",
                required_capabilities=("projector", "audio", "microphone"),
                reservation_id=RESERVATION_ID,
            )
        )
        install_normal_lifecycle(
            self.building_runtime,
            (
                MeetingPhase(
                    "conference-preparation",
                    datetime(2026, 9, 10, 9, 15, tzinfo=CST),
                    BuildingEventType.MEETING_PREPARATION_DUE,
                    MEETING_ID,
                    {"room_id": ROOM_ID},
                ),
                MeetingPhase(
                    "conference-arrival",
                    datetime(2026, 9, 10, 9, 45, tzinfo=CST),
                    BuildingEventType.OCCUPANCY_CHANGED,
                    ROOM_ID,
                    {"room_id": ROOM_ID, "occupancy_count": 12},
                ),
                MeetingPhase(
                    "conference-start",
                    start,
                    BuildingEventType.MEETING_STARTED,
                    MEETING_ID,
                    {"occupancy_count": 12},
                ),
                MeetingPhase(
                    "conference-end",
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
        air = self.get_typed_app(AirDeviceApp)
        ventilation = self.get_typed_app(VentilationApp)
        lighting = self.get_typed_app(LightingApp)
        equipment = self.get_typed_app(MeetingEquipmentApp)
        printing = self.get_typed_app(PrintingApp)

        with EventRegisterer.capture_mode():
            briefing = (
                aui.send_message_to_agent(content=self.scenario_input)
                .with_id("briefing")
                .depends_on(None, delay_seconds=1)
            )
            wait_for_preparation = (
                system.advance_time(minutes=15, stop_on_event=False)
                .oracle()
                .with_id("wait_for_preparation")
                .depends_on(briefing, delay_seconds=1)
            )
            initial_environment = (
                sensors.read_zone_sensors(ZONE_ID)
                .oracle()
                .with_id("read_preparation_environment")
                .depends_on(wait_for_preparation, delay_seconds=1)
            )
            start_ventilation = (
                ventilation.set_ventilation("k1315_ventilation_01", True, 3)
                .oracle()
                .with_id("start_conference_ventilation")
                .depends_on(initial_environment, delay_seconds=1)
            )
            start_hvac = (
                hvac.set_hvac("k1315_hvac_01", True, "cooling", 24.0, 3)
                .oracle()
                .with_id("start_conference_hvac")
                .depends_on(start_ventilation, delay_seconds=1)
            )
            start_purifier = (
                air.set_air_purifier("k1315_purifier_01", True, 2)
                .oracle()
                .with_id("start_conference_purifier")
                .depends_on(start_hvac, delay_seconds=1)
            )
            printer_on = (
                printing.set_printer_power("k1315_printer_01", True)
                .oracle()
                .with_id("power_on_conference_printer")
                .depends_on(start_purifier, delay_seconds=1)
            )
            submit_materials = (
                printing.submit_print_job(
                    "k1315_printer_01", "conference_materials", 12, 2, "staff-01"
                )
                .oracle()
                .with_id("print_conference_materials")
                .depends_on(printer_on, delay_seconds=1)
            )
            set_front_lighting = (
                lighting.set_lighting(
                    "k1315_lighting_front_01", True, 45, 4000, "presentation"
                )
                .oracle()
                .with_id("set_presentation_lighting")
                .depends_on(submit_materials, delay_seconds=1)
            )
            set_audience_lighting = (
                lighting.set_lighting(
                    "k1315_lighting_audience_01", True, 70, 4000, "conference"
                )
                .oracle()
                .with_id("set_audience_lighting")
                .depends_on(set_front_lighting, delay_seconds=1)
            )
            start_projector = (
                equipment.set_projector("k1315_projector_01", True, "hdmi")
                .oracle()
                .with_id("prepare_projector")
                .depends_on(set_audience_lighting, delay_seconds=1)
            )
            start_audio = (
                equipment.set_audio_system("k1315_audio_01", True, 55, True)
                .oracle()
                .with_id("prepare_audio_and_microphone")
                .depends_on(start_projector, delay_seconds=1)
            )
            precondition = (
                system.advance_time(minutes=30, stop_on_event=False)
                .oracle()
                .with_id("precondition_room")
                .depends_on(start_audio, delay_seconds=1)
            )
            print_check = (
                printing.get_print_job("print-job-0001")
                .oracle()
                .with_id("verify_printed_materials")
                .depends_on(precondition, delay_seconds=1)
            )
            before_meeting = (
                sensors.read_zone_sensors(ZONE_ID)
                .oracle()
                .with_id("verify_before_meeting")
                .depends_on(print_check, delay_seconds=1)
            )
            equipment_check = (
                equipment.get_meeting_equipment_readiness(ROOM_ID)
                .oracle()
                .with_id("verify_meeting_equipment")
                .depends_on(before_meeting, delay_seconds=1)
            )
            wait_for_start = (
                system.advance_time(minutes=15, stop_on_event=False)
                .oracle()
                .with_id("wait_for_meeting_start")
                .depends_on(equipment_check, delay_seconds=1)
            )
            meeting_start_reading = (
                sensors.read_zone_sensors(ZONE_ID)
                .oracle()
                .with_id("verify_at_meeting_start")
                .depends_on(wait_for_start, delay_seconds=1)
            )
            run_meeting = (
                system.advance_time(minutes=60, stop_on_event=False)
                .oracle()
                .with_id("run_standard_meeting")
                .depends_on(meeting_start_reading, delay_seconds=1)
            )
            end_reading = (
                sensors.read_zone_sensors(ZONE_ID)
                .oracle()
                .with_id("read_at_meeting_end")
                .depends_on(run_meeting, delay_seconds=1)
            )
            stop_hvac = (
                hvac.set_hvac("k1315_hvac_01", False, "off", 24.0, 0)
                .oracle()
                .with_id("stop_conference_hvac")
                .depends_on(end_reading, delay_seconds=1)
            )
            stop_purifier = (
                air.set_air_purifier("k1315_purifier_01", False, 0)
                .oracle()
                .with_id("stop_conference_purifier")
                .depends_on(stop_hvac, delay_seconds=1)
            )
            stop_ventilation = (
                ventilation.set_ventilation("k1315_ventilation_01", False, 0)
                .oracle()
                .with_id("stop_conference_ventilation")
                .depends_on(stop_purifier, delay_seconds=1)
            )
            stop_projector = (
                equipment.set_projector("k1315_projector_01", False, "off")
                .oracle()
                .with_id("stop_projector")
                .depends_on(stop_ventilation, delay_seconds=1)
            )
            stop_audio = (
                equipment.set_audio_system("k1315_audio_01", False, 0, False)
                .oracle()
                .with_id("stop_audio")
                .depends_on(stop_projector, delay_seconds=1)
            )
            stop_front_lighting = (
                lighting.set_lighting("k1315_lighting_front_01", False, 0, 4000, "off")
                .oracle()
                .with_id("stop_presentation_lighting")
                .depends_on(stop_audio, delay_seconds=1)
            )
            stop_audience_lighting = (
                lighting.set_lighting(
                    "k1315_lighting_audience_01", False, 0, 4000, "off"
                )
                .oracle()
                .with_id("stop_audience_lighting")
                .depends_on(stop_front_lighting, delay_seconds=1)
            )
            printer_off = (
                printing.set_printer_power("k1315_printer_01", False)
                .oracle()
                .with_id("power_off_conference_printer")
                .depends_on(stop_audience_lighting, delay_seconds=1)
            )
            report = (
                aui.send_message_to_user(
                    content="K1315 标准会议已正常结束，环境设备已关闭，房间资源已释放。"
                )
                .oracle()
                .with_id("report_conference_complete")
                .depends_on(printer_off, delay_seconds=1)
            )
            self.events = collect_event_graph(briefing)
            assert any(event is report for event in self.events)

    def validate(self, env) -> ScenarioValidationResult:
        world = self.get_typed_app(BuildingWorldApp)
        meeting = world.schedule[MEETING_ID]
        reservation = world.reservations[RESERVATION_ID]
        controlled_device_ids = (
            "k1315_hvac_01",
            "k1315_purifier_01",
            "k1315_ventilation_01",
            "k1315_projector_01",
            "k1315_audio_01",
            "k1315_lighting_front_01",
            "k1315_lighting_audience_01",
            "k1315_printer_01",
        )
        all_devices_off = all(
            not world.device_states[device_id].power_on
            for device_id in controlled_device_ids
        )
        print_job = self.get_typed_app(PrintingApp).jobs.get("print-job-0001", {})
        expected_end = datetime(2026, 9, 10, 11, 0, tzinfo=CST)
        elapsed_after_end_minutes = max(
            0.0,
            (self.building_runtime.current_time - expected_end).total_seconds() / 60.0,
        )
        completed_on_time = elapsed_after_end_minutes <= 20.0
        ordered = event_types_in_order(world.events)
        expected = [
            BuildingEventType.MEETING_PREPARATION_DUE,
            BuildingEventType.OCCUPANCY_CHANGED,
            BuildingEventType.MEETING_STARTED,
            BuildingEventType.MEETING_ENDED,
        ]
        success = (
            meeting.status == MeetingStatus.COMPLETED
            and reservation.status == ReservationStatus.RELEASED
            and world.room_states[ROOM_ID].occupancy_count == 0
            and all_devices_off
            and print_job.get("status") == "completed"
            and completed_on_time
            and ordered == expected
            and self.building_runtime.physics.cumulative_energy_kwh_by_zone[ZONE_ID]
            > 0.0
        )
        return ScenarioValidationResult(
            success=success,
            rationale=(
                "standard conference lifecycle completed"
                if success
                else "conference lifecycle or cleanup is incomplete"
            ),
            metadata={
                "lifecycle_events": [item.value for item in ordered],
                "meeting_status": meeting.status.value,
                "reservation_status": reservation.status.value,
                "print_job_status": print_job.get("status"),
                "all_controlled_devices_off": all_devices_off,
                "elapsed_after_scheduled_end_minutes": elapsed_after_end_minutes,
                "completed_on_time": completed_on_time,
                "energy_kwh": self.building_runtime.physics.cumulative_energy_kwh_by_zone[
                    ZONE_ID
                ],
            },
        )
