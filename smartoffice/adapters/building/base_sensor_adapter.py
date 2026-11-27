"""Reusable push-to-pull boundary for real building sensor protocols."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Callable, Iterable, Mapping

from smartoffice.physics.building.sensor_api import (
    EXPECTED_UNITS,
    SensorProvider,
    SensorPublisher,
    SensorQuality,
    SensorQuantity,
    SensorReading,
    SensorReadRequest,
)


UnitConverter = Callable[[float], float]


@dataclass(frozen=True)
class SensorPointMapping:
    """Map one vendor/protocol point into the canonical building schema."""

    point_address: str
    sensor_id: str
    zone_id: str
    quantity: SensorQuantity
    source_unit: str
    canonical_unit: str | None = None
    scale: float = 1.0
    offset: float = 0.0

    def __post_init__(self) -> None:
        canonical = self.canonical_unit or EXPECTED_UNITS[self.quantity]
        if canonical != EXPECTED_UNITS[self.quantity]:
            raise ValueError(
                f"canonical unit for {self.quantity.value} must be "
                f"{EXPECTED_UNITS[self.quantity]!r}"
            )
        object.__setattr__(self, "canonical_unit", canonical)


class PushSensorAdapter(SensorProvider, SensorPublisher):
    """Thread-safe cache shared by MQTT/BACnet/Modbus callback adapters.

    Network clients push normalized readings through :meth:`ingest`; Building
    World pulls a time-scoped view through the standard ``SensorProvider``
    interface.  No network SDK is imported at this stable boundary.
    """

    def __init__(
        self,
        point_mappings: Iterable[SensorPointMapping] = (),
        unit_converters: Mapping[tuple[str, str], UnitConverter] | None = None,
    ) -> None:
        mappings = list(point_mappings)
        addresses = [mapping.point_address for mapping in mappings]
        if len(addresses) != len(set(addresses)):
            raise ValueError("sensor point addresses must be unique")
        self.point_mappings = {
            mapping.point_address: mapping for mapping in mappings
        }
        self.unit_converters = dict(unit_converters or {})
        self._latest: dict[tuple[str, SensorQuantity], SensorReading] = {}
        self._lock = RLock()

    def ingest(
        self,
        point_address: str,
        raw_value: float,
        *,
        observed_at: datetime | None = None,
        received_at: datetime | None = None,
        quality: SensorQuality = SensorQuality.GOOD,
        metadata: Mapping[str, str] | None = None,
    ) -> SensorReading:
        """Normalize one protocol message and publish its canonical reading."""

        mapping = self.point_mappings.get(point_address)
        if mapping is None:
            raise KeyError(f"unknown sensor point address: {point_address!r}")
        received_at = received_at or datetime.now(timezone.utc)
        observed_at = observed_at or received_at
        value = self._convert_value(mapping, float(raw_value))
        reading = SensorReading(
            sensor_id=mapping.sensor_id,
            zone_id=mapping.zone_id,
            quantity=mapping.quantity,
            value=value,
            unit=str(mapping.canonical_unit),
            observed_at=observed_at,
            available_at=received_at,
            quality=quality,
            metadata={
                "point_address": point_address,
                "source_unit": mapping.source_unit,
                **dict(metadata or {}),
            },
        )
        self.publish(reading)
        return reading

    def publish(self, reading: SensorReading) -> None:
        """Atomically retain the newest sample for a zone and quantity."""

        key = (reading.zone_id, reading.quantity)
        with self._lock:
            current = self._latest.get(key)
            if current is None or reading.observed_at >= current.observed_at:
                self._latest[key] = reading

    def publish_many(self, readings: Iterable[SensorReading]) -> None:
        for reading in readings:
            self.publish(reading)

    def read(self, request: SensorReadRequest) -> list[SensorReading]:
        zone_ids = set(request.zone_ids)
        quantities = set(request.quantities)
        with self._lock:
            snapshot = list(self._latest.items())
        return [
            reading
            for (zone_id, quantity), reading in snapshot
            if zone_id in zone_ids
            and quantity in quantities
            and reading.available_at is not None
            and reading.available_at <= request.at_time
        ]

    def _convert_value(
        self, mapping: SensorPointMapping, raw_value: float
    ) -> float:
        """Apply an explicit converter or a declarative affine conversion."""

        target_unit = str(mapping.canonical_unit)
        if mapping.source_unit == target_unit:
            converted = raw_value
        else:
            converter = self.unit_converters.get(
                (mapping.source_unit, target_unit)
            )
            if converter is None:
                raise ValueError(
                    f"no unit converter for {mapping.source_unit!r} -> "
                    f"{target_unit!r}"
                )
            converted = converter(raw_value)
        return converted * mapping.scale + mapping.offset
