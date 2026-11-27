"""Drive Building World physics and publish its observations over MQTT."""

from __future__ import annotations

import argparse
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from smartoffice.adapters.building import (
    MqttConnectionSettings,
    MqttSensorPublisher,
    SensorPointMapping,
)
from smartoffice.apps.building_world.room_loader import LoadedRoomConfiguration
from smartoffice.apps.building_world.runtime import (
    BuildingWorldRuntime,
    constant_outdoor_provider,
)
from smartoffice.physics.building.models import OutdoorConditions
from smartoffice.physics.building.sensor_api import (
    EXPECTED_UNITS,
    SensorQuantity,
    SensorReading,
)


class ReadingPublisher(Protocol):
    """Publisher boundary used to test the simulator without a live broker."""

    def start(self) -> None: ...

    def publish(self, reading: SensorReading) -> None: ...

    def stop(self) -> None: ...


_TOPIC_QUANTITY_NAMES = {
    SensorQuantity.AIR_TEMPERATURE_C: "temperature",
    SensorQuantity.RELATIVE_HUMIDITY_PCT: "humidity",
    SensorQuantity.CO2_PPM: "co2",
    SensorQuantity.PM25_UG_M3: "pm25",
}


def build_sensor_topics(
    configurations: Sequence[LoadedRoomConfiguration],
    *,
    topic_prefix: str = "kechuang",
) -> dict[str, str]:
    """Build stable topics matching ``<prefix>/<room>/environment/<quantity>``."""

    prefix = topic_prefix.strip("/")
    if not prefix:
        raise ValueError("MQTT topic prefix cannot be empty")
    topics: dict[str, str] = {}
    used_topics: set[str] = set()
    for configuration in configurations:
        for sensor in configuration.sensors:
            topic = (
                f"{prefix}/{configuration.room.room_id}/environment/"
                f"{_TOPIC_QUANTITY_NAMES[sensor.quantity]}"
            )
            if topic in used_topics:
                raise ValueError(
                    "multiple sensors resolve to the same MQTT topic; configure "
                    "a more specific topic scheme"
                )
            used_topics.add(topic)
            topics[sensor.sensor_id] = topic
    return topics


def build_sensor_point_mappings(
    configurations: Sequence[LoadedRoomConfiguration],
    *,
    topic_prefix: str = "kechuang",
) -> tuple[SensorPointMapping, ...]:
    """Create receiver mappings that exactly mirror the simulator topics."""

    topics = build_sensor_topics(configurations, topic_prefix=topic_prefix)
    return tuple(
        SensorPointMapping(
            point_address=topics[sensor.sensor_id],
            sensor_id=sensor.sensor_id,
            zone_id=sensor.zone_id,
            quantity=sensor.quantity,
            source_unit=EXPECTED_UNITS[sensor.quantity],
        )
        for configuration in configurations
        for sensor in configuration.sensors
    )


class BuildingMqttSensorSimulator:
    """Advance a deterministic building runtime and publish each new sample.

    ``step`` advances simulation time explicitly.  Callers decide whether to
    sleep between steps (real-time mode) or run immediately (accelerated mode).
    The last-published watermark prevents unchanged cached readings from being
    emitted again when a physics tick is shorter than a sensor sample interval.
    """

    def __init__(
        self,
        runtime: BuildingWorldRuntime,
        publisher: ReadingPublisher,
    ) -> None:
        self.runtime = runtime
        self.publisher = publisher
        self._last_published_at: dict[str, datetime] = {}

    def step(self, simulation_seconds: float) -> list[SensorReading]:
        """Advance physics once and publish observations newly available now."""

        if simulation_seconds <= 0:
            raise ValueError("simulation step must be positive")
        self.runtime.advance_by(simulation_seconds)
        visible = self.runtime.sensors.read_all(self.runtime.current_time)
        published: list[SensorReading] = []
        for reading in visible:
            previous = self._last_published_at.get(reading.sensor_id)
            if previous is not None and reading.observed_at <= previous:
                continue
            self.publisher.publish(reading)
            self._last_published_at[reading.sensor_id] = reading.observed_at
            published.append(reading)
        return published

    def run(
        self,
        *,
        simulation_step_seconds: float,
        steps: int | None = None,
        realtime: bool = True,
    ) -> None:
        """Run until ``steps`` is reached or the process receives Ctrl-C."""

        if steps is not None and steps <= 0:
            raise ValueError("steps must be positive when provided")
        self.publisher.start()
        completed = 0
        try:
            while steps is None or completed < steps:
                started = time.monotonic()
                readings = self.step(simulation_step_seconds)
                completed += 1
                for reading in readings:
                    print(
                        f"{reading.observed_at.isoformat()} "
                        f"{reading.sensor_id}={reading.value:.3f} {reading.unit}"
                    )
                if realtime:
                    remaining = simulation_step_seconds - (time.monotonic() - started)
                    if remaining > 0:
                        time.sleep(remaining)
        except KeyboardInterrupt:
            pass
        finally:
            self.publisher.stop()


def create_runtime(
    configurations: Sequence[LoadedRoomConfiguration],
    *,
    start_at: datetime,
    seed: int = 0,
) -> BuildingWorldRuntime:
    """Create the same physical runtime used by normal Building scenarios."""

    return BuildingWorldRuntime.from_room_configurations(
        configurations,
        start_at=start_at,
        outdoor_provider=constant_outdoor_provider(
            OutdoorConditions(
                air_temperature_c=30.0,
                relative_humidity_pct=65.0,
                co2_ppm=420.0,
                pm25_ug_m3=25.0,
                solar_irradiance_w_m2=300.0,
            )
        ),
        observation_seed=seed,
    )


def _default_room_config_paths() -> tuple[Path, ...]:
    room_directory = Path(__file__).parents[2] / "configs" / "rooms"
    return tuple(
        room_directory / filename
        for filename in ("k1324.yaml", "k1316.yaml", "k1315.yaml")
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--room-config",
        action="append",
        type=Path,
        help="JSON-compatible room YAML; repeat to simulate multiple rooms",
    )
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--topic-prefix", default="kechuang")
    parser.add_argument("--qos", type=int, choices=(0, 1, 2), default=1)
    parser.add_argument("--retain", action="store_true")
    parser.add_argument("--client-id", default="smartoffice-building-sensor-simulator")
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--step-seconds", type=float, default=60.0)
    parser.add_argument("--steps", type=int)
    parser.add_argument(
        "--accelerated",
        action="store_true",
        help="advance without wall-clock sleeping",
    )
    parser.add_argument("--seed", type=int, default=0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from smartoffice.apps.building_world.room_loader import load_room_configuration

    args = _build_parser().parse_args(argv)
    paths = tuple(args.room_config or _default_room_config_paths())
    configurations = tuple(load_room_configuration(path) for path in paths)
    topics = build_sensor_topics(configurations, topic_prefix=args.topic_prefix)
    settings = MqttConnectionSettings(
        host=args.broker,
        port=args.port,
        qos=args.qos,
        retain=args.retain,
        client_id=args.client_id,
        username=args.username,
        password=args.password,
        use_tls=args.tls,
    )
    runtime = create_runtime(
        configurations,
        start_at=datetime.now(timezone.utc),
        seed=args.seed,
    )
    simulator = BuildingMqttSensorSimulator(
        runtime,
        MqttSensorPublisher(settings, topics),
    )
    simulator.run(
        simulation_step_seconds=args.step_seconds,
        steps=args.steps,
        realtime=not args.accelerated,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
