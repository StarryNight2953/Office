"""Generate agent-observable sensor data from hidden building truth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random
from typing import Mapping, Sequence

from smartoffice.physics.building.models import ZoneState
from smartoffice.physics.building.sensor_api import (
    EXPECTED_UNITS,
    SensorQuality,
    SensorQuantity,
    SensorReading,
)


@dataclass(frozen=True)
class SensorSpec:
    """Static behavior of one simulated sensor.

    ``bias`` and ``noise_standard_deviation`` use the sensor quantity's native
    unit.  Missing samples still consume a sampling opportunity, matching a
    device that sampled on schedule but failed to deliver a usable value.
    """

    sensor_id: str
    zone_id: str
    quantity: SensorQuantity
    sample_interval_seconds: float = 60.0
    latency_seconds: float = 0.0
    noise_standard_deviation: float = 0.0
    bias: float = 0.0
    missing_probability: float = 0.0
    quality: SensorQuality = SensorQuality.GOOD

    def __post_init__(self) -> None:
        if self.sample_interval_seconds <= 0.0:
            raise ValueError("sample interval must be positive")
        if self.latency_seconds < 0.0:
            raise ValueError("sensor latency cannot be negative")
        if self.noise_standard_deviation < 0.0:
            raise ValueError("sensor noise cannot be negative")
        if not 0.0 <= self.missing_probability <= 1.0:
            raise ValueError("missing probability must be in [0, 1]")


@dataclass(frozen=True)
class ObservationSample:
    """Internal audit record; ``truth_value`` must not be exposed to agents."""

    reading: SensorReading
    truth_value: float


class BuildingObservationModel:
    """Deterministically sample hidden truth with configurable imperfections.

    The private random generator prevents sensor noise from depending on other
    scenario randomness.  Its state is included in snapshots for exact replay.
    """

    def __init__(self, sensor_specs: Sequence[SensorSpec], seed: int = 0) -> None:
        ids = [spec.sensor_id for spec in sensor_specs]
        if len(ids) != len(set(ids)):
            raise ValueError("sensor IDs must be unique")
        self.sensor_specs = tuple(sensor_specs)
        self.rng = Random(seed)
        self._last_sample_at: dict[str, datetime] = {}

    def sample(
        self, at_time: datetime, truth_by_zone: Mapping[str, ZoneState]
    ) -> list[ObservationSample]:
        """Sample every due sensor and return internal audit records.

        ``observed_at`` is the physical sampling time.  ``available_at`` adds
        transport/processing latency and is enforced by ``SensorProvider``.
        """

        samples: list[ObservationSample] = []
        for spec in self.sensor_specs:
            truth = truth_by_zone.get(spec.zone_id)
            if truth is None:
                raise ValueError(f"sensor {spec.sensor_id!r} references unknown zone")
            last = self._last_sample_at.get(spec.sensor_id)
            if last is not None:
                elapsed = (at_time - last).total_seconds()
                # A physics tick may be faster than a sensor's sampling rate.
                if elapsed < spec.sample_interval_seconds:
                    continue
            self._last_sample_at[spec.sensor_id] = at_time

            # Record the due time even for a missing sample so the next tick
            # cannot immediately retry and accidentally increase sample rate.
            if self.rng.random() < spec.missing_probability:
                continue
            truth_value = _truth_value(truth, spec.quantity)

            # Bias is repeatable calibration error; Gaussian noise changes on
            # every delivered sample but remains deterministic for a fixed seed.
            value = truth_value + spec.bias
            if spec.noise_standard_deviation:
                value += self.rng.gauss(0.0, spec.noise_standard_deviation)
            reading = SensorReading(
                sensor_id=spec.sensor_id,
                zone_id=spec.zone_id,
                quantity=spec.quantity,
                value=value,
                unit=EXPECTED_UNITS[spec.quantity],
                observed_at=at_time,
                available_at=at_time + timedelta(seconds=spec.latency_seconds),
                quality=spec.quality,
                metadata={"source": "building_observation_model"},
            )
            samples.append(ObservationSample(reading=reading, truth_value=truth_value))
        return samples

    def snapshot(self) -> dict[str, object]:
        """Capture schedule and RNG state required for deterministic replay."""

        return {
            "last_sample_at": dict(self._last_sample_at),
            "rng_state": self.rng.getstate(),
        }

    def restore(self, snapshot: Mapping[str, object]) -> None:
        """Restore a snapshot produced by :meth:`snapshot`."""

        self._last_sample_at = dict(snapshot.get("last_sample_at", {}))  # type: ignore[arg-type]
        rng_state = snapshot.get("rng_state")
        if rng_state is not None:
            self.rng.setstate(rng_state)  # type: ignore[arg-type]


def _truth_value(state: ZoneState, quantity: SensorQuantity) -> float:
    """Map a sensor quantity to hidden truth in one centralized location."""

    if quantity == SensorQuantity.AIR_TEMPERATURE_C:
        return state.air_temperature_c
    if quantity == SensorQuantity.RELATIVE_HUMIDITY_PCT:
        return state.relative_humidity_pct
    if quantity == SensorQuantity.CO2_PPM:
        return state.co2_ppm
    if quantity == SensorQuantity.PM25_UG_M3:
        return state.pm25_ug_m3
    raise ValueError(f"unsupported sensor quantity: {quantity}")
