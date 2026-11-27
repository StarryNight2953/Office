from datetime import datetime, timezone
from pathlib import Path

from smartoffice.apps.building_world import (
    BuildingEventType,
    LightingApp,
    MeetingEquipmentApp,
    PrintingApp,
    VentilationApp,
)
from smartoffice.apps.building_world.room_loader import load_room_configuration
from smartoffice.apps.building_world.runtime import (
    BuildingWorldRuntime,
    constant_outdoor_provider,
)
from smartoffice.apps.system import SystemApp
from smartoffice.controllers.engine import Engine
from smartoffice.physics.building import OutdoorConditions
from smartoffice.scenarios.building_kechuang.k1315.scenario_conference_standard import (
    ScenarioBuildingKechuangK1315ConferenceStandard,
)

CONFIG_PATH = Path(__file__).parents[1] / "smartoffice" / "configs" / "rooms" / "k1315.yaml"
ZONE_ID = "k1315_conference_zone"


def _runtime() -> BuildingWorldRuntime:
    return BuildingWorldRuntime.from_room_configuration(
        load_room_configuration(CONFIG_PATH),
        start_at=datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc),
        outdoor_provider=constant_outdoor_provider(
            OutdoorConditions(
                air_temperature_c=30.0,
                relative_humidity_pct=65.0,
                co2_ppm=420.0,
                pm25_ug_m3=25.0,
            )
        ),
    )


def test_ventilation_reduces_occupied_room_co2_and_consumes_energy() -> None:
    without_ventilation = _runtime()
    with_ventilation = _runtime()
    without_ventilation.world.room_states["k1315"].occupancy_count = 12
    with_ventilation.world.room_states["k1315"].occupancy_count = 12

    command = VentilationApp(with_ventilation.world).set_ventilation(
        "k1315_ventilation_01", True, 3
    )
    without_ventilation.advance_by(30 * 60)
    with_ventilation.advance_by(30 * 60)

    assert command["status"] == "accepted"
    assert with_ventilation.physics.engine.get_state()[ZONE_ID].co2_ppm < (
        without_ventilation.physics.engine.get_state()[ZONE_ID].co2_ppm
    )
    assert with_ventilation.physics.cumulative_energy_kwh_by_zone[ZONE_ID] > 0.0


def test_meeting_tools_track_settings_print_completion_and_cleanup() -> None:
    scenario = ScenarioBuildingKechuangK1315ConferenceStandard()
    Engine(None, scenario).build_oracle_workflow(run_oracle=False)
    lighting = scenario.get_typed_app(LightingApp)
    equipment = scenario.get_typed_app(MeetingEquipmentApp)
    printing = scenario.get_typed_app(PrintingApp)
    system = scenario.get_typed_app(SystemApp)

    assert (
        lighting.set_lighting(
            "k1315_lighting_front_01", True, 45, 4000, "presentation"
        )["status"]
        == "accepted"
    )
    assert (
        equipment.set_projector("k1315_projector_01", True, "hdmi")["status"]
        == "accepted"
    )
    assert (
        equipment.set_audio_system("k1315_audio_01", True, 55, True)["status"]
        == "accepted"
    )
    assert equipment.get_meeting_equipment_readiness("k1315")["ready"] is True

    assert printing.set_printer_power("k1315_printer_01", True)["status"] == "accepted"
    submitted = printing.submit_print_job(
        "k1315_printer_01", "conference_materials", 12, 2, "staff-01"
    )
    assert submitted["status"] == "accepted"
    assert printing.get_print_job(submitted["job_id"])["status"] == "printing"
    system.advance_time(minutes=2)
    # Completion is a scheduled domain fact.  It must update the workflow at
    # the physical ready time even if the Agent never polls the print job.
    assert printing.jobs[submitted["job_id"]]["status"] == "completed"
    completion_events = [
        event
        for event in scenario.get_typed_app(PrintingApp).world.events
        if event.event_type == BuildingEventType.PRINT_JOB_COMPLETED
        and event.subject_id == submitted["job_id"]
    ]
    assert len(completion_events) == 1
    assert printing.get_print_job(submitted["job_id"])["status"] == "completed"

    equipment.set_projector("k1315_projector_01", False, "off")
    equipment.set_audio_system("k1315_audio_01", False, 0, False)
    assert equipment.get_meeting_equipment_readiness("k1315")["ready"] is False


def test_printer_serializes_jobs_and_rejects_invalid_priority() -> None:
    runtime = _runtime()
    printing = PrintingApp(runtime.world)
    assert printing.set_printer_power("k1315_printer_01", True)["status"] == "accepted"

    first = printing.submit_print_job(
        "k1315_printer_01", "high_priority_pack", 20, 2, "staff-01", 3
    )
    second = printing.submit_print_job(
        "k1315_printer_01", "queued_nameplates", 20, 1, "staff-01", 1
    )
    rejected = printing.submit_print_job(
        "k1315_printer_01", "bad_priority", 1, 1, "staff-01", 4
    )

    assert first["queued_seconds"] == 0.0
    assert second["queued_seconds"] > 0.0
    assert printing.get_print_job(second["job_id"])["status"] == "queued"
    assert rejected == {"status": "rejected", "reason": "invalid_priority"}


def test_shutdown_ignores_irrelevant_lighting_and_audio_placeholders() -> None:
    runtime = _runtime()
    lighting = LightingApp(runtime.world)
    equipment = MeetingEquipmentApp(runtime.world)

    light_result = lighting.set_lighting(
        "k1315_lighting_front_01", False, 0, 0, "off"
    )
    audio_result = equipment.set_audio_system(
        "k1315_audio_01", False, -1, False
    )

    assert light_result["status"] == "accepted"
    assert audio_result["status"] == "accepted"
    assert runtime.world.device_states["k1315_lighting_front_01"].power_on is False
    assert runtime.world.device_states["k1315_audio_01"].power_on is False
