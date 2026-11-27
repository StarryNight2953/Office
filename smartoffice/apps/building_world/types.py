"""Canonical domain types shared by Building World applications."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class BuildingRunMode(str, Enum):
    """Select the authoritative observation and actuation boundary."""

    SIMULATION = "simulation"
    SHADOW = "shadow"
    HARDWARE_IN_LOOP = "hardware_in_loop"


class PersonRole(str, Enum):
    LEADER = "leader"
    PROFESSOR = "professor"
    STAFF = "staff"
    STUDENT = "student"
    VISITOR = "visitor"
    UNKNOWN = "unknown"


class MeetingStatus(str, Enum):
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class ReservationStatus(str, Enum):
    ACTIVE = "active"
    RELEASED = "released"
    CANCELLED = "cancelled"


class BuildingEventType(str, Enum):
    SCHEDULE_CREATED = "schedule_created"
    SCHEDULE_CHANGED = "schedule_changed"
    SCHEDULE_CANCELLED = "schedule_cancelled"
    MEETING_PREPARATION_DUE = "meeting_preparation_due"
    MEETING_STARTED = "meeting_started"
    MEETING_ENDED = "meeting_ended"
    ENVIRONMENT_CHECK_DUE = "environment_check_due"
    MEETING_DELAYED = "meeting_delayed"
    MEETING_ENDED_EARLY = "meeting_ended_early"
    RESOURCE_RESERVED = "resource_reserved"
    RESOURCE_RELEASED = "resource_released"
    PERSON_ARRIVED = "person_arrived"
    PERSON_LEFT = "person_left"
    OCCUPANCY_CHANGED = "occupancy_changed"
    DEVICE_STATE_CHANGED = "device_state_changed"
    PRINT_REQUEST_ADDED = "print_request_added"
    PRINT_JOB_SUBMITTED = "print_job_submitted"
    PRINT_JOB_COMPLETED = "print_job_completed"
    DEVICE_FAILED = "device_failed"
    SENSOR_UPDATED = "sensor_updated"
    COMFORT_THRESHOLD_VIOLATED = "comfort_threshold_violated"
    AIR_QUALITY_THRESHOLD_VIOLATED = "air_quality_threshold_violated"
    THRESHOLD_RECOVERED = "threshold_recovered"
    MANUAL_OVERRIDE = "manual_override"


class DeviceType(str, Enum):
    """Hardware-neutral device categories used by Apps and configuration."""

    HVAC = "hvac"
    HUMIDIFIER = "humidifier"
    AIR_PURIFIER = "air_purifier"
    VENTILATION = "ventilation"
    LIGHTING = "lighting"
    PROJECTOR = "projector"
    AUDIO = "audio"
    PRINTER = "printer"
    ACCESS_CONTROL = "access_control"


class DeviceHealth(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    FAILED = "failed"


@dataclass(frozen=True)
class RoomSpec:
    room_id: str
    name: str
    capacity: int
    capabilities: frozenset[str] = field(default_factory=frozenset)
    room_type: str = "general"
    bookable: bool = True

    def __post_init__(self) -> None:
        if not self.room_id:
            raise ValueError("room_id cannot be empty")
        if self.capacity <= 0:
            raise ValueError("room capacity must be positive")
        if not self.room_type:
            raise ValueError("room_type cannot be empty")


@dataclass(frozen=True)
class ZoneSpec:
    """Static spatial subdivision shared by occupancy and building physics."""

    zone_id: str
    room_id: str
    name: str
    purpose: str = "general"
    area_m2: float = 1.0
    height_m: float = 3.0

    def __post_init__(self) -> None:
        if not self.zone_id or not self.room_id:
            raise ValueError("zone_id and room_id cannot be empty")
        if self.area_m2 <= 0.0 or self.height_m <= 0.0:
            raise ValueError("zone area and height must be positive")


@dataclass(frozen=True)
class FunctionalAreaSpec:
    """Human-facing subdivision mapped onto a physical simulation zone.

    Functional areas describe how a room is used without pretending that each
    seating or service area has an independently measured air volume.
    """

    area_id: str
    room_id: str
    name: str
    purpose: str
    zone_id: str
    capacity: int | None = None

    def __post_init__(self) -> None:
        if not self.area_id or not self.room_id or not self.zone_id:
            raise ValueError("functional-area identity and placement cannot be empty")
        if self.capacity is not None and self.capacity <= 0:
            raise ValueError("functional-area capacity must be positive")


@dataclass
class RoomDynamicState:
    """Observable business state not owned by the hidden physics engine."""

    room_id: str
    occupancy_count: int = 0
    pending_task_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.occupancy_count < 0:
            raise ValueError("occupancy_count cannot be negative")


@dataclass(frozen=True)
class DeviceSpec:
    """Static device identity, placement, capability and rated power."""

    device_id: str
    device_type: DeviceType
    room_id: str
    zone_id: str
    functional_area_id: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)
    rated_power_w: float = 0.0
    parameters: Mapping[str, float | str | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.device_id or not self.room_id or not self.zone_id:
            raise ValueError("device identity and placement cannot be empty")
        if self.rated_power_w < 0.0:
            raise ValueError("rated_power_w cannot be negative")


@dataclass
class DeviceState:
    """Mutable command state; physical effects are produced downstream."""

    device_id: str
    power_on: bool = False
    mode: str = "off"
    level: int = 0
    target_temperature_c: float | None = None
    health: DeviceHealth = DeviceHealth.ONLINE
    water_level_pct: float = 100.0
    filter_life_pct: float = 100.0
    # Device-specific controls (for example brightness or audio volume) stay
    # serializable while the common state remains hardware-neutral.
    settings: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0 <= self.level <= 3:
            raise ValueError("device level must be in [0, 3]")
        if not 0.0 <= self.water_level_pct <= 100.0:
            raise ValueError("water_level_pct must be in [0, 100]")
        if not 0.0 <= self.filter_life_pct <= 100.0:
            raise ValueError("filter_life_pct must be in [0, 100]")


@dataclass(frozen=True)
class BuildingAction:
    action_id: str
    device_id: str
    command: str
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    status: str
    event_id: str | None = None
    reason: str | None = None


@dataclass
class PersonState:
    person_id: str
    name: str
    role: PersonRole
    on_campus: bool = False
    current_zone_id: str | None = None


@dataclass
class ScheduleEntry:
    meeting_id: str
    room_id: str
    organizer_id: str
    participant_ids: tuple[str, ...]
    start_at: datetime
    end_at: datetime
    expected_attendees: int
    status: MeetingStatus = MeetingStatus.CONFIRMED
    title: str = ""
    required_capabilities: tuple[str, ...] = ()
    reservation_id: str | None = None

    def __post_init__(self) -> None:
        if self.start_at >= self.end_at:
            raise ValueError("meeting start_at must be before end_at")
        if self.expected_attendees <= 0:
            raise ValueError("expected_attendees must be positive")


@dataclass
class ResourceReservation:
    reservation_id: str
    resource_id: str
    owner_id: str
    start_at: datetime
    end_at: datetime
    status: ReservationStatus = ReservationStatus.ACTIVE


@dataclass(frozen=True)
class BuildingEvent:
    event_id: str
    event_type: BuildingEventType
    occurred_at: datetime
    source: str
    subject_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    parent_event_id: str | None = None


def time_ranges_overlap(
    start_a: datetime,
    end_a: datetime,
    start_b: datetime,
    end_b: datetime,
) -> bool:
    """Return whether two half-open intervals ``[start, end)`` overlap."""

    return start_a < end_b and start_b < end_a
