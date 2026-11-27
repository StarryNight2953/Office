"""Aggregate simulated and real sensor providers behind one read boundary."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from smartoffice.physics.building.sensor_api import (
    InMemorySensorProvider,
    SensorProvider,
    SensorPublisher,
    SensorQuality,
    SensorQuantity,
    SensorReading,
    SensorReadRequest,
)


@dataclass(frozen=True)
class _ProviderRegistration:
    name: str
    provider: SensorProvider
    priority: int


class SensorHub(SensorProvider, SensorPublisher):
    """Merge multiple providers using explicit priority and sample recency.

    Higher priority wins for the same ``zone_id + quantity``.  Recency only
    breaks ties within equal priority, preventing a simulated sample from
    silently replacing an authoritative real sensor in shadow/HIL mode.
    """

    def __init__(
        self,
        zone_ids: Iterable[str],
        *,
        published_priority: int = 0,
    ) -> None:
        self.zone_ids = tuple(dict.fromkeys(zone_ids))
        if not self.zone_ids:
            raise ValueError("SensorHub requires at least one known zone")
        self._published = InMemorySensorProvider()
        self._published_priority = int(published_priority)
        self._providers: list[_ProviderRegistration] = []

    def register_provider(
        self, name: str, provider: SensorProvider, *, priority: int = 0
    ) -> None:
        """Register one uniquely named provider at an explicit precedence."""

        if not name:
            raise ValueError("provider name cannot be empty")
        if provider is self:
            raise ValueError("SensorHub cannot register itself as a provider")
        if any(registration.name == name for registration in self._providers):
            raise ValueError(f"sensor provider {name!r} is already registered")
        self._providers.append(
            _ProviderRegistration(name=name, provider=provider, priority=priority)
        )

    def publish(self, reading: SensorReading) -> None:
        """Accept generated readings, normally from simulated observation."""

        if reading.zone_id not in self.zone_ids:
            raise ValueError(f"reading references unknown zone {reading.zone_id!r}")
        self._published.publish(reading)

    def publish_many(self, readings: Iterable[SensorReading]) -> None:
        for reading in readings:
            self.publish(reading)

    def read(self, request: SensorReadRequest) -> list[SensorReading]:
        unknown = set(request.zone_ids) - set(self.zone_ids)
        if unknown:
            raise ValueError(
                f"sensor request contains unknown zones: {sorted(unknown)}"
            )

        # Candidate tuple is (priority, provider name, reading).  Provider name
        # is retained for deterministic tie-breaking and health diagnostics.
        candidates = self._read_candidates(request)

        selected: dict[
            tuple[str, SensorQuantity], tuple[int, str, SensorReading]
        ] = {}
        for candidate in candidates:
            priority, provider_name, reading = candidate
            key = (reading.zone_id, reading.quantity)
            current = selected.get(key)
            if current is None or _candidate_is_better(candidate, current):
                selected[key] = (priority, provider_name, reading)
        return [
            selected[key][2]
            for key in sorted(selected, key=lambda item: (item[0], item[1].value))
        ]

    def read_by_source(
        self, request: SensorReadRequest
    ) -> dict[str, list[SensorReading]]:
        """Return unmerged readings for shadow-mode model/error comparison."""

        unknown = set(request.zone_ids) - set(self.zone_ids)
        if unknown:
            raise ValueError(
                f"sensor request contains unknown zones: {sorted(unknown)}"
            )
        grouped: dict[str, list[SensorReading]] = {}
        for _, provider_name, reading in self._read_candidates(request):
            grouped.setdefault(provider_name, []).append(reading)
        return grouped

    def read_all(
        self,
        at_time: datetime,
        quantities: Sequence[SensorQuantity] = tuple(SensorQuantity),
    ) -> list[SensorReading]:
        return self.read(
            self.read_all_request(at_time, quantities)
        )

    def read_all_request(
        self,
        at_time: datetime,
        quantities: Sequence[SensorQuantity] = tuple(SensorQuantity),
    ) -> SensorReadRequest:
        """Build the canonical all-zone request used by reads and evaluators."""

        return SensorReadRequest(
            at_time=at_time,
            zone_ids=self.zone_ids,
            quantities=tuple(quantities),
        )

    def health(self, at_time: datetime, stale_after_seconds: float) -> list[dict]:
        """Return structured freshness/quality status for currently visible points."""

        if stale_after_seconds < 0.0:
            raise ValueError("stale_after_seconds cannot be negative")
        readings = self.read_all(at_time)
        health_rows: list[dict] = []
        for reading in readings:
            try:
                age_seconds = max(
                    0.0, (at_time - reading.observed_at).total_seconds()
                )
            except TypeError:
                age_seconds = float("inf")
            if reading.quality == SensorQuality.BAD:
                status = "bad"
            elif age_seconds > stale_after_seconds:
                status = "stale"
            elif reading.quality == SensorQuality.UNCERTAIN:
                status = "uncertain"
            else:
                status = "healthy"
            health_rows.append(
                {
                    "sensor_id": reading.sensor_id,
                    "zone_id": reading.zone_id,
                    "quantity": reading.quantity.value,
                    "status": status,
                    "age_seconds": age_seconds,
                    "quality": reading.quality.value,
                }
            )
        return health_rows

    def snapshot(self) -> dict[str, object]:
        """Capture internally published simulation values.

        External providers remain authoritative for their own persistence; a
        pure simulation replay only needs this internal cache.
        """

        return {"published": self._published.snapshot()}

    def restore(self, snapshot: dict[str, object]) -> None:
        published = snapshot.get("published", {})
        if not isinstance(published, dict):
            raise ValueError("invalid SensorHub snapshot")
        self._published.restore(published)

    def _read_candidates(
        self, request: SensorReadRequest
    ) -> list[tuple[int, str, SensorReading]]:
        candidates: list[tuple[int, str, SensorReading]] = [
            (self._published_priority, "published", reading)
            for reading in self._published.read(request)
        ]
        for registration in self._providers:
            candidates.extend(
                (registration.priority, registration.name, reading)
                for reading in registration.provider.read(request)
            )
        return candidates


def _candidate_is_better(
    candidate: tuple[int, str, SensorReading],
    current: tuple[int, str, SensorReading],
) -> bool:
    candidate_priority, candidate_name, candidate_reading = candidate
    current_priority, current_name, current_reading = current
    if candidate_priority != current_priority:
        return candidate_priority > current_priority
    if candidate_reading.observed_at != current_reading.observed_at:
        return candidate_reading.observed_at > current_reading.observed_at
    return candidate_name < current_name
