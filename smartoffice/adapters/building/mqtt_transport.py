"""MQTT network transport for building sensor readings.

The transport is intentionally separate from the physics package: physics
produces :class:`SensorReading` objects, while this module owns broker
connections, MQTT serialization, subscriptions and reconnect behavior.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event
from typing import Any, Protocol, Self

from smartoffice.adapters.building.mqtt_sensor_adapter import MqttSensorAdapter
from smartoffice.physics.building.sensor_api import SensorReading


class MqttClient(Protocol):
    """Small subset of the paho client used by the transport classes."""

    on_connect: Any
    on_message: Any

    def connect(self, host: str, port: int, keepalive: int) -> Any: ...

    def disconnect(self) -> Any: ...

    def loop_start(self) -> Any: ...

    def loop_stop(self) -> Any: ...

    def publish(
        self, topic: str, payload: str, qos: int = 0, retain: bool = False
    ) -> MqttPublishResult: ...

    def subscribe(self, topic: str, qos: int = 0) -> Any: ...

    def username_pw_set(self, username: str, password: str | None = None) -> Any: ...

    def tls_set(self, **kwargs: Any) -> Any: ...


class MqttMessage(Protocol):
    """Incoming message fields shared by supported paho callback versions."""

    topic: str
    payload: bytes


class MqttPublishResult(Protocol):
    """Delivery handle returned by paho's ``publish`` method."""

    rc: int

    def wait_for_publish(self, timeout: float | None = None) -> None: ...

    def is_published(self) -> bool: ...


@dataclass(frozen=True)
class MqttConnectionSettings:
    """Connection and delivery settings shared by publishers and subscribers."""

    host: str = "localhost"
    port: int = 1883
    keepalive_seconds: int = 60
    qos: int = 1
    retain: bool = False
    client_id: str = ""
    username: str | None = None
    password: str | None = None
    use_tls: bool = False

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("MQTT host cannot be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("MQTT port must be in [1, 65535]")
        if self.keepalive_seconds <= 0:
            raise ValueError("MQTT keepalive must be positive")
        if self.qos not in (0, 1, 2):
            raise ValueError("MQTT QoS must be 0, 1 or 2")


def encode_sensor_reading(reading: SensorReading) -> str:
    """Serialize one canonical reading using the adapter's JSON contract."""

    return json.dumps(
        {
            "sensor_id": reading.sensor_id,
            "zone_id": reading.zone_id,
            "quantity": reading.quantity.value,
            "value": reading.value,
            "unit": reading.unit,
            "observed_at": reading.observed_at.isoformat(),
            "quality": reading.quality.value,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class MqttSensorPublisher:
    """Publish canonical readings to one exact MQTT topic per sensor."""

    def __init__(
        self,
        settings: MqttConnectionSettings,
        topic_by_sensor_id: Mapping[str, str],
        *,
        client: MqttClient | None = None,
        connection_timeout_seconds: float = 10.0,
        publish_timeout_seconds: float = 10.0,
    ) -> None:
        if not topic_by_sensor_id:
            raise ValueError("at least one MQTT sensor topic is required")
        if len(set(topic_by_sensor_id.values())) != len(topic_by_sensor_id):
            raise ValueError("MQTT sensor topics must be unique")
        self.settings = settings
        if connection_timeout_seconds <= 0:
            raise ValueError("MQTT connection timeout must be positive")
        if publish_timeout_seconds <= 0:
            raise ValueError("MQTT publish timeout must be positive")
        self.connection_timeout_seconds = float(connection_timeout_seconds)
        self.publish_timeout_seconds = float(publish_timeout_seconds)
        self.topic_by_sensor_id = dict(topic_by_sensor_id)
        self.client = client or _create_paho_client(settings.client_id)
        _configure_client(self.client, settings)
        self._connected = Event()
        self.client.on_connect = self._on_connect
        self.started = False

    def start(self) -> None:
        """Connect and start paho's background network loop."""

        if self.started:
            return
        self.client.connect(
            self.settings.host,
            self.settings.port,
            self.settings.keepalive_seconds,
        )
        self.client.loop_start()
        if not self._connected.wait(self.connection_timeout_seconds):
            self.client.loop_stop()
            self.client.disconnect()
            raise TimeoutError("timed out waiting for MQTT publisher connection")
        self.started = True

    def publish(self, reading: SensorReading) -> None:
        """Publish one reading and fail fast on an immediate client error."""

        if not self.started:
            raise RuntimeError("MQTT publisher has not been started")
        try:
            topic = self.topic_by_sensor_id[reading.sensor_id]
        except KeyError as exc:
            raise KeyError(
                f"no MQTT topic configured for sensor {reading.sensor_id!r}"
            ) from exc
        result = self.client.publish(
            topic,
            encode_sensor_reading(reading),
            qos=self.settings.qos,
            retain=self.settings.retain,
        )
        if result.rc != 0:
            raise RuntimeError(f"MQTT publish failed with result code {result.rc}")
        # Accelerated simulations may exit immediately after a burst. Waiting
        # for each delivery prevents the final messages from being discarded
        # when the network loop is stopped.
        result.wait_for_publish(timeout=self.publish_timeout_seconds)
        if not result.is_published():
            raise TimeoutError("MQTT publish confirmation timed out")

    def stop(self) -> None:
        """Disconnect cleanly; repeated calls are safe."""

        if not self.started:
            return
        self.client.disconnect()
        self.client.loop_stop()
        self._connected.clear()
        self.started = False

    def _on_connect(
        self,
        _client: MqttClient,
        _userdata: object,
        _flags: object,
        reason_code: object,
        _properties: object | None = None,
    ) -> None:
        if reason_code == 0:
            self._connected.set()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()


class MqttSensorSubscriber:
    """Subscribe to sensor topics and feed messages into an adapter cache."""

    def __init__(
        self,
        settings: MqttConnectionSettings,
        adapter: MqttSensorAdapter,
        *,
        client: MqttClient | None = None,
        on_error: Callable[[Exception, str], None] | None = None,
        connection_timeout_seconds: float = 10.0,
    ) -> None:
        if not adapter.point_mappings:
            raise ValueError("MQTT subscriber requires at least one point mapping")
        self.settings = settings
        self.adapter = adapter
        self.client = client or _create_paho_client(settings.client_id)
        self.on_error = on_error
        if connection_timeout_seconds <= 0:
            raise ValueError("MQTT connection timeout must be positive")
        self.connection_timeout_seconds = float(connection_timeout_seconds)
        self.errors: list[tuple[str, Exception]] = []
        self._connected = Event()
        self.started = False
        _configure_client(self.client, settings)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def start(self) -> None:
        if self.started:
            return
        self.client.connect(
            self.settings.host,
            self.settings.port,
            self.settings.keepalive_seconds,
        )
        self.client.loop_start()
        if not self._connected.wait(self.connection_timeout_seconds):
            self.client.loop_stop()
            self.client.disconnect()
            raise TimeoutError("timed out waiting for MQTT subscriber connection")
        self.started = True

    def stop(self) -> None:
        if not self.started:
            return
        self.client.disconnect()
        self.client.loop_stop()
        self._connected.clear()
        self.started = False

    def _on_connect(
        self,
        _client: MqttClient,
        _userdata: object,
        _flags: object,
        reason_code: object,
        _properties: object | None = None,
    ) -> None:
        # paho automatically invokes this callback after a reconnect, so the
        # exact topic subscriptions are restored without external bookkeeping.
        if reason_code != 0:
            self._record_error(
                RuntimeError(f"MQTT connection failed: {reason_code}"), "<connect>"
            )
            return
        self._connected.set()
        for topic in self.adapter.point_mappings:
            self.client.subscribe(topic, qos=self.settings.qos)

    def _on_message(
        self, _client: MqttClient, _userdata: object, message: MqttMessage
    ) -> None:
        topic = str(message.topic)
        payload = message.payload
        try:
            self.adapter.on_message(
                topic,
                payload,
                received_at=datetime.now(timezone.utc),
            )
        except (KeyError, TypeError, ValueError) as exc:
            # A malformed device message must not terminate paho's network
            # thread.  Keep it inspectable and optionally notify deployment code.
            self._record_error(exc, topic)

    def _record_error(self, error: Exception, topic: str) -> None:
        self.errors.append((topic, error))
        if self.on_error is not None:
            self.on_error(error, topic)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()


def _configure_client(client: MqttClient, settings: MqttConnectionSettings) -> None:
    if settings.username is not None:
        client.username_pw_set(settings.username, settings.password)
    if settings.use_tls:
        client.tls_set()


def _create_paho_client(client_id: str) -> MqttClient:
    """Import paho lazily so pure physics and unit tests remain lightweight."""

    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:  # pragma: no cover - depends on deployment env
        raise RuntimeError(
            "paho-mqtt is required for live MQTT transport; install "
            "requirements-building.txt"
        ) from exc
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
