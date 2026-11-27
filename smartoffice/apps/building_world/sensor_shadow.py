"""Shadow-mode comparison between authoritative and simulated sensor sources."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import sqrt
from typing import Any

from smartoffice.apps.building_world.sensor_hub import SensorHub
from smartoffice.physics.building.sensor_api import SensorQuantity, SensorReadRequest


@dataclass(frozen=True)
class SensorShadowComparison:
    """One time-aligned comparison for a zone and physical quantity."""

    compared_at: datetime
    zone_id: str
    quantity: SensorQuantity
    reference_sensor_id: str
    candidate_sensor_id: str
    reference_value: float
    candidate_value: float
    signed_error: float
    time_offset_seconds: float

    @property
    def absolute_error(self) -> float:
        return abs(self.signed_error)


class SensorShadowEvaluator:
    """Accumulate deterministic bias, MAE and RMSE without changing truth."""

    def __init__(
        self,
        *,
        reference_source: str,
        candidate_source: str = "published",
        max_alignment_seconds: float = 300.0,
    ) -> None:
        if not reference_source or not candidate_source:
            raise ValueError("shadow source names cannot be empty")
        if reference_source == candidate_source:
            raise ValueError("shadow sources must be different")
        if max_alignment_seconds < 0:
            raise ValueError("shadow alignment window cannot be negative")
        self.reference_source = reference_source
        self.candidate_source = candidate_source
        self.max_alignment_seconds = float(max_alignment_seconds)
        self.comparisons: list[SensorShadowComparison] = []
        self._reference_watermarks: dict[tuple[str, SensorQuantity], datetime] = {}

    def sample(
        self,
        hub: SensorHub,
        request: SensorReadRequest,
    ) -> tuple[SensorShadowComparison, ...]:
        """Compare the latest available matching points exactly once."""

        by_source = hub.read_by_source(request)
        reference = {
            (reading.zone_id, reading.quantity): reading
            for reading in by_source.get(self.reference_source, ())
        }
        candidate = {
            (reading.zone_id, reading.quantity): reading
            for reading in by_source.get(self.candidate_source, ())
        }
        created: list[SensorShadowComparison] = []
        for key in sorted(
            reference.keys() & candidate.keys(),
            key=lambda item: (item[0], item[1].value),
        ):
            reference_reading = reference[key]
            candidate_reading = candidate[key]
            if reference_reading.unit != candidate_reading.unit:
                raise ValueError(
                    f"shadow units differ for {key}: {reference_reading.unit!r} "
                    f"!= {candidate_reading.unit!r}"
                )
            time_offset_seconds = (
                candidate_reading.observed_at - reference_reading.observed_at
            ).total_seconds()
            if abs(time_offset_seconds) > self.max_alignment_seconds:
                continue
            # A real/reference sample contributes at most once. The simulated
            # candidate may update faster, but repeatedly scoring one stale
            # reference would bias MAE toward long network gaps.
            if self._reference_watermarks.get(key) == reference_reading.observed_at:
                continue
            self._reference_watermarks[key] = reference_reading.observed_at
            comparison = SensorShadowComparison(
                compared_at=request.at_time,
                zone_id=key[0],
                quantity=key[1],
                reference_sensor_id=reference_reading.sensor_id,
                candidate_sensor_id=candidate_reading.sensor_id,
                reference_value=reference_reading.value,
                candidate_value=candidate_reading.value,
                signed_error=candidate_reading.value - reference_reading.value,
                time_offset_seconds=time_offset_seconds,
            )
            self.comparisons.append(comparison)
            created.append(comparison)
        return tuple(created)

    def summary(self) -> dict[str, Any]:
        """Return per-zone/quantity and overall calibration metrics."""

        groups: dict[tuple[str, SensorQuantity], list[float]] = defaultdict(list)
        for comparison in self.comparisons:
            groups[(comparison.zone_id, comparison.quantity)].append(
                comparison.signed_error
            )
        rows = [
            _metric_row(zone_id, quantity, errors)
            for (zone_id, quantity), errors in sorted(
                groups.items(), key=lambda item: (item[0][0], item[0][1].value)
            )
        ]
        return {
            "reference_source": self.reference_source,
            "candidate_source": self.candidate_source,
            "max_alignment_seconds": self.max_alignment_seconds,
            "sample_count": len(self.comparisons),
            # Errors with different units must never be combined into one MAE.
            "point_count": len(rows),
            "by_point": rows,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "reference_source": self.reference_source,
            "candidate_source": self.candidate_source,
            "max_alignment_seconds": self.max_alignment_seconds,
            "comparisons": [
                {
                    "compared_at": item.compared_at,
                    "zone_id": item.zone_id,
                    "quantity": item.quantity.value,
                    "reference_sensor_id": item.reference_sensor_id,
                    "candidate_sensor_id": item.candidate_sensor_id,
                    "reference_value": item.reference_value,
                    "candidate_value": item.candidate_value,
                    "signed_error": item.signed_error,
                    "time_offset_seconds": item.time_offset_seconds,
                }
                for item in self.comparisons
            ],
            "reference_watermarks": [
                {
                    "zone_id": zone_id,
                    "quantity": quantity.value,
                    "reference_observed_at": observed_at,
                }
                for (zone_id, quantity), observed_at in (
                    self._reference_watermarks.items()
                )
            ],
        }

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        if snapshot.get("reference_source") != self.reference_source:
            raise ValueError("shadow snapshot reference source does not match")
        if snapshot.get("candidate_source") != self.candidate_source:
            raise ValueError("shadow snapshot candidate source does not match")
        if float(snapshot.get("max_alignment_seconds", 300.0)) != (
            self.max_alignment_seconds
        ):
            raise ValueError("shadow snapshot alignment window does not match")
        self.comparisons = [
            SensorShadowComparison(
                compared_at=item["compared_at"],
                zone_id=str(item["zone_id"]),
                quantity=SensorQuantity(item["quantity"]),
                reference_sensor_id=str(item["reference_sensor_id"]),
                candidate_sensor_id=str(item["candidate_sensor_id"]),
                reference_value=float(item["reference_value"]),
                candidate_value=float(item["candidate_value"]),
                signed_error=float(item["signed_error"]),
                time_offset_seconds=float(item.get("time_offset_seconds", 0.0)),
            )
            for item in snapshot.get("comparisons", [])
        ]
        self._reference_watermarks = {
            (str(item["zone_id"]), SensorQuantity(item["quantity"])): item[
                "reference_observed_at"
            ]
            for item in snapshot.get("reference_watermarks", [])
        }


def _metric_row(
    zone_id: str,
    quantity: SensorQuantity,
    errors: list[float],
) -> dict[str, Any]:
    return {
        "zone_id": zone_id,
        "quantity": quantity.value,
        **_aggregate_errors(errors),
    }


def _aggregate_errors(errors: list[float]) -> dict[str, float | int | None]:
    if not errors:
        return {
            "count": 0,
            "bias": None,
            "mae": None,
            "rmse": None,
            "max_absolute_error": None,
        }
    count = len(errors)
    return {
        "count": count,
        "bias": sum(errors) / count,
        "mae": sum(abs(error) for error in errors) / count,
        "rmse": sqrt(sum(error * error for error in errors) / count),
        "max_absolute_error": max(abs(error) for error in errors),
    }
