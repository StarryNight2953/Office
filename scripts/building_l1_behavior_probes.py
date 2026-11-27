"""Additional multi-family experiments for the Building L1 diagnostic.

Device experiments use source-derived in-memory boundaries. The booking
experiment is explicitly supplemental: current L3s pre-create their bookings.
"""

from __future__ import annotations

import copy
from datetime import datetime

from smartoffice.apps.building_world import BuildingWorldApp, PrintingApp
from smartoffice.scenarios.building_kechuang.l3.scenarios import L3_SCENARIO_CLASSES
from smartoffice.scenarios.building_kechuang.l3.specs import ZONE_IDS
from scripts.diagnose_building_l3_to_l1 import fresh_engine, invoke


def _capture(engine):
    printing = engine.scenario.get_typed_app(PrintingApp)
    return copy.deepcopy(engine.scenario.building_runtime.snapshot()), copy.deepcopy(
        (printing.requests, printing.jobs)), engine.time_manager.time()


def _restore(engine, checkpoint):
    runtime, printing, clock = checkpoint
    engine.scenario.building_runtime.restore(copy.deepcopy(runtime))
    app = engine.scenario.get_typed_app(PrintingApp)
    app.requests, app.jobs = copy.deepcopy(printing)
    engine.time_manager.reset(start_time=clock)


def _device_matches(device, action):
    args = action["args"]
    checks = {"power_on": device["power_on"] == args["power_on"]}
    if not args["power_on"]:
        checks["off"] = device["mode"] == "off"
    elif action["method"] == "set_lighting":
        checks["lighting"] = all(device["settings"].get(key) == args[key] for key in (
            "brightness_pct", "color_temperature_k", "scene"))
    elif action["method"] == "set_hvac":
        checks["hvac"] = (device["mode"] == args["mode"]
                          and device["level"] == args["fan_level"]
                          and device["target_temperature_c"] == args["target_temperature_c"])
    elif action["method"] == "set_projector":
        checks["projector"] = device["settings"].get("input_source") == args["input_source"]
    elif action["method"] == "set_audio_system":
        checks["audio"] = all(device["settings"].get(key) == args[key]
                              for key in ("volume_pct", "microphone_enabled"))
    elif "level" in args:
        checks["level"] = device["level"] == args["level"]
    return all(checks.values())


def probe_device_candidate(scenario_class, report, steps, candidate):
    """Check a complete room behavior, not one test per device call."""
    engine, tools = fresh_engine(scenario_class)
    boundary = min(step["position"] for step in candidate["source_actions"])
    for step in steps[:boundary]:
        if step["tool"]:
            invoke(tools, step["tool"], step["args"])
    checkpoint = _capture(engine)
    kind = candidate["behavior_family"]
    thermal = kind == "environment_control"
    world = engine.scenario.get_typed_app(BuildingWorldApp)
    original_devices = copy.deepcopy(world.device_states)
    target_ids = {step["args"]["device_id"] for step in candidate["source_actions"]}
    outcomes = {}
    for mode in ("source_reference", "do_nothing", "wrong_setting", "no_confirmation"):
        _restore(engine, checkpoint)
        trace = []

        def call(name, **kwargs):
            result = invoke(tools, name, kwargs)
            trace.append({"tool": name, "args": kwargs, "result": copy.deepcopy(result)})
            return result

        call("BuildingWorldApp__get_recent_building_events")
        call("BuildingWorldApp__get_building_overview")
        call("BuildingOperationsApp__get_commitment_status")
        if thermal:
            zone = ZONE_IDS[candidate["objects"][0]]
            call("BuildingSensorApp__read_zone_sensors", zone_id=zone)
        if mode != "do_nothing":
            for position, step in enumerate(candidate["source_actions"]):
                args = dict(step["args"])
                if mode == "wrong_setting":
                    if thermal and step["method"] == "set_hvac":
                        args.update(mode="heating", target_temperature_c=30.0)
                    elif not thermal and position == 0:
                        if step["method"] == "set_lighting" and args["power_on"]:
                            args["brightness_pct"] = max(1, args["brightness_pct"] - 10)
                        else:
                            # Omit one required device transition, leaving its
                            # inherited state as the wrong-setting control.
                            continue
                call(step["tool"], **args)
        samples = []
        if thermal:
            # Explicit prototype contract: precondition this office within 30
            # minutes, and have two 5-minute-apart readings in [21, 25] C.
            # This tests temperature preparation, not full air-quality comfort.
            for _ in range(6):
                call("SystemApp__advance_time", minutes=5, stop_on_event=False)
                if mode != "no_confirmation":
                    sample = call("BuildingSensorApp__read_zone_sensors", zone_id=zone)
                    temperature = next(item["value"] for item in sample["readings"]
                                       if item["quantity"] == "air_temperature_c")
                    samples.append(float(temperature))
        final = None
        if mode != "no_confirmation":
            final = call("BuildingWorldApp__get_building_overview")
        # State assertions are evaluated even for an arm that never queried.
        final_devices = invoke(tools, "BuildingWorldApp__get_building_overview", {})["devices"]
        by_id = {device["device_id"]: device for device in final_devices}
        latest_actions = {step["args"]["device_id"]: step for step in candidate["source_actions"]}
        configured = all(_device_matches(by_id[key], action) for key, action in latest_actions.items())
        untouched = all(world.device_states[key] == value for key, value in original_devices.items()
                        if key not in target_ids)
        conditions = {"target_configuration": configured, "completion_observed": final is not None,
                      "other_device_configuration_preserved": untouched}
        if thermal:
            conditions["temperature_in_band_last_two_samples"] = (
                len(samples) >= 2 and all(21.0 <= value <= 25.0 for value in samples[-2:]))
        if kind == "power_constrained_adjustment":
            budget = call("BuildingOperationsApp__get_power_budget_status")
            conditions["within_power_limit"] = budget["estimated_configured_max_power_w"] <= candidate["power_limit_w"]
        outcomes[mode] = {"success": all(conditions.values()), "conditions": conditions,
                          "temperature_samples_c": samples, "trace": trace}
    contrast = outcomes["source_reference"]["success"] and all(
        not value["success"] for key, value in outcomes.items() if key != "source_reference")
    return {
        "behavior_family": kind, "candidate_id": candidate["id"],
        "source_l3_id": report["source_l3_id"], "source_fingerprint": report["source_fingerprint"],
        "capture_before": steps[boundary]["id"],
        "capture_sim_time": checkpoint[0]["current_time"].isoformat(),
        "prototype_contrast_passed": contrast, "outcomes": outcomes,
        "status": "prototype_passed" if contrast else "needs_contract_or_policy_review",
        "contract": ("30 分钟温度准备；21–25°C，末两次观测间隔 5 分钟；保持源控制配置"
                     if thermal else "源公开活动/模式对应的设备配置全部满足，并查询确认；保护其它设备"),
        "limitations": ["局部原型；未注册独立 Scenario、未验证磁盘存档",
                        "设备配置对齐不等于整场会议/全楼舒适度通过",
                        "确定性时间推进，真实 Agent 事件中断及长期背景约束另验"],
    }


def probe_booking():
    from smartoffice.scenarios.building_kechuang.scenario_room_booking import (
        END_AT, START_AT, ScenarioBuildingKechuangRoomBooking,
    )
    engine, tools = fresh_engine(ScenarioBuildingKechuangRoomBooking)
    checkpoint = _capture(engine)
    world = engine.scenario.get_typed_app(BuildingWorldApp)
    existing = copy.deepcopy(world.schedule["existing-001"])
    outcomes = {}
    for mode in ("correct", "do_nothing", "conflicting_room", "no_confirmation"):
        _restore(engine, checkpoint)
        trace = []

        def call(name, **kwargs):
            # Rejection is an expected outcome in the conflicting-room arm.
            result = tools[name](**kwargs)
            trace.append({"tool": name, "args": kwargs, "result": copy.deepcopy(result)})
            return result

        call("OccupancyApp__get_person_presence", person_id="professor-01")
        call("ScheduleApp__get_person_schedule", person_id="professor-01", start_at=START_AT, end_at=END_AT)
        rooms = call("RoomApp__find_available_rooms", start_at=START_AT, end_at=END_AT,
                     min_capacity=6, required_capabilities=["projector"])
        choices = rooms["available_rooms"]
        room_id = choices[0]["room_id"]
        if mode == "conflicting_room":
            room_id = "k1315"
        result = None
        if mode != "do_nothing":
            result = call("ScheduleApp__create_meeting", request_id="request-l1-probe",
                          organizer_id="student-01", participant_ids=["professor-01"],
                          room_id=room_id, start_at=START_AT, end_at=END_AT,
                          expected_attendees=6, required_capabilities=["projector"], title="Consultation")
        observed = False
        if mode != "no_confirmation":
            agenda = call("ScheduleApp__get_room_schedule", room_id=room_id,
                          start_at=START_AT, end_at=END_AT)
            observed = bool(result and result.get("status") == "confirmed" and any(
                row["meeting_id"] == result["meeting"]["meeting_id"] for row in agenda["meetings"]))
        created = [meeting for key, meeting in world.schedule.items() if key != "existing-001"]
        meeting = created[0] if len(created) == 1 else None
        reservation = world.reservations.get(meeting.reservation_id) if meeting else None
        checks = {
            "one_correct_meeting": bool(meeting and meeting.room_id in {item["room_id"] for item in choices}
                                        and meeting.expected_attendees == 6
                                        and meeting.start_at == datetime.fromisoformat(START_AT)
                                        and meeting.end_at == datetime.fromisoformat(END_AT)
                                        and meeting.participant_ids == ("professor-01",)
                                        and meeting.status.value == "confirmed"),
            "linked_active_reservation": bool(reservation and reservation.resource_id == meeting.room_id
                                              and reservation.status.value == "active"),
            "existing_meeting_unchanged": world.schedule["existing-001"] == existing,
            "completion_observed": observed,
        }
        outcomes[mode] = {"success": all(checks.values()), "conditions": checks, "trace": trace}
    return {
        "behavior_family": "room_booking",
        "source_l3_id": None,
        "source_fixture": "scenario_building_kechuang_room_booking",
        "provenance": "supplemental_existing_building_fixture_not_extracted_from_twenty_l3s",
        "prototype_contrast_passed": outcomes["correct"]["success"] and all(
            not value["success"] for key, value in outcomes.items() if key != "correct"),
        "outcomes": outcomes,
        "limitations": ["当前 20 个 L3 预置预约，缺少实际创建预约的源动作；本试验验证标准适用于预约能力"],
    }


def probe_additional_behaviors(analyzed: dict) -> list[dict]:
    results = []
    for number, family in ((6, "environment_control"), (8, "lighting_service"),
                           (8, "meeting_equipment"), (6, "room_recovery"),
                           (16, "power_constrained_adjustment")):
        if number not in analyzed:
            continue
        report, steps = analyzed[number]
        candidates = [item for item in report["candidates"] if item["behavior_family"] == family]
        if family == "power_constrained_adjustment":
            candidates = [item for item in candidates if len(item["objects"]) >= 2]
        if candidates:
            results.append(probe_device_candidate(L3_SCENARIO_CLASSES[number - 1],
                                                  report, steps, candidates[0]))
            if family == "environment_control":
                # Alternative task contract, not an additional counted leaf:
                # activating HVAC does not claim to finish room conditioning.
                activation = copy.deepcopy(candidates[0])
                activation["id"] += "-activation-alternative"
                activation["behavior_family"] = "hvac_activation"
                activation["source_actions"] = [step for step in activation["source_actions"]
                                                 if step["method"] == "set_hvac"]
                alternative = probe_device_candidate(L3_SCENARIO_CLASSES[number - 1],
                                                       report, steps, activation)
                alternative["provenance"] = "alternative_narrower_contract_not_additional_counted_leaf"
                alternative["contract"] = "启动并设置指定空调后查询确认，不声称完成室温调节"
                results.append(alternative)
    results.append(probe_booking())
    return results
