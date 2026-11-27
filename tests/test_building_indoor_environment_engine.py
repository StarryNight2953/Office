from datetime import datetime, timedelta, timezone

import pytest

from smartoffice.physics.building import (
    BuildingObservationModel,
    BuildingPhysicsOrchestrator,
    HvacCommand,
    HvacMode,
    IndoorEnvironmentEngine,
    InternalLoads,
    OutdoorConditions,
    PhysicsEventType,
    SensorQuantity,
    SensorSpec,
    ZoneParameters,
    ZoneState,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _isolated_parameters(**overrides) -> ZoneParameters:
    values = {
        "volume_m3": 100.0,
        "envelope_conductance_w_k": 0.0,
        "infiltration_air_changes_per_hour": 0.0,
        "effective_solar_aperture_m2": 0.0,
    }
    values.update(overrides)
    return ZoneParameters(**values)


def test_heating_increases_temperature_and_consumes_energy() -> None:
    engine = IndoorEnvironmentEngine(
        {"z1": _isolated_parameters()},
        {"z1": ZoneState(zone_id="z1", air_temperature_c=20.0)},
    )

    result = engine.step(
        at_time=NOW,
        timestep_seconds=300.0,
        outdoor=OutdoorConditions(air_temperature_c=20.0),
        hvac_by_zone={
            "z1": HvacCommand(mode=HvacMode.HEATING, heating_w=2000.0)
        },
    )[0]

    assert result.air_temperature_c > 20.0
    assert result.relative_humidity_pct < 50.0
    assert result.sensible_heat_balance_w == pytest.approx(2000.0)
    assert result.energy_kwh > 0.0


def test_cooling_reduces_temperature_and_absolute_moisture() -> None:
    engine = IndoorEnvironmentEngine(
        {
            "z1": _isolated_parameters(
                cooling_moisture_removal_g_per_kwh=2000.0
            )
        },
        {
            "z1": ZoneState(
                zone_id="z1", air_temperature_c=28.0, relative_humidity_pct=65.0
            )
        },
    )

    result = engine.step(
        NOW,
        900.0,
        OutdoorConditions(air_temperature_c=28.0),
        hvac_by_zone={
            "z1": HvacCommand(mode=HvacMode.COOLING, cooling_w=4000.0)
        },
    )[0]

    assert result.air_temperature_c < 28.0
    assert result.relative_humidity_pct < 65.0


def test_occupants_raise_co2_and_ventilation_reduces_it() -> None:
    params = _isolated_parameters()
    loads = {"z1": InternalLoads(occupants=10)}
    outdoor = OutdoorConditions(air_temperature_c=22.0, co2_ppm=420.0)

    unventilated = IndoorEnvironmentEngine(
        {"z1": params}, {"z1": ZoneState(zone_id="z1", co2_ppm=500.0)}
    ).step(NOW, 900.0, outdoor, loads_by_zone=loads)[0]
    ventilated = IndoorEnvironmentEngine(
        {"z1": params}, {"z1": ZoneState(zone_id="z1", co2_ppm=500.0)}
    ).step(
        NOW,
        900.0,
        outdoor,
        loads_by_zone=loads,
        hvac_by_zone={"z1": HvacCommand(outdoor_airflow_m3_s=0.3)},
    )[0]

    assert unventilated.co2_ppm > 500.0
    assert ventilated.co2_ppm < unventilated.co2_ppm


def test_heat_recovery_reduces_winter_ventilation_loss_without_hiding_co2() -> None:
    params = _isolated_parameters()
    outdoor = OutdoorConditions(air_temperature_c=-15.0, co2_ppm=420.0)
    initial = ZoneState(zone_id="z1", air_temperature_c=22.0, co2_ppm=1000.0)

    without_recovery = IndoorEnvironmentEngine({"z1": params}, {"z1": initial}).step(
        NOW,
        600.0,
        outdoor,
        hvac_by_zone={"z1": HvacCommand(outdoor_airflow_m3_s=0.2)},
    )[0]
    with_recovery = IndoorEnvironmentEngine({"z1": params}, {"z1": initial}).step(
        NOW,
        600.0,
        outdoor,
        hvac_by_zone={
            "z1": HvacCommand(
                outdoor_airflow_m3_s=0.2,
                outdoor_air_sensible_recovery_fraction=0.8,
            )
        },
    )[0]

    assert with_recovery.air_temperature_c > without_recovery.air_temperature_c
    assert with_recovery.co2_ppm == pytest.approx(without_recovery.co2_ppm)


def test_air_cleaner_reduces_pm25() -> None:
    params = _isolated_parameters(pm25_deposition_rate_per_hour=0.0)
    result = IndoorEnvironmentEngine(
        {"z1": params}, {"z1": ZoneState(zone_id="z1", pm25_ug_m3=100.0)}
    ).step(
        NOW,
        600.0,
        OutdoorConditions(air_temperature_c=22.0, pm25_ug_m3=100.0),
        hvac_by_zone={"z1": HvacCommand(clean_air_delivery_m3_s=0.25)},
    )[0]

    assert result.pm25_ug_m3 < 100.0


def test_observation_does_not_modify_truth_and_respects_sampling_interval() -> None:
    engine = IndoorEnvironmentEngine(
        {"z1": _isolated_parameters()},
        {"z1": ZoneState(zone_id="z1", air_temperature_c=20.0)},
    )
    observations = BuildingObservationModel(
        [
            SensorSpec(
                sensor_id="temp-01",
                zone_id="z1",
                quantity=SensorQuantity.AIR_TEMPERATURE_C,
                sample_interval_seconds=300.0,
                latency_seconds=30.0,
                bias=4.0,
            )
        ],
        seed=7,
    )
    runtime = BuildingPhysicsOrchestrator(engine, observations)

    first = runtime.step(NOW, 60.0, OutdoorConditions(air_temperature_c=20.0))
    second = runtime.step(
        NOW + timedelta(minutes=1),
        60.0,
        OutdoorConditions(air_temperature_c=20.0),
    )

    reading = first.observations[0].reading
    assert reading.value == pytest.approx(24.0)
    assert reading.available_at == NOW + timedelta(seconds=30)
    assert runtime.engine.get_state()["z1"].air_temperature_c == pytest.approx(20.0)
    assert second.observations == ()


def test_orchestrator_emits_only_threshold_transitions_and_restores_snapshot() -> None:
    engine = IndoorEnvironmentEngine(
        {"z1": _isolated_parameters()},
        {"z1": ZoneState(zone_id="z1", co2_ppm=1200.0)},
    )
    runtime = BuildingPhysicsOrchestrator(engine)

    first = runtime.step(NOW, 60.0, OutdoorConditions(air_temperature_c=22.0))
    snapshot = runtime.snapshot()
    second = runtime.step(
        NOW + timedelta(minutes=1),
        60.0,
        OutdoorConditions(air_temperature_c=22.0),
    )

    assert any(
        event.event_type == PhysicsEventType.AIR_QUALITY_THRESHOLD_VIOLATED
        for event in first.events
    )
    assert second.events == ()
    runtime.restore(snapshot)
    assert runtime.last_step_at == NOW


def test_unknown_zone_input_is_rejected() -> None:
    engine = IndoorEnvironmentEngine({"z1": ZoneParameters()})
    with pytest.raises(ValueError, match="unknown zones"):
        engine.step(
            NOW,
            60.0,
            OutdoorConditions(air_temperature_c=22.0),
            loads_by_zone={"missing": InternalLoads()},
        )
