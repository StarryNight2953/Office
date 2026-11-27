"""MQTT callback adapter without a hard dependency on an MQTT client SDK."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Mapping

from smartoffice.adapters.building.base_sensor_adapter import PushSensorAdapter
from smartoffice.physics.building.sensor_api import SensorQuality, SensorReading


PayloadDecoder = Callable[[bytes | str], Mapping[str, Any]]


class MqttSensorAdapter(PushSensorAdapter):
    """Translate MQTT messages into canonical readings.

    A concrete paho-mqtt, asyncio-mqtt or gateway callback should call
    :meth:`on_message`.  Keeping connection management outside this class
    makes the adapter testable and avoids selecting a deployment SDK here.
    """

    def __init__(
        self,
        *args: Any,
        payload_decoder: PayloadDecoder | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.payload_decoder = payload_decoder or _default_json_decoder

    def on_message(
        self,
        topic: str,
        payload: bytes | str,
        *,
        received_at: datetime | None = None,
    ) -> SensorReading:
        data = self.payload_decoder(payload)
        observed_at = _parse_datetime(data.get("observed_at"))
        quality = _parse_quality(data.get("quality"))
        return self.ingest(
            topic,
            float(data["value"]),
            observed_at=observed_at,
            received_at=received_at,
            quality=quality,
            metadata={"source": "mqtt", "topic": topic},
        )


def _default_json_decoder(payload: bytes | str) -> Mapping[str, Any]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    decoded = json.loads(payload)
    if not isinstance(decoded, dict) or "value" not in decoded:
        raise ValueError("MQTT sensor payload must be an object containing 'value'")
    return decoded


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _parse_quality(value: Any) -> SensorQuality:
    if value in (None, ""):
        return SensorQuality.GOOD
    try:
        return SensorQuality(str(value).lower())
    except ValueError:
        return SensorQuality.UNCERTAIN
