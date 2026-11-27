"""Meeting booking scenario with a conflicting preferred room."""

from __future__ import annotations

from smartoffice.apps.agent_user_interface import AgentUserInterface
from smartoffice.apps.building_world import (
    BuildingWorldApp,
    OccupancyApp,
    RoomApp,
    ScheduleApp,
)
from smartoffice.apps.building_world.datetime_utils import parse_datetime
from smartoffice.apps.building_world.types import (
    BuildingEventType,
    MeetingStatus,
    ScheduleEntry,
)
from smartoffice.scenarios.building_kechuang.base import (
    KechuangBuildingScenario,
    collect_event_graph,
    local_timestamp,
)
from smartoffice.scenarios.registry import register_scenario
from smartoffice.scenarios.validation_result import ScenarioValidationResult
from smartoffice.types import EventRegisterer

START_AT = "2026-09-10T14:00:00+08:00"
END_AT = "2026-09-10T15:00:00+08:00"


@register_scenario("scenario_building_kechuang_room_booking")
class ScenarioBuildingKechuangRoomBooking(KechuangBuildingScenario):
    """Book a professor consultation after discovering K1315 is occupied."""

    # Scenario's dataclass-default migration reads concrete-class attributes,
    # so repeat inherited clock defaults here until Scenario supports MRO lookup.
    start_time: float | None = local_timestamp(2026, 9, 9, 9)
    time_increment_in_seconds: int = 60
    scenario_input = """
学生 student-01 希望与 professor-01 在 2026-09-10 14:00 至 15:00 开会，
预计共 6 人，需要投影设备，首选 K1315 会议室。K1324 是固定办公室，不可预约。

请先确认教授是否在校并检查其日程，再检查满足容量和投影要求的可用房间。
如果首选房间冲突，请选择满足条件的其他房间并完成预约。不得覆盖已有会议。
完成后报告会议 ID、最终房间和时间。
""".strip()

    def initiate_scenario(self) -> None:
        super().initiate_scenario()
        world = self.get_typed_app(BuildingWorldApp)
        # The preferred K1315 meeting room overlaps the requested interval. K1316 is the
        # only alternative satisfying both capacity and projector constraints.
        world.add_schedule_entry(
            ScheduleEntry(
                meeting_id="existing-001",
                room_id="k1315",
                organizer_id="staff-01",
                participant_ids=(),
                start_at=parse_datetime("2026-09-10T13:30:00+08:00"),
                end_at=parse_datetime("2026-09-10T15:30:00+08:00"),
                expected_attendees=10,
                status=MeetingStatus.CONFIRMED,
                title="Existing formal meeting",
                required_capabilities=("projector", "audio"),
            )
        )

    def build_events_flow(self) -> None:
        """Build the briefing and reference tool path as captured events.

        The calls inside capture mode are not executed immediately.  They are
        converted to an Oracle workflow by ``Engine`` and can then be executed
        or replayed with their declared dependencies and simulated timestamps.
        """

        aui = self.get_typed_app(AgentUserInterface)
        occupancy = self.get_typed_app(OccupancyApp)
        schedule = self.get_typed_app(ScheduleApp)
        room = self.get_typed_app(RoomApp)

        with EventRegisterer.capture_mode():
            briefing = (
                aui.send_message_to_agent(content=self.scenario_input)
                .with_id("briefing")
                .depends_on(None, delay_seconds=5)
            )
            presence = (
                occupancy.get_person_presence("professor-01")
                .oracle()
                .with_id("check_professor_presence")
                .depends_on(briefing, delay_seconds=1)
            )
            professor_schedule = (
                schedule.get_person_schedule(
                    person_id="professor-01",
                    start_at=START_AT,
                    end_at=END_AT,
                )
                .oracle()
                .with_id("check_professor_schedule")
                .depends_on(presence, delay_seconds=1)
            )
            available_room = (
                room.find_available_rooms(
                    start_at=START_AT,
                    end_at=END_AT,
                    min_capacity=6,
                    required_capabilities=["projector"],
                )
                .oracle()
                .with_id("find_available_room")
                .depends_on(professor_schedule, delay_seconds=1)
            )
            booking = (
                schedule.create_meeting(
                    request_id="request-001",
                    organizer_id="student-01",
                    participant_ids=["professor-01"],
                    room_id="k1316",
                    start_at=START_AT,
                    end_at=END_AT,
                    expected_attendees=6,
                    required_capabilities=["projector"],
                    title="Student consultation",
                )
                .oracle()
                .with_id("book_k1316")
                .depends_on(available_room, delay_seconds=1)
            )
            (
                aui.send_message_to_user(
                    content=(
                        "会议已预约：request-001 对应的新会议安排在 K1316，"
                        f"时间为 {START_AT} 至 {END_AT}。"
                    )
                )
                .oracle()
                .with_id("report_booking_result")
                .depends_on(booking, delay_seconds=1)
            )

            # Keep the full path, including the non-Oracle briefing required by
            # the first Oracle event, for agent task resolution and replay.
            self.events = collect_event_graph(briefing)

    def validate(self, env) -> ScenarioValidationResult:
        world = self.get_typed_app(BuildingWorldApp)
        created = [
            meeting
            for meeting in world.schedule.values()
            if meeting.meeting_id != "existing-001"
            and meeting.status == MeetingStatus.CONFIRMED
        ]
        success = (
            len(created) == 1
            and created[0].room_id == "k1316"
            and created[0].organizer_id == "student-01"
            and "professor-01" in created[0].participant_ids
            and not any(state.power_on for state in world.device_states.values())
            and not any(
                event.event_type == BuildingEventType.DEVICE_STATE_CHANGED
                for event in world.events
            )
        )
        return ScenarioValidationResult(
            success=success,
            rationale=(
                "meeting booked in the valid fallback room"
                if success
                else "expected one valid k1316 booking without premature device actions"
            ),
            metadata={
                "created_meeting_ids": [meeting.meeting_id for meeting in created],
                "building_event_types": [
                    event.event_type.value for event in world.events
                ],
                "active_device_ids": [
                    device_id
                    for device_id, state in world.device_states.items()
                    if state.power_on
                ],
            },
        )
