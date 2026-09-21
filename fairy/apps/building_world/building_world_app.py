"""Canonical mutable state store for Building World."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fairy.apps.app import App
from fairy.apps.building_world.types import (
    BuildingEvent,
    BuildingEventType,
    DeviceHealth,
    DeviceSpec,
    DeviceState,
    DeviceType,
    FunctionalAreaSpec,
    MeetingStatus,
    PersonRole,
    PersonState,
    ReservationStatus,
    ResourceReservation,
    RoomDynamicState,
    RoomSpec,
    ScheduleEntry,
    ZoneSpec,
)
from fairy.tool_utils import OperationType, app_tool, data_tool
from fairy.types import event_registered
from fairy.utils.type_utils import type_check


class BuildingWorldApp(App):
    """Single source of truth for rooms, people, meetings and reservations."""

    def __init__(self) -> None:
        super().__init__(name="BuildingWorldApp")
        self.rooms: dict[str, RoomSpec] = {}
        self.room_states: dict[str, RoomDynamicState] = {}
        self.zones: dict[str, ZoneSpec] = {}
        self.functional_areas: dict[str, FunctionalAreaSpec] = {}
        self.devices: dict[str, DeviceSpec] = {}
        self.device_states: dict[str, DeviceState] = {}
        self.people: dict[str, PersonState] = {}
        self.schedule: dict[str, ScheduleEntry] = {}
        self.reservations: dict[str, ResourceReservation] = {}
        self.events: list[BuildingEvent] = []
        self._id_counters: dict[str, int] = {}

    def next_id(self, prefix: str) -> str:
        """Generate replay-stable IDs without wall-clock time or UUIDs."""

        value = self._id_counters.get(prefix, 0) + 1
        self._id_counters[prefix] = value
        return f"{prefix}-{value:04d}"

    def resolve_room_id(self, room_id: str) -> str | None:
        """Resolve a user-facing room ID to its canonical stored spelling."""

        if room_id in self.rooms:
            return room_id
        folded = room_id.casefold()
        matches = [
            candidate
            for candidate in self.rooms
            if candidate.casefold() == folded
        ]
        return matches[0] if len(matches) == 1 else None

    def add_room(self, room: RoomSpec) -> None:
        if room.room_id in self.rooms:
            raise ValueError(f"room {room.room_id!r} already exists")
        self.rooms[room.room_id] = room
        self.room_states[room.room_id] = RoomDynamicState(room_id=room.room_id)

    def add_zone(self, zone: ZoneSpec) -> None:
        if zone.zone_id in self.zones:
            raise ValueError(f"zone {zone.zone_id!r} already exists")
        if zone.room_id not in self.rooms:
            raise ValueError(f"zone references unknown room {zone.room_id!r}")
        self.zones[zone.zone_id] = zone

    def add_functional_area(self, area: FunctionalAreaSpec) -> None:
        """Register a usage area that maps to an existing physical zone."""

        if area.area_id in self.functional_areas:
            raise ValueError(f"functional area {area.area_id!r} already exists")
        if area.room_id not in self.rooms:
            raise ValueError(
                f"functional area references unknown room {area.room_id!r}"
            )
        zone = self.zones.get(area.zone_id)
        if zone is None or zone.room_id != area.room_id:
            raise ValueError("functional area must reference a zone in the same room")
        self.functional_areas[area.area_id] = area

    def add_device(self, device: DeviceSpec, state: DeviceState | None = None) -> None:
        if device.device_id in self.devices:
            raise ValueError(f"device {device.device_id!r} already exists")
        if device.room_id not in self.rooms:
            raise ValueError(f"device references unknown room {device.room_id!r}")
        if device.zone_id not in self.zones:
            raise ValueError(f"device references unknown zone {device.zone_id!r}")
        if device.functional_area_id is not None:
            area = self.functional_areas.get(device.functional_area_id)
            if area is None or area.room_id != device.room_id:
                raise ValueError("device references an unknown functional area")
        if state is not None and state.device_id != device.device_id:
            raise ValueError("device state ID must match device spec ID")
        self.devices[device.device_id] = device
        self.device_states[device.device_id] = state or DeviceState(device.device_id)

    def add_person(self, person: PersonState) -> None:
        if person.person_id in self.people:
            raise ValueError(f"person {person.person_id!r} already exists")
        self.people[person.person_id] = person

    def add_schedule_entry(self, entry: ScheduleEntry) -> None:
        if entry.meeting_id in self.schedule:
            raise ValueError(f"meeting {entry.meeting_id!r} already exists")
        self.schedule[entry.meeting_id] = entry

    def publish_building_event(
        self,
        event_type: BuildingEventType,
        *,
        source: str,
        subject_id: str,
        payload: dict[str, Any] | None = None,
        parent_event_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> BuildingEvent:
        event = BuildingEvent(
            event_id=self.next_id("event"),
            event_type=event_type,
            occurred_at=occurred_at
            or datetime.fromtimestamp(self.time_manager.time(), tz=timezone.utc),
            source=source,
            subject_id=subject_id,
            payload=dict(payload or {}),
            parent_event_id=parent_event_id,
        )
        self.events.append(event)
        self.add_event(event)
        return event

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_building_overview(self) -> dict[str, Any]:
        """Return the observable room, person and confirmed-meeting overview."""

        return {
            "rooms": [_room_to_dict(room) for room in self.rooms.values()],
            "zones": [_zone_to_dict(zone) for zone in self.zones.values()],
            "functional_areas": [
                _functional_area_to_dict(area)
                for area in self.functional_areas.values()
            ],
            "devices": [
                _device_to_dict(device, self.device_states[device.device_id])
                for device in self.devices.values()
            ],
            "people": [_person_to_dict(person) for person in self.people.values()],
            "meetings": [
                _meeting_to_dict(meeting)
                for meeting in self.schedule.values()
                if meeting.status == MeetingStatus.CONFIRMED
            ],
        }

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_recent_building_events(
        self, limit: Annotated[int, {"minimum": 1, "maximum": 200}] = 20
    ) -> dict[str, Any]:
        """Return only events that have already occurred in the simulation.

        Scheduled future events remain inside the Runtime queue and therefore
        cannot leak through this Agent-facing reconciliation tool.
        """

        if not 1 <= limit <= 100:
            return {"error": "limit must be between 1 and 100"}
        events = self.events[-limit:]
        return {
            "events": [_event_to_dict(event) for event in events],
            "latest_event_id": events[-1].event_id if events else None,
        }

    def get_state(self) -> dict[str, Any]:
        return self.snapshot()

    def load_state(self, state_dict: dict[str, Any]) -> None:
        self.restore(state_dict)

    def reset(self) -> None:
        """Return the canonical store to an empty pre-fixture state."""

        super().reset()
        self.rooms = {}
        self.room_states = {}
        self.zones = {}
        self.functional_areas = {}
        self.devices = {}
        self.device_states = {}
        self.people = {}
        self.schedule = {}
        self.reservations = {}
        self.events = []
        self._id_counters = {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "rooms": [_room_to_dict(room) for room in self.rooms.values()],
            "room_states": [
                {
                    "room_id": state.room_id,
                    "occupancy_count": state.occupancy_count,
                    "pending_task_ids": list(state.pending_task_ids),
                }
                for state in self.room_states.values()
            ],
            "zones": [_zone_to_dict(zone) for zone in self.zones.values()],
            "functional_areas": [
                _functional_area_to_dict(area)
                for area in self.functional_areas.values()
            ],
            "devices": [
                _device_to_dict(device, self.device_states[device.device_id])
                for device in self.devices.values()
            ],
            "people": [_person_to_dict(person) for person in self.people.values()],
            "schedule": [_meeting_to_dict(entry) for entry in self.schedule.values()],
            "reservations": [
                _reservation_to_dict(item) for item in self.reservations.values()
            ],
            "events": [_event_to_dict(event) for event in self.events],
            "id_counters": dict(self._id_counters),
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.rooms = {
            item["room_id"]: RoomSpec(
                room_id=item["room_id"],
                name=item["name"],
                capacity=int(item["capacity"]),
                capabilities=frozenset(item.get("capabilities", [])),
                room_type=item.get("room_type", "general"),
                bookable=bool(item.get("bookable", True)),
            )
            for item in snapshot.get("rooms", [])
        }
        raw_room_states = {
            item["room_id"]: item for item in snapshot.get("room_states", [])
        }
        self.room_states = {
            room_id: RoomDynamicState(
                room_id=room_id,
                occupancy_count=int(
                    raw_room_states.get(room_id, {}).get("occupancy_count", 0)
                ),
                pending_task_ids=list(
                    raw_room_states.get(room_id, {}).get("pending_task_ids", [])
                ),
            )
            for room_id in self.rooms
        }
        self.zones = {
            item["zone_id"]: ZoneSpec(
                zone_id=item["zone_id"],
                room_id=item["room_id"],
                name=item["name"],
                purpose=item.get("purpose", "general"),
                area_m2=float(item.get("area_m2", 1.0)),
                height_m=float(item.get("height_m", 3.0)),
            )
            for item in snapshot.get("zones", [])
        }
        self.functional_areas = {
            item["area_id"]: FunctionalAreaSpec(
                area_id=item["area_id"],
                room_id=item["room_id"],
                name=item["name"],
                purpose=item["purpose"],
                zone_id=item["zone_id"],
                capacity=item.get("capacity"),
            )
            for item in snapshot.get("functional_areas", [])
        }
        self.devices = {}
        self.device_states = {}
        for item in snapshot.get("devices", []):
            spec = DeviceSpec(
                device_id=item["device_id"],
                device_type=DeviceType(item["device_type"]),
                room_id=item["room_id"],
                zone_id=item["zone_id"],
                functional_area_id=item.get("functional_area_id"),
                capabilities=frozenset(item.get("capabilities", [])),
                rated_power_w=float(item.get("rated_power_w", 0.0)),
                parameters=dict(item.get("parameters", {})),
            )
            state = DeviceState(
                device_id=spec.device_id,
                power_on=bool(item.get("power_on", False)),
                mode=item.get("mode", "off"),
                level=int(item.get("level", 0)),
                target_temperature_c=item.get("target_temperature_c"),
                health=DeviceHealth(item.get("health", DeviceHealth.ONLINE.value)),
                water_level_pct=float(item.get("water_level_pct", 100.0)),
                filter_life_pct=float(item.get("filter_life_pct", 100.0)),
                settings=dict(item.get("settings", {})),
            )
            self.devices[spec.device_id] = spec
            self.device_states[spec.device_id] = state
        self.people = {
            item["person_id"]: PersonState(
                person_id=item["person_id"],
                name=item["name"],
                role=PersonRole(item["role"]),
                on_campus=bool(item.get("on_campus", False)),
                current_zone_id=item.get("current_zone_id"),
            )
            for item in snapshot.get("people", [])
        }
        self.schedule = {
            item["meeting_id"]: _meeting_from_dict(item)
            for item in snapshot.get("schedule", [])
        }
        self.reservations = {
            item["reservation_id"]: ResourceReservation(
                reservation_id=item["reservation_id"],
                resource_id=item["resource_id"],
                owner_id=item["owner_id"],
                start_at=datetime.fromisoformat(item["start_at"]),
                end_at=datetime.fromisoformat(item["end_at"]),
                status=ReservationStatus(item["status"]),
            )
            for item in snapshot.get("reservations", [])
        }
        self.events = [
            BuildingEvent(
                event_id=item["event_id"],
                event_type=BuildingEventType(item["event_type"]),
                occurred_at=datetime.fromisoformat(item["occurred_at"]),
                source=item["source"],
                subject_id=item["subject_id"],
                payload=dict(item.get("payload", {})),
                parent_event_id=item.get("parent_event_id"),
            )
            for item in snapshot.get("events", [])
        ]
        self._id_counters = {
            str(key): int(value)
            for key, value in snapshot.get("id_counters", {}).items()
        }


def _room_to_dict(room: RoomSpec) -> dict[str, Any]:
    return {
        "room_id": room.room_id,
        "name": room.name,
        "capacity": room.capacity,
        "capabilities": sorted(room.capabilities),
        "room_type": room.room_type,
        "bookable": room.bookable,
    }


def _zone_to_dict(zone: ZoneSpec) -> dict[str, Any]:
    return {
        "zone_id": zone.zone_id,
        "room_id": zone.room_id,
        "name": zone.name,
        "purpose": zone.purpose,
        "area_m2": zone.area_m2,
        "height_m": zone.height_m,
    }


def _functional_area_to_dict(area: FunctionalAreaSpec) -> dict[str, Any]:
    return {
        "area_id": area.area_id,
        "room_id": area.room_id,
        "name": area.name,
        "purpose": area.purpose,
        "zone_id": area.zone_id,
        "capacity": area.capacity,
    }


def _device_to_dict(device: DeviceSpec, state: DeviceState) -> dict[str, Any]:
    return {
        "device_id": device.device_id,
        "device_type": device.device_type.value,
        "room_id": device.room_id,
        "zone_id": device.zone_id,
        "functional_area_id": device.functional_area_id,
        "capabilities": sorted(device.capabilities),
        "rated_power_w": device.rated_power_w,
        "parameters": dict(device.parameters),
        "power_on": state.power_on,
        "mode": state.mode,
        "level": state.level,
        "target_temperature_c": state.target_temperature_c,
        "health": state.health.value,
        "water_level_pct": state.water_level_pct,
        "filter_life_pct": state.filter_life_pct,
        "settings": dict(state.settings),
    }


def _person_to_dict(person: PersonState) -> dict[str, Any]:
    return {
        "person_id": person.person_id,
        "name": person.name,
        "role": person.role.value,
        "on_campus": person.on_campus,
        "current_zone_id": person.current_zone_id,
    }


def _meeting_to_dict(entry: ScheduleEntry) -> dict[str, Any]:
    return {
        "meeting_id": entry.meeting_id,
        "room_id": entry.room_id,
        "organizer_id": entry.organizer_id,
        "participant_ids": list(entry.participant_ids),
        "start_at": entry.start_at.isoformat(),
        "end_at": entry.end_at.isoformat(),
        "expected_attendees": entry.expected_attendees,
        "status": entry.status.value,
        "title": entry.title,
        "required_capabilities": list(entry.required_capabilities),
        "reservation_id": entry.reservation_id,
    }


def _meeting_from_dict(item: dict[str, Any]) -> ScheduleEntry:
    return ScheduleEntry(
        meeting_id=item["meeting_id"],
        room_id=item["room_id"],
        organizer_id=item["organizer_id"],
        participant_ids=tuple(item.get("participant_ids", [])),
        start_at=datetime.fromisoformat(item["start_at"]),
        end_at=datetime.fromisoformat(item["end_at"]),
        expected_attendees=int(item["expected_attendees"]),
        status=MeetingStatus(item["status"]),
        title=item.get("title", ""),
        required_capabilities=tuple(item.get("required_capabilities", [])),
        reservation_id=item.get("reservation_id"),
    )


def _reservation_to_dict(item: ResourceReservation) -> dict[str, Any]:
    return {
        "reservation_id": item.reservation_id,
        "resource_id": item.resource_id,
        "owner_id": item.owner_id,
        "start_at": item.start_at.isoformat(),
        "end_at": item.end_at.isoformat(),
        "status": item.status.value,
    }


def _event_to_dict(event: BuildingEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "occurred_at": event.occurred_at.isoformat(),
        "source": event.source,
        "subject_id": event.subject_id,
        "payload": dict(event.payload),
        "parent_event_id": event.parent_event_id,
    }
