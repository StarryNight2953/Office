import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from smartoffice.adapters.building import MqttSensorAdapter, SensorPointMapping
from smartoffice.apps.building_world import (
    AirDeviceApp,
    BuildingEventType,
    HvacApp,
    OccupancyApp,
)
from smartoffice.apps.building_world.room_loader import load_room_configuration
from smartoffice.apps.building_world.runtime import (
    BuildingWorldRuntime,
    constant_outdoor_provider,
)
from smartoffice.apps.building_world.sensor_app import BuildingSensorApp
from smartoffice.apps.system import SystemApp
from smartoffice.controllers.engine import Engine
from smartoffice.physics.building import (
    OutdoorConditions,
    SensorQuantity,
    SensorReadRequest,
)
from smartoffice.scenarios.building_kechuang.scenario_room_booking import (
    ScenarioBuildingKechuangRoomBooking,
)

CONFIG_PATH = Path(__file__).parents[1] / "smartoffice" / "configs" / "rooms" / "k1324.yaml"
START_AT = datetime(2026, 9, 10, 5, 0, tzinfo=timezone.utc)
ZONE_ID = "k1324_office_zone"


def _runtime() -> BuildingWorldRuntime:
    return BuildingWorldRuntime.from_room_configuration(
        load_room_configuration(CONFIG_PATH),
        start_at=START_AT,
        outdoor_provider=constant_outdoor_provider(
            OutdoorConditions(
                air_temperature_c=32.0,
                relative_humidity_pct=70.0,
                co2_ppm=420.0,
                pm25_ug_m3=45.0,
            )
        ),
    )


def test_room_loader_builds_business_physics_device_and_sensor_models() -> None:
    config = load_room_configuration(CONFIG_PATH)

    assert config.room.room_id == "k1324"
    assert config.room.room_type == "graduate_office"
    assert config.room.bookable is False
    assert config.room.capacity == 17
    assert list(config.zone_parameters) == [ZONE_ID]
    assert config.zone_parameters[ZONE_ID].volume_m3 == pytest.approx(210.0)
    assert {area.area_id for area in config.functional_areas} == {
        "k1324_professor_office_01",
        "k1324_professor_office_02",
        "k1324_shared_cubicle",
        "k1324_graduate_workstations",
    }
    assert {device.device_id for device in config.devices} >= {
        "k1324_hvac_01",
        "k1324_humidifier_01",
        "k1324_purifier_01",
    }
    assert len(config.sensors) == 4

    world = _runtime().world
    snapshot = world.snapshot()
    assert len(snapshot["functional_areas"]) == 4
    assert snapshot["rooms"][0]["bookable"] is False
    assert snapshot["rooms"][0]["room_type"] == "graduate_office"


def test_room_loader_rejects_unknown_device_zone(tmp_path: Path) -> None:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["devices"][0]["zone_id"] = "missing"
    invalid = tmp_path / "invalid-room.yaml"
    invalid.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown zone"):
        load_room_configuration(invalid)


def test_hvac_shutdown_ignores_irrelevant_target_temperature() -> None:
    runtime = _runtime()
    hvac = HvacApp(runtime.world)
    assert hvac.set_hvac("k1324_hvac_01", True, "cooling", 24.0, 2)["status"] == (
        "accepted"
    )

    result = hvac.set_hvac("k1324_hvac_01", False, "off", 0.0, 0)

    assert result["status"] == "accepted"
    assert runtime.world.device_states["k1324_hvac_01"].target_temperature_c is None


def test_room_occupancy_tool_exposes_aggregate_count_and_capacity() -> None:
    runtime = _runtime()
    runtime.world.room_states["k1324"].occupancy_count = 15

    result = OccupancyApp(runtime.world).get_room_occupancy("k1324")

    assert result == {
        "room_id": "k1324",
        "occupancy_count": 15,
        "capacity": 17,
        "occupancy_fraction": pytest.approx(15 / 17),
    }


def test_runtime_shadow_mode_records_each_reference_sample_once() -> None:
    runtime = _runtime()
    mqtt = MqttSensorAdapter(
        [
            SensorPointMapping(
                point_address="k1324/temperature",
                sensor_id="k1324_temp_real",
                zone_id=ZONE_ID,
                quantity=SensorQuantity.AIR_TEMPERATURE_C,
                source_unit="degC",
            )
        ]
    )
    runtime.sensors.register_provider("mqtt", mqtt, priority=100)
    mqtt.ingest(
        "k1324/temperature",
        27.5,
        observed_at=START_AT,
        received_at=START_AT,
    )
    shadow = runtime.configure_sensor_shadow(reference_source="mqtt")

    runtime.advance_by(60)
    snapshot = runtime.snapshot()
    runtime.advance_by(60)

    assert shadow.summary()["sample_count"] == 1
    assert any(item.get("kind") == "sensor_shadow" for item in runtime.trace)

    runtime.restore(snapshot)
    assert shadow.summary()["sample_count"] == 1


def test_cooling_humidification_energy_events_and_checkpoint() -> None:
    runtime = _runtime()
    world = runtime.world
    hvac = HvacApp(world)
    air = AirDeviceApp(world)
    initial = runtime.physics.engine.get_state()[ZONE_ID]

    accepted = hvac.set_hvac(
        "k1324_hvac_01", True, "cooling", target_temperature_c=22.0, fan_level=3
    )
    assert accepted["status"] == "accepted"
    runtime.advance_by(30 * 60)
    cooled = runtime.physics.engine.get_state()[ZONE_ID]
    checkpoint = runtime.snapshot()
    energy_after_cooling = runtime.physics.cumulative_energy_kwh_by_zone[ZONE_ID]

    assert cooled.air_temperature_c < initial.air_temperature_c
    assert cooled.relative_humidity_pct < initial.relative_humidity_pct
    assert energy_after_cooling > 0.0
    assert any(
        event.event_type == BuildingEventType.SENSOR_UPDATED for event in world.events
    )

    assert air.set_humidifier("k1324_humidifier_01", True, 3)["status"] == ("accepted")
    runtime.advance_by(15 * 60)
    humidified = runtime.physics.engine.get_state()[ZONE_ID]
    assert humidified.relative_humidity_pct > cooled.relative_humidity_pct
    assert runtime.physics.cumulative_energy_kwh_by_zone[ZONE_ID] > (
        energy_after_cooling
    )

    readings = runtime.sensors.read(
        SensorReadRequest(
            at_time=runtime.current_time,
            zone_ids=(ZONE_ID,),
        )
    )
    assert len(readings) == 4

    runtime.restore(checkpoint)
    restored = runtime.physics.engine.get_state()[ZONE_ID]
    assert restored.relative_humidity_pct == pytest.approx(cooled.relative_humidity_pct)
    assert world.device_states["k1324_humidifier_01"].power_on is False
    assert runtime.physics.cumulative_energy_kwh_by_zone[ZONE_ID] == pytest.approx(
        energy_after_cooling
    )


def test_k1324_system_time_advance_drives_runtime_and_sensor_app() -> None:
    scenario = ScenarioBuildingKechuangRoomBooking()
    engine = Engine(None, scenario)
    engine.build_oracle_workflow(run_oracle=False)
    hvac = scenario.get_typed_app(HvacApp)
    system = scenario.get_typed_app(SystemApp)
    sensors = scenario.get_typed_app(BuildingSensorApp)
    runtime = scenario.building_runtime
    before = runtime.physics.engine.get_state()[ZONE_ID].air_temperature_c

    assert (
        hvac.set_hvac("k1324_hvac_01", True, "cooling", 22.0, 3)["status"] == "accepted"
    )
    assert system.advance_time(minutes=5)["status"] == "ok"

    after = runtime.physics.engine.get_state()[ZONE_ID].air_temperature_c
    response = sensors.read_zone_sensors(ZONE_ID)
    assert after < before
    assert len(response["readings"]) == 4


def test_scenario_base_assembles_three_distinct_room_models() -> None:
    scenario = ScenarioBuildingKechuangRoomBooking()
    Engine(None, scenario).build_oracle_workflow(run_oracle=False)
    world = scenario.building_runtime.world

    assert set(world.rooms) == {"k1324", "k1316", "k1315"}
    assert world.rooms["k1324"].bookable is False
    assert world.rooms["k1324"].capacity == 17
    assert world.rooms["k1316"].room_type == "seminar_room"
    assert world.rooms["k1316"].bookable is True
    assert world.rooms["k1315"].room_type == "conference_room"
    assert world.rooms["k1315"].bookable is True
    assert "k1315_projector_01" in world.devices
    assert "k1324_projector_01" not in world.devices
    assert set(scenario.building_runtime.physics.engine.get_state()) == {
        "k1324_office_zone",
        "k1316_seminar_zone",
        "k1315_conference_zone",
    }
