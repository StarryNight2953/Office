import pytest

from smartoffice.apps.building_world import BuildingEventType
from smartoffice.controllers.engine import Engine
from smartoffice.scenarios.building_kechuang.k1315.scenario_conference_standard import (
    ScenarioBuildingKechuangK1315ConferenceStandard,
)
from smartoffice.scenarios.building_kechuang.k1316.scenario_seminar_standard import (
    ScenarioBuildingKechuangK1316SeminarStandard,
)
from smartoffice.scenarios.building_kechuang.k1324.scenario_climate_coordination import (
    ScenarioBuildingKechuangK1324ClimateCoordination,
)
from smartoffice.scenarios.building_kechuang.k1324.scenario_occupancy_ramp import (
    OCCUPANCY_TIMELINE,
    ScenarioBuildingKechuangK1324OccupancyRamp,
)
from smartoffice.scenarios.registry import get_scenario_class


def _replay(scenario_class):
    build_engine = Engine(None, scenario_class())
    oracle = build_engine.build_oracle_workflow(run_oracle=False)
    replay_engine = Engine(None, scenario_class())
    replayed = replay_engine.replay_workflow(oracle)
    report = replay_engine.evaluation_report(replayed)
    return oracle, replay_engine, replayed, report


@pytest.mark.parametrize(
    "scenario_class",
    [
        ScenarioBuildingKechuangK1315ConferenceStandard,
        ScenarioBuildingKechuangK1316SeminarStandard,
        ScenarioBuildingKechuangK1324ClimateCoordination,
        ScenarioBuildingKechuangK1324OccupancyRamp,
    ],
)
def test_normal_scenario_oracle_replay_validates(scenario_class) -> None:
    oracle, _, replayed, report = _replay(scenario_class)

    assert len(oracle.dag) == len(replayed.dag)
    assert report["validation"]["success"] is True
    metrics = report["outcome"]["building"]
    assert metrics["physics_step_count"] > 0
    assert metrics["simulated_time_seconds"] > 0
    assert metrics["energy"]["total_kwh"] >= 0.0
    assert metrics["inventory"] == {
        "room_count": 3,
        "zone_count": 3,
        "device_count": 17,
        "sensor_count": 11,
        "device_types": [
            "air_purifier",
            "audio",
            "humidifier",
            "hvac",
            "lighting",
            "printer",
            "projector",
            "ventilation",
        ],
    }


def test_standard_conference_runs_ordered_lifecycle_and_cleanup() -> None:
    _, replay_engine, replayed, report = _replay(
        ScenarioBuildingKechuangK1315ConferenceStandard
    )
    metadata = report["validation"]["metadata"]

    assert metadata["lifecycle_events"] == [
        BuildingEventType.MEETING_PREPARATION_DUE.value,
        BuildingEventType.OCCUPANCY_CHANGED.value,
        BuildingEventType.MEETING_STARTED.value,
        BuildingEventType.MEETING_ENDED.value,
    ]
    assert metadata["energy_kwh"] > 0.0
    assert len(replayed.dag["verify_at_meeting_start"].content["readings"]) == 4
    wake_types = {
        item["event_type"]
        for item in replay_engine.scenario.building_runtime.trace
        if item.get("kind") == "trigger_decision" and item.get("should_wake_agent")
    }
    assert BuildingEventType.MEETING_PREPARATION_DUE.value in wake_types
    assert BuildingEventType.MEETING_ENDED.value in wake_types


def test_small_seminar_runs_midpoint_feedback_and_cleanup() -> None:
    _, _, replayed, report = _replay(
        ScenarioBuildingKechuangK1316SeminarStandard
    )
    metadata = report["validation"]["metadata"]

    assert metadata["lifecycle_events"] == [
        BuildingEventType.MEETING_PREPARATION_DUE.value,
        BuildingEventType.OCCUPANCY_CHANGED.value,
        BuildingEventType.MEETING_STARTED.value,
        BuildingEventType.ENVIRONMENT_CHECK_DUE.value,
        BuildingEventType.MEETING_ENDED.value,
    ]
    assert metadata["peak_co2_ppm"] < 1200.0
    assert metadata["energy_kwh"] > 0.0
    assert replayed.dag["verify_seminar_equipment"].content["ready"] is True
    assert replayed.dag["boost_full_room_ventilation"].content["status"] == "accepted"


def test_climate_coordination_observes_cooling_and_humidity() -> None:
    _, _, replayed, report = _replay(ScenarioBuildingKechuangK1324ClimateCoordination)
    metadata = report["validation"]["metadata"]

    assert metadata["final_temperature_c"] < metadata["initial_temperature_c"]
    assert 40.0 <= metadata["final_relative_humidity_pct"] <= 65.0
    assert metadata["environment_checks"] == 2
    for step_id in (
        "read_after_cooling",
        "read_coordinated_climate",
        "read_final_climate",
    ):
        assert len(replayed.dag[step_id].content["readings"]) == 4


def test_occupancy_ramp_records_planned_counts_and_co2_recovery() -> None:
    _, _, _, report = _replay(ScenarioBuildingKechuangK1324OccupancyRamp)
    metadata = report["validation"]["metadata"]

    assert metadata["occupancy_counts"] == [count for _, count in OCCUPANCY_TIMELINE]
    assert metadata["peak_co2_ppm"] > metadata["initial_co2_ppm"]
    assert metadata["final_co2_ppm"] < metadata["peak_co2_ppm"]


@pytest.mark.parametrize(
    ("scenario_id", "scenario_class"),
    [
        (
            "scenario_building_kechuang_k1315_conference_standard",
            ScenarioBuildingKechuangK1315ConferenceStandard,
        ),
        (
            "scenario_building_kechuang_k1316_seminar_standard",
            ScenarioBuildingKechuangK1316SeminarStandard,
        ),
        (
            "scenario_building_kechuang_k1324_climate_coordination",
            ScenarioBuildingKechuangK1324ClimateCoordination,
        ),
        (
            "scenario_building_kechuang_k1324_occupancy_ramp",
            ScenarioBuildingKechuangK1324OccupancyRamp,
        ),
    ],
)
def test_normal_scenario_is_registered(scenario_id, scenario_class) -> None:
    assert get_scenario_class(scenario_id) is scenario_class
