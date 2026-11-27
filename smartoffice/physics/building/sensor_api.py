"""Stable sensor boundary for building physics.

Hardware-specific code should translate its payloads into :class:`SensorReading`
objects and implement :class:`SensorProvider`.  The physics engine deliberately
does not know whether a reading came from MQTT, BACnet, Modbus, a REST API, or a
simulated sensor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable, Mapping, Protocol, Sequence, runtime_checkable


class SensorQuantity(str, Enum):
    """Canonical quantities and names used across adapters and simulation."""

    AIR_TEMPERATURE_C = "air_temperature_c"
    RELATIVE_HUMIDITY_PCT = "relative_humidity_pct"
    CO2_PPM = "co2_ppm"
    PM25_UG_M3 = "pm25_ug_m3"


class SensorQuality(str, Enum):
    """Hardware-neutral subset of common building sensor quality codes."""

    GOOD = "good"
    UNCERTAIN = "uncertain"
    BAD = "bad"


EXPECTED_UNITS: dict[SensorQuantity, str] = {
    SensorQuantity.AIR_TEMPERATURE_C: "degC",
    SensorQuantity.RELATIVE_HUMIDITY_PCT: "%RH",
    SensorQuantity.CO2_PPM: "ppm",
    SensorQuantity.PM25_UG_M3: "ug/m3",
}


@dataclass(frozen=True)
class SensorReading:
    """One immutable, unit-qualified sensor observation.

    ``observed_at`` is when the physical sample was taken; ``available_at`` is
    when an agent may receive it after transport or processing latency.
    Sensor readings intentionally contain no simulator truth value.
    """

    sensor_id: str
    zone_id: str
    quantity: SensorQuantity
    value: float
    unit: str
    observed_at: datetime
    available_at: datetime | None = None
    quality: SensorQuality = SensorQuality.GOOD
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Zero-latency adapters can omit available_at without forcing every
        # producer to duplicate observed_at explicitly.
        if self.available_at is None:
            object.__setattr__(self, "available_at", self.observed_at)


@dataclass(frozen=True)
class SensorReadRequest:
    """Pull request scoped by simulation time, zones and quantities."""

    at_time: datetime
    zone_ids: tuple[str, ...]
    quantities: tuple[SensorQuantity, ...] = tuple(SensorQuantity)


@runtime_checkable
class SensorProvider(Protocol):
    """Port implemented by simulated and real sensor adapters."""

    def read(self, request: SensorReadRequest) -> Sequence[SensorReading]: ...


@runtime_checkable
class SensorPublisher(Protocol):
    """Push boundary implemented by hubs and in-memory adapter bridges."""

    def publish(self, reading: SensorReading) -> None: ...

    def publish_many(self, readings: Iterable[SensorReading]) -> None: ...


class InMemorySensorProvider:
    """Small push-to-pull bridge useful for tests and hardware gateways.

    A gateway may call ``publish`` whenever a device message arrives.  The
    orchestrator can then use the normal ``SensorProvider.read`` interface.
    Production gateways may replace this class without changing the engine.
    """

    def __init__(self, readings: Iterable[SensorReading] = ()) -> None:
        self._latest: dict[tuple[str, SensorQuantity], SensorReading] = {}
        self.publish_many(readings)

    def publish(self, reading: SensorReading) -> None:
        """Keep the newest sample for each stable zone/quantity point."""

        key = (reading.zone_id, reading.quantity)
        current = self._latest.get(key)
        if current is None or reading.observed_at >= current.observed_at:
            self._latest[key] = reading

    def publish_many(self, readings: Iterable[SensorReading]) -> None:
        for reading in readings:
            self.publish(reading)

    def read(self, request: SensorReadRequest) -> list[SensorReading]:
        """Return matching readings whose configured latency has elapsed."""

        zone_ids = set(request.zone_ids)
        quantities = set(request.quantities)
        return [
            reading
            for (zone_id, quantity), reading in self._latest.items()
            if zone_id in zone_ids
            and quantity in quantities
            and reading.available_at is not None
            and reading.available_at <= request.at_time
        ]

    def snapshot(self) -> dict[str, object]:
        """Capture the latest values for simulation checkpoint/replay."""

        return {"readings": list(self._latest.values())}

    def restore(self, snapshot: Mapping[str, object]) -> None:
        """Restore readings previously returned by :meth:`snapshot`."""

        self._latest = {}
        self.publish_many(snapshot.get("readings", ()))  # type: ignore[arg-type]
