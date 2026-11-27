"""Declarative inputs shared by all Kechuang Building L3 scenarios."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from smartoffice.apps.building_world.types import BuildingEventType
from smartoffice.physics.building.models import OutdoorConditions
from smartoffice.scenarios.building_kechuang.base import CST

ROOM_CAPACITIES = {"k1324": 17, "k1316": 8, "k1315": 20}
ZONE_IDS = {
    "k1324": "k1324_office_zone",
    "k1316": "k1316_seminar_zone",
    "k1315": "k1315_conference_zone",
}


@dataclass(frozen=True)
class OutdoorPoint:
    """One point in a local-time outdoor profile."""

    minute: int
    temperature_c: float
    humidity_pct: float
    pm25_ug_m3: float
    solar_w_m2: float


@dataclass(frozen=True)
class OutdoorProfile:
    """Piecewise-linear weather profile used by the physical runtime."""

    profile_id: str
    points: tuple[OutdoorPoint, ...]

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("an outdoor profile needs at least two points")
        if tuple(point.minute for point in self.points) != tuple(
            sorted(point.minute for point in self.points)
        ):
            raise ValueError("outdoor profile points must be time ordered")

    def provider(self, start_at: datetime):
        """Create a Runtime-compatible provider anchored to scenario start."""

        def provide(at_time: datetime) -> OutdoorConditions:
            minute = (at_time - start_at).total_seconds() / 60.0
            left, right = self.points[0], self.points[-1]
            for candidate_left, candidate_right in zip(self.points, self.points[1:]):
                if candidate_left.minute <= minute <= candidate_right.minute:
                    left, right = candidate_left, candidate_right
                    break
            if minute <= self.points[0].minute:
                left = right = self.points[0]
            elif minute >= self.points[-1].minute:
                left = right = self.points[-1]
            span = max(1.0, float(right.minute - left.minute))
            ratio = min(1.0, max(0.0, (minute - left.minute) / span))

            def interpolate(name: str) -> float:
                return float(getattr(left, name)) + ratio * (
                    float(getattr(right, name)) - float(getattr(left, name))
                )

            return OutdoorConditions(
                air_temperature_c=interpolate("temperature_c"),
                relative_humidity_pct=interpolate("humidity_pct"),
                co2_ppm=420.0,
                pm25_ug_m3=interpolate("pm25_ug_m3"),
                solar_irradiance_w_m2=interpolate("solar_w_m2"),
            )

        return provide


@dataclass(frozen=True)
class OccupancyPhase:
    """Expected whole-room occupancy at a timeline boundary."""

    minute: int
    counts: tuple[tuple[str, int], ...]
    episode: str

    @property
    def occupancy(self) -> Mapping[str, int]:
        return dict(self.counts)


@dataclass(frozen=True)
class MeetingPlan:
    """A bookable activity whose state spans preparation, use and release."""

    activity_id: str
    room_id: str
    start_minute: int
    end_minute: int
    attendees: int
    title: str
    capabilities: tuple[str, ...] = ("projector",)


@dataclass(frozen=True)
class InteractionPlan:
    """A normal mid-run change that forces the Agent to reconcile its plan."""

    minute: int
    event_type: BuildingEventType
    subject_id: str
    description: str
    payload: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class PrintBatch:
    """One material job with an explicit submission point and deadline."""

    document_name: str
    copies: int
    pages_per_copy: int
    submit_minute: int
    ready_by_minute: int
    priority: int = 1
    reveal_minute: int = 0

    def __post_init__(self) -> None:
        if self.copies <= 0 or self.pages_per_copy <= 0:
            raise ValueError("print batch size must be positive")
        if not 0 <= self.submit_minute < self.ready_by_minute:
            raise ValueError("print submission must precede its deadline")
        if not 0 <= self.reveal_minute <= self.submit_minute:
            raise ValueError("print request must be revealed no later than submission")
        if self.priority not in {1, 2, 3}:
            raise ValueError("print priority must be 1, 2 or 3")


@dataclass(frozen=True)
class EvaluationSpec:
    """Scenario-specific outcome limits used by the common validator."""

    max_peak_co2_ppm: float = 1200.0
    max_peak_pm25_ug_m3: float = 50.0
    max_occupied_comfort_violation_ratio: float = 0.9
    max_unoccupied_energy_kwh: float | None = None
    response_wait_minutes: int = 10
    stability_wait_minutes: int = 5

    def __post_init__(self) -> None:
        if self.max_peak_co2_ppm <= 0 or self.max_peak_pm25_ug_m3 <= 0:
            raise ValueError("environment limits must be positive")
        if not 0.0 <= self.max_occupied_comfort_violation_ratio <= 1.0:
            raise ValueError("comfort violation ratio must be in [0, 1]")
        if self.response_wait_minutes <= 0 or self.stability_wait_minutes <= 0:
            raise ValueError("feedback waits must be positive")


@dataclass(frozen=True)
class BuildingL3Spec:
    """Complete L3 problem statement independent of its reference workflow."""

    number: int
    slug: str
    title: str
    family: str
    research_question: str
    start_at: datetime
    duration_minutes: int
    outdoor: OutdoorProfile
    phases: tuple[OccupancyPhase, ...]
    meetings: tuple[MeetingPlan, ...]
    target_temperature_c: float
    hvac_mode: str
    initial_indoor_temperature_c: float
    initial_indoor_humidity_pct: float
    preparation_lead_minutes: int
    use_humidifier: bool = False
    use_purifier: bool = False
    use_presentation: bool = False
    print_batches: tuple[PrintBatch, ...] = ()
    interaction: InteractionPlan | None = None
    power_limit_w: float | None = None
    primary_metrics: tuple[str, ...] = ()
    evaluation: EvaluationSpec = field(default_factory=EvaluationSpec)

    @property
    def scenario_id(self) -> str:
        return f"scenario_building_kechuang_l3_{self.number:02d}_{self.slug}"

    @property
    def rooms(self) -> tuple[str, ...]:
        return tuple(
            room_id
            for room_id in ROOM_CAPACITIES
            if any(room_id in phase.occupancy for phase in self.phases)
        )

    @property
    def end_at(self) -> datetime:
        return self.start_at + timedelta(minutes=self.duration_minutes)

    def validate(self) -> None:
        if self.start_at.tzinfo is None:
            raise ValueError(f"{self.scenario_id}: start_at must be timezone-aware")
        if self.duration_minutes <= 0 or len(self.phases) < 3:
            raise ValueError(f"{self.scenario_id}: L3 needs at least three phases")
        if not -10.0 <= self.initial_indoor_temperature_c <= 40.0:
            raise ValueError(f"{self.scenario_id}: invalid initial indoor temperature")
        if not 0.0 <= self.initial_indoor_humidity_pct <= 100.0:
            raise ValueError(f"{self.scenario_id}: invalid initial indoor humidity")
        if not 5 <= self.preparation_lead_minutes <= 180:
            raise ValueError(f"{self.scenario_id}: invalid preparation lead time")
        phase_minutes = tuple(phase.minute for phase in self.phases)
        if phase_minutes != tuple(sorted(phase_minutes)):
            raise ValueError(f"{self.scenario_id}: phases must be ordered")
        if phase_minutes[-1] > self.duration_minutes:
            raise ValueError(f"{self.scenario_id}: phase exceeds scenario duration")
        if phase_minutes[0] < 0:
            raise ValueError(f"{self.scenario_id}: phase cannot precede start")
        if len({phase.episode for phase in self.phases}) < 3:
            raise ValueError(f"{self.scenario_id}: L3 needs three L2 episodes")
        for phase in self.phases:
            room_ids = tuple(room_id for room_id, _ in phase.counts)
            if len(set(room_ids)) != len(room_ids):
                raise ValueError(f"{self.scenario_id}: duplicate room in phase")
            for room_id, count in phase.counts:
                if room_id not in ROOM_CAPACITIES:
                    raise ValueError(f"unknown room {room_id!r}")
                if count < 0 or count > ROOM_CAPACITIES[room_id]:
                    raise ValueError(f"invalid occupancy for {room_id}: {count}")
        meeting_ids = tuple(meeting.activity_id for meeting in self.meetings)
        if len(set(meeting_ids)) != len(meeting_ids):
            raise ValueError(f"{self.scenario_id}: duplicate meeting ID")
        for meeting in self.meetings:
            if meeting.room_id not in ROOM_CAPACITIES:
                raise ValueError(f"unknown meeting room {meeting.room_id!r}")
            if meeting.attendees > ROOM_CAPACITIES[meeting.room_id]:
                raise ValueError(f"meeting exceeds capacity: {meeting.activity_id}")
            if meeting.attendees <= 0:
                raise ValueError(f"meeting has no attendees: {meeting.activity_id}")
            if not (
                self.preparation_lead_minutes
                <= meeting.start_minute
                < meeting.end_minute
                <= self.duration_minutes
            ):
                raise ValueError(f"invalid meeting time: {meeting.activity_id}")
        for left_index, left in enumerate(self.meetings):
            for right in self.meetings[left_index + 1 :]:
                overlaps = (
                    left.start_minute < right.end_minute
                    and right.start_minute < left.end_minute
                )
                if left.room_id == right.room_id and overlaps:
                    raise ValueError(
                        f"overlapping meetings in {left.room_id}: "
                        f"{left.activity_id}, {right.activity_id}"
                    )
        if self.interaction is not None:
            if not 0 <= self.interaction.minute <= self.duration_minutes:
                raise ValueError(f"{self.scenario_id}: interaction outside scenario")
            if self.interaction.subject_id not in set(meeting_ids):
                raise ValueError(f"{self.scenario_id}: interaction meeting is unknown")
        document_names = tuple(batch.document_name for batch in self.print_batches)
        if len(set(document_names)) != len(document_names):
            raise ValueError(f"{self.scenario_id}: duplicate print document")
        for batch in self.print_batches:
            if batch.ready_by_minute > self.duration_minutes:
                raise ValueError(
                    f"print deadline exceeds scenario: {batch.document_name}"
                )
        if not self.primary_metrics or len(set(self.primary_metrics)) != len(
            self.primary_metrics
        ):
            raise ValueError(f"{self.scenario_id}: primary metrics must be unique")


def day_start(month: int, day: int) -> datetime:
    """Use one deterministic 2026 local date for every catalog entry."""

    return datetime(2026, month, day, 8, 0, tzinfo=CST)
