from datetime import datetime, timedelta, timezone

import pytest

from smartoffice.adapters.building import (
    MqttSensorAdapter,
    SensorPointMapping,
    SimulatedSensorAdapter,
)
from smartoffice.apps.building_world import (
    BuildingSensorApp,
    SensorHub,
    SensorShadowEvaluator,
)
from smartoffice.physics.building import (
    BuildingObservationModel,
    BuildingPhysicsOrchestrator,
    IndoorEnvironmentEngine,
    OutdoorConditions,
    SensorQuantity,
    SensorReadRequest,
    SensorSpec,
    ZoneParameters,
    ZoneState,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_mqtt_adapter_maps_payload_and_converts_units() -> None:
    adapter = MqttSensorAdapter(
        [
            SensorPointMapping(
                point_address="k1324/temp",
                sensor_id="temp-01",
                zone_id="meeting",
                quantity=SensorQuantity.AIR_TEMPERATURE_C,
                source_unit="degF",
            )
        ],
        unit_converters={
            ("degF", "degC"): lambda value: (value - 32.0) * 5.0 / 9.0
        },
    )

    reading = adapter.on_message(
        "k1324/temp",
        '{"value": 77, "observed_at": "2026-01-01T11:59:55Z"}',
        received_at=NOW,
    )

    assert reading.value == pytest.approx(25.0)
    assert reading.unit == "degC"
    assert reading.available_at == NOW


def test_hub_prefers_real_provider_but_preserves_shadow_sources() -> None:
    hub = SensorHub(["meeting"], published_priority=0)
    simulated_mapping = SensorPointMapping(
        point_address="temp",
        sensor_id="temp-sim",
        zone_id="meeting",
        quantity=SensorQuantity.AIR_TEMPERATURE_C,
        source_unit="degC",
    )
    real_mapping = SensorPointMapping(
        point_address="temp",
        sensor_id="temp-real",
        zone_id="meeting",
        quantity=SensorQuantity.AIR_TEMPERATURE_C,
        source_unit="degC",
    )
    simulated = SimulatedSensorAdapter([simulated_mapping])
    real = SimulatedSensorAdapter([real_mapping])
    hub.register_provider("simulation", simulated, priority=0)
    hub.register_provider("real", real, priority=100)
    simulated.ingest("temp", 23.0, received_at=NOW)
    real.ingest("temp", 25.0, received_at=NOW - timedelta(seconds=5))
    request = SensorReadRequest(
        at_time=NOW,
        zone_ids=("meeting",),
        quantities=(SensorQuantity.AIR_TEMPERATURE_C,),
    )

    assert hub.read(request)[0].sensor_id == "temp-real"
    assert set(hub.read_by_source(request)) == {"simulation", "real"}


def test_orchestrator_publishes_simulated_reading_to_sensor_app() -> None:
    hub = SensorHub(["meeting"])
    observation_model = BuildingObservationModel(
        [
            SensorSpec(
                sensor_id="temp-sim",
                zone_id="meeting",
                quantity=SensorQuantity.AIR_TEMPERATURE_C,
            )
        ]
    )
    runtime = BuildingPhysicsOrchestrator(
        IndoorEnvironmentEngine(
            {"meeting": ZoneParameters()},
            {"meeting": ZoneState("meeting", air_temperature_c=24.0)},
        ),
        observation_model=observation_model,
        observation_sink=hub,
    )
    runtime.step(NOW, 60.0, OutdoorConditions(air_temperature_c=24.0))
    app = BuildingSensorApp(hub)
    app.time_manager.reset(start_time=NOW.timestamp())

    response = app.read_zone_sensors("meeting")

    assert response["readings"][0]["sensor_id"] == "temp-sim"
    assert "truth_value" not in response["readings"][0]


def test_shadow_evaluator_records_bias_mae_and_avoids_duplicate_samples() -> None:
    hub = SensorHub(["meeting"])
    simulated = SimulatedSensorAdapter(
        [
            SensorPointMapping(
                point_address="temp-sim",
                sensor_id="temp-sim",
                zone_id="meeting",
                quantity=SensorQuantity.AIR_TEMPERATURE_C,
                source_unit="degC",
            )
        ]
    )
    real = SimulatedSensorAdapter(
        [
            SensorPointMapping(
                point_address="temp-real",
                sensor_id="temp-real",
                zone_id="meeting",
                quantity=SensorQuantity.AIR_TEMPERATURE_C,
                source_unit="degC",
            )
        ]
    )
    hub.register_provider("simulation", simulated)
    hub.register_provider("mqtt", real, priority=100)
    simulated.ingest("temp-sim", 24.0, observed_at=NOW, received_at=NOW)
    real.ingest("temp-real", 25.5, observed_at=NOW, received_at=NOW)
    request = hub.read_all_request(NOW)
    evaluator = SensorShadowEvaluator(
        reference_source="mqtt", candidate_source="simulation"
    )

    comparisons = evaluator.sample(hub, request)
    duplicate = evaluator.sample(hub, request)
    summary = evaluator.summary()

    assert comparisons[0].signed_error == -1.5
    assert duplicate == ()
    assert summary["sample_count"] == 1
    assert summary["point_count"] == 1
    assert summary["by_point"][0] == {
        "zone_id": "meeting",
        "quantity": "air_temperature_c",
        "count": 1,
        "bias": -1.5,
        "mae": 1.5,
        "rmse": 1.5,
        "max_absolute_error": 1.5,
    }

    restored = SensorShadowEvaluator(
        reference_source="mqtt", candidate_source="simulation"
    )
    restored.restore(evaluator.snapshot())
    assert restored.summary() == summary
    assert restored.sample(hub, request) == ()

    simulated.ingest(
        "temp-sim",
        24.1,
        observed_at=NOW + timedelta(seconds=301),
        received_at=NOW + timedelta(seconds=301),
    )
    strict = SensorShadowEvaluator(
        reference_source="mqtt",
        candidate_source="simulation",
        max_alignment_seconds=60,
    )
    assert strict.sample(
        hub,
        hub.read_all_request(NOW + timedelta(seconds=301)),
    ) == ()
