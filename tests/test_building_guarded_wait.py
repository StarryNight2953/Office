"""V3 deterministic wait policy: real physics and existing guards, no live LLM."""

from datetime import timedelta

import pytest

from fairy.apps.building_world import BuildingOperationsApp, HvacApp, PrintingApp
from fairy.apps.system import SystemApp
from fairy.controllers.engine import Engine
from fairy.scenarios.building_kechuang.l3.scenarios import L3_SCENARIO_CLASSES


def _scenario(index=0):
    scenario = L3_SCENARIO_CLASSES[index]()
    scenario.setup()
    engine = Engine(None, scenario, virtual_time=True)
    operations = scenario.get_typed_app(BuildingOperationsApp)
    operations.enable_fair_wait_policy()
    return scenario, engine, operations, scenario.get_typed_app(SystemApp)


def test_wait_advances_stable_time_instead_of_returning_already_stable() -> None:
    scenario, engine, operations, system = _scenario()
    operations.world.schedule.clear()  # Isolate the wait from readiness work.
    start = scenario.building_runtime.current_time
    result = system.advance_time(minutes=12)
    assert result["status"] == "ok"
    assert result["advanced_seconds"] == 720
    assert result["interrupted"] is False
    assert result["bounded_wait"]["time_advance_calls"] == 3
    assert result["bounded_wait"]["environment_checks"] == 4
    assert engine.time_manager.time() == (start + timedelta(minutes=12)).timestamp()


@pytest.mark.parametrize("wait_tool", ["advance_time", "wait_for_notification"])
def test_all_live_wait_paths_clamp_to_horizon_and_freeze_there(wait_tool) -> None:
    scenario, engine, operations, system = _scenario()
    operations.world.schedule.clear()
    start = scenario.building_runtime.current_time
    operations.operational_end_at = start + timedelta(minutes=7)
    wait = (
        (lambda: system.advance_time(hours=3))
        if wait_tool == "advance_time"
        else (lambda: system.wait_for_notification(timeout=3 * 3600))
    )
    first = wait()
    assert first["status"] == "ok"
    assert first["advanced_seconds"] == 420
    assert first["interrupted"] is True
    assert first["reason"] == "operational_horizon_reached"
    assert scenario.building_runtime.current_time == operations.operational_end_at
    assert engine.time_manager.time() == operations.operational_end_at.timestamp()
    second = wait()
    assert second["status"] == "rejected"
    assert second["advanced_seconds"] == 0
    assert second["reason"] == "operational_horizon_reached"
    assert (
        operations.time_advance_precondition(
            operations.operational_end_at.timestamp() + 1
        )["code"]
        == "operational_horizon_reached"
    )


def test_wait_refreshes_environment_but_does_not_bypass_missing_controls() -> None:
    scenario, engine, _operations, system = _scenario(19)
    first = system.advance_time(minutes=30)
    assert first["status"] == "ok"
    assert first["interrupted"] is True
    rejected = system.advance_time(minutes=5)
    assert rejected["status"] == "rejected"
    details = rejected["bounded_wait"]["time_advance"]["details"]
    assert details["code"] == "occupied_environment_uncontrolled"
    # No separate model-driven get_environment_control_status call needed.
    scenario.get_typed_app(HvacApp).set_hvac("k1324_hvac_01", True, "cooling", 24.0, 3)
    accepted = system.advance_time(minutes=5)
    assert accepted["status"] == "ok"
    assert accepted["advanced_seconds"] > 0
    assert (
        engine.time_manager.time() == scenario.building_runtime.current_time.timestamp()
    )
    assert (
        system.advance_time(minutes=5, stop_on_event=False)["reason"]
        == "unguarded_time_advance_forbidden"
    )


def test_wait_still_runs_every_registered_precondition_and_surfaces_details() -> None:
    scenario, _, operations, system = _scenario()
    operations.world.schedule.clear()
    start = scenario.building_runtime.current_time
    system.register_time_advance_precondition(
        lambda _: {"allowed": False, "code": "custom_safety_guard"}
    )
    result = system.advance_time(minutes=30)
    assert result["status"] == "rejected"
    assert (
        result["bounded_wait"]["time_advance"]["details"]["code"]
        == "custom_safety_guard"
    )
    assert scenario.building_runtime.current_time == start


def test_wait_does_not_postpone_an_urgent_print_job() -> None:
    scenario, _, operations, system = _scenario()
    operations.world.schedule.clear()
    now = scenario.building_runtime.current_time
    scenario.get_typed_app(PrintingApp).requests["urgent"] = {
        "status": "pending",
        "ready_by": (now + timedelta(minutes=8)).isoformat(),
        "job_id": None,
    }
    result = system.advance_time(minutes=30)
    assert result["status"] == "rejected"
    assert result["reason"] == "urgent_commitment"
    assert result["bounded_wait"]["urgent_commitments"] == [
        {"type": "print_request", "request_id": "urgent"}
    ]
    assert scenario.building_runtime.current_time == now


def test_legacy_reference_wait_is_not_modified_without_opt_in() -> None:
    scenario = L3_SCENARIO_CLASSES[0]()
    scenario.setup()
    Engine(None, scenario, virtual_time=True)
    system = scenario.get_typed_app(SystemApp)
    operations = scenario.get_typed_app(BuildingOperationsApp)
    assert system.guarded_wait_handler is None
    assert operations.fair_wait_enabled is False
    assert "bounded_wait" not in system.advance_time(minutes=1, stop_on_event=False)


def test_closeout_progress_includes_pending_workers_without_query_counters() -> None:
    scenario, _, operations, _ = _scenario()
    assert operations.closeout_progress_state() is None
    scenario.building_runtime.current_time = operations.operational_end_at
    blockers = [
        {
            "code": "room_agent_events_pending",
            "rooms": [
                {"room_id": "k1324", "event_ids": ["unresolved-event"]},
            ],
        }
    ]
    for run_count in (1, 50, 100):
        combined_status = {
            "can_finish": False,
            "blockers": blockers,
            "worker_run_count": run_count,
            "current_time": str(run_count),
        }
        assert operations.closeout_progress_state(combined_status) == {
            "blockers": blockers
        }
    assert (
        operations.closeout_progress_state({"can_finish": True, "blockers": []}) is None
    )


def test_repeated_plateau_backs_off_notices_but_keeps_five_minute_checks(monkeypatch):
    scenario, engine, operations, system = _scenario()
    operations.world.schedule.clear()
    reading = {"occupied_environment_stable": False, "rooms": [{
        "room_id": "k1324", "occupancy_count": 1, "readings": {"air_temperature_c": 26.0},
        "warnings": [], "uncontrolled_warnings": [], "violations": ["temperature_outside_comfort_band"],
    }]}
    calls = []
    monkeypatch.setattr(operations, "get_environment_control_status", lambda: reading)

    def advance(*, seconds, stop_on_event):
        assert seconds <= 300 and stop_on_event is True
        calls.append(seconds)
        scenario.building_runtime.current_time += timedelta(seconds=seconds)
        engine.time_manager.add_offset(seconds)
        return {"status": "ok", "advanced_seconds": seconds, "interrupted": False}

    monkeypatch.setattr(system, "_advance_seconds", advance)
    for limit in (2, 4, 6):
        result = system.advance_time(hours=1)
        assert result["reason"] == "environment_not_improving"
        assert result["bounded_wait"]["stagnation_check_limit"] == limit
        assert result["bounded_wait"]["time_advance_calls"] == limit
    assert calls == [300] * 12
    # A meaningful control change restores the original first-notice interval.
    operations.world.device_states["k1324_hvac_01"].level = 1
    assert system.advance_time(hours=1)["bounded_wait"]["stagnation_check_limit"] == 2
    # A new alert interrupts the backed-off wait after the very next check.
    checks = iter([reading, {"occupied_environment_stable": False, "rooms": [{
        **reading["rooms"][0], "warnings": ["co2_control_warning"],
    }]}])
    monkeypatch.setattr(operations, "get_environment_control_status", lambda: next(checks))
    result = system.advance_time(hours=1)
    assert result["reason"] == "new_environment_alert"
    assert result["bounded_wait"]["stagnation_check_limit"] == 4
    assert result["bounded_wait"]["time_advance_calls"] == 1
    operations._stagnant_wait_feedback = (
        operations._monitor_control_signature(), operations._environment_signals(reading), 1.0, 2
    )
    checks = iter([reading, {"occupied_environment_stable": False, "rooms": [{
        **reading["rooms"][0], "readings": {"air_temperature_c": 26.4},
    }]}])
    monkeypatch.setattr(operations, "get_environment_control_status", lambda: next(checks))
    result = system.advance_time(hours=1)
    assert result["reason"] == "environment_worsening"
    assert result["bounded_wait"]["stagnation_check_limit"] == 6
    assert result["bounded_wait"]["time_advance_calls"] == 1
