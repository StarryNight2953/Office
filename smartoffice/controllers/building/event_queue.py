"""Replay-stable scheduling of future Building World events."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from smartoffice.apps.building_world.types import BuildingEventType


@dataclass(frozen=True)
class ScheduledBuildingEvent:
    """A future event envelope, distinct from an already-observed event.

    ``execute_at`` determines when the event becomes a domain fact.  The
    runtime publishes a regular ``BuildingEvent`` only when this envelope is
    consumed, so future knowledge never leaks into the observable event log.
    """

    scheduled_id: str
    execute_at: datetime
    event_type: BuildingEventType
    source: str
    subject_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    parent_event_id: str | None = None
    sequence: int = 0

    def __post_init__(self) -> None:
        if self.execute_at.tzinfo is None:
            raise ValueError("execute_at must be timezone-aware")
        if not self.scheduled_id or not self.source or not self.subject_id:
            raise ValueError("scheduled event identity fields cannot be empty")


class BuildingEventQueue:
    """Priority queue with deterministic same-time ordering and checkpoints."""

    def __init__(self) -> None:
        self._heap: list[tuple[datetime, int, str]] = []
        self._events: dict[str, ScheduledBuildingEvent] = {}
        self._cancelled_ids: set[str] = set()
        self._next_sequence = 0
        self._next_id = 0

    def schedule(
        self,
        *,
        execute_at: datetime,
        event_type: BuildingEventType,
        source: str,
        subject_id: str,
        payload: Mapping[str, Any] | None = None,
        parent_event_id: str | None = None,
        scheduled_id: str | None = None,
    ) -> ScheduledBuildingEvent:
        """Add one future event and return its stable scheduling envelope."""

        if execute_at.tzinfo is None:
            raise ValueError("execute_at must be timezone-aware")
        if scheduled_id is None:
            self._next_id += 1
            scheduled_id = f"scheduled-{self._next_id:04d}"
        elif scheduled_id in self._events or scheduled_id in self._cancelled_ids:
            raise ValueError(f"scheduled event {scheduled_id!r} already exists")
        self._next_sequence += 1
        event = ScheduledBuildingEvent(
            scheduled_id=scheduled_id,
            execute_at=execute_at,
            event_type=event_type,
            source=source,
            subject_id=subject_id,
            payload=dict(payload or {}),
            parent_event_id=parent_event_id,
            sequence=self._next_sequence,
        )
        self._events[event.scheduled_id] = event
        heapq.heappush(
            self._heap,
            (event.execute_at, event.sequence, event.scheduled_id),
        )
        return event

    def cancel(self, scheduled_id: str) -> bool:
        """Cancel an active event; stale heap entries are removed lazily."""

        if scheduled_id not in self._events:
            return False
        del self._events[scheduled_id]
        self._cancelled_ids.add(scheduled_id)
        return True

    def next_time(self) -> datetime | None:
        """Return the next active execution time without consuming the event."""

        self._discard_stale_head()
        return self._heap[0][0] if self._heap else None

    def pop_due(self, through: datetime) -> tuple[ScheduledBuildingEvent, ...]:
        """Consume all active events due at or before ``through``."""

        if through.tzinfo is None:
            raise ValueError("through must be timezone-aware")
        due: list[ScheduledBuildingEvent] = []
        self._discard_stale_head()
        while self._heap and self._heap[0][0] <= through:
            _, _, scheduled_id = heapq.heappop(self._heap)
            event = self._events.pop(scheduled_id, None)
            if event is not None:
                due.append(event)
            self._discard_stale_head()
        return tuple(due)

    def __len__(self) -> int:
        return len(self._events)

    def snapshot(self) -> dict[str, object]:
        """Serialize active events and counters without exposing heap internals."""

        events = sorted(
            self._events.values(),
            key=lambda event: (event.execute_at, event.sequence),
        )
        return {
            "events": [self._event_to_dict(event) for event in events],
            "cancelled_ids": sorted(self._cancelled_ids),
            "next_sequence": self._next_sequence,
            "next_id": self._next_id,
        }

    def restore(self, snapshot: Mapping[str, object]) -> None:
        """Replace queue state from a previous deterministic checkpoint."""

        self._heap = []
        self._events = {}
        self._cancelled_ids = set(snapshot.get("cancelled_ids", []))  # type: ignore[arg-type]
        self._next_sequence = int(snapshot.get("next_sequence", 0))
        self._next_id = int(snapshot.get("next_id", 0))
        for raw in snapshot.get("events", []):  # type: ignore[assignment]
            item = dict(raw)
            event = ScheduledBuildingEvent(
                scheduled_id=str(item["scheduled_id"]),
                execute_at=datetime.fromisoformat(str(item["execute_at"])),
                event_type=BuildingEventType(str(item["event_type"])),
                source=str(item["source"]),
                subject_id=str(item["subject_id"]),
                payload=dict(item.get("payload", {})),
                parent_event_id=item.get("parent_event_id"),
                sequence=int(item["sequence"]),
            )
            if event.scheduled_id in self._events:
                raise ValueError(
                    f"duplicate scheduled event {event.scheduled_id!r} in snapshot"
                )
            self._events[event.scheduled_id] = event
            heapq.heappush(
                self._heap,
                (event.execute_at, event.sequence, event.scheduled_id),
            )

    def _discard_stale_head(self) -> None:
        while self._heap and self._heap[0][2] not in self._events:
            heapq.heappop(self._heap)

    @staticmethod
    def _event_to_dict(event: ScheduledBuildingEvent) -> dict[str, object]:
        return {
            "scheduled_id": event.scheduled_id,
            "execute_at": event.execute_at.isoformat(),
            "event_type": event.event_type.value,
            "source": event.source,
            "subject_id": event.subject_id,
            "payload": dict(event.payload),
            "parent_event_id": event.parent_event_id,
            "sequence": event.sequence,
        }
