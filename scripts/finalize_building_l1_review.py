"""Consolidate Building L3 evidence into reusable business-capability L1s.

Each source L3 is replayed once for traceability. Time, room, initial state,
target parameters and execution branch become coverage variants; they do not
create new L1 identities. Run without ``--apply`` for a preview, then use
``--apply`` to refresh the formal manifest and audit ledgers.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fairy.apps.building_world import BuildingWorldApp, PrintingApp
from fairy.scenarios.building_kechuang.l1.manifest import (
    MANIFEST_PATH,
    validate_manifest,
    validate_source_links,
)
from fairy.scenarios.building_kechuang.l3.scenarios import L3_SCENARIO_CLASSES
from fairy.scenarios.building_kechuang.scenario_room_booking import (
    ScenarioBuildingKechuangRoomBooking,
)
from scripts.diagnose_building_l3_to_l1 import analyze_source, fresh_engine, invoke

FORMAL_DIR = ROOT / "workflow_exports" / "building_l3_to_l1_formal"
REVIEW_LEDGER_PATH = FORMAL_DIR / "review_ledger.json"
OBLIGATION_LEDGER_PATH = FORMAL_DIR / "obligation_ledger.json"
REVIEWED_AT = "2026-09-12"

CAPABILITY_DEFINITIONS = {
    "hvac_control": {
        "family": "object_control",
        "title": "空调控制",
        "goal": "按当前温控需求调节一台空调并确认最终配置。",
        "dimensions": ["设备", "房间", "时间", "初始开关与模式", "目标温度", "风量"],
    },
    "ventilation_control": {
        "family": "object_control",
        "title": "新风控制",
        "goal": "按当前空气需求调节一台新风机并确认最终配置。",
        "dimensions": ["设备", "房间", "时间", "初始状态", "开关", "档位"],
    },
    "humidifier_control": {
        "family": "object_control",
        "title": "加湿器控制",
        "goal": "按当前湿度需求调节一台加湿器并确认最终配置。",
        "dimensions": ["设备", "房间", "时间", "初始状态", "开关", "档位"],
    },
    "air_purifier_control": {
        "family": "object_control",
        "title": "空气净化器控制",
        "goal": "按当前空气质量需求调节一台空气净化器并确认最终配置。",
        "dimensions": ["设备", "房间", "时间", "初始状态", "开关", "档位"],
    },
    "lighting_control": {
        "family": "object_control",
        "title": "照明控制",
        "goal": "按当前照明需求调节灯或灯组并确认最终配置。",
        "dimensions": ["灯组", "房间", "时间", "初始状态", "亮度", "色温", "场景"],
    },
    "projector_control": {
        "family": "object_control",
        "title": "投影仪控制",
        "goal": "按当前演示需求调节投影仪并确认最终配置。",
        "dimensions": ["设备", "房间", "时间", "初始状态", "开关", "输入源"],
    },
    "audio_system_control": {
        "family": "object_control",
        "title": "音频系统控制",
        "goal": "按当前会议需求调节音频系统并确认最终配置。",
        "dimensions": ["设备", "房间", "时间", "初始状态", "音量", "麦克风"],
    },
    "printer_control": {
        "family": "object_control",
        "title": "打印机控制与交付",
        "goal": "按公开打印需求完成设备检查、提交、等待和结果确认。",
        "dimensions": ["打印机", "时间", "初始开关", "队列", "文档", "数量", "优先级", "截止时间"],
    },
    "meeting_room_booking": {
        "family": "business_workflow",
        "title": "会议室预约",
        "goal": "检查人员、日程和房间冲突后创建有效会议预约并确认。",
        "dimensions": ["人员", "人数", "时间", "房间偏好", "设备能力", "冲突状态"],
    },
    "meeting_requirement_change": {
        "family": "business_workflow",
        "title": "会议需求变更",
        "goal": "根据用户变更修订会议与房间承诺并确认资源一致性。",
        "dimensions": ["变更类型", "人员", "人数", "时间", "房间", "设备需求"],
    },
    "meeting_room_recovery": {
        "family": "business_workflow",
        "title": "会议结束后的房间恢复",
        "goal": "会议结束后结合后续承诺恢复房间状态并确认。",
        "dimensions": ["房间", "结束时间", "继承状态", "后续预约", "恢复配置"],
    },
    "power_constrained_adjustment": {
        "family": "business_workflow",
        "title": "共享功率约束下的服务调整",
        "goal": "在共享功率预算内协调多个设备或房间并确认服务配置。",
        "dimensions": ["功率上限", "房间集合", "背景负载", "服务目标", "设备组合"],
    },
}

METHOD_CAPABILITIES = {
    "set_hvac": "hvac_control",
    "set_ventilation": "ventilation_control",
    "set_humidifier": "humidifier_control",
    "set_air_purifier": "air_purifier_control",
    "set_lighting": "lighting_control",
    "set_projector": "projector_control",
    "set_audio_system": "audio_system_control",
    "set_printer_power": "printer_control",
    "submit_print_job": "printer_control",
}

CAPABILITY_SCENARIO_IDS = {
    key: f"scenario_building_kechuang_l1_{key}"
    for key in CAPABILITY_DEFINITIONS
}


def device_projection(world: BuildingWorldApp, device_id: str) -> dict[str, Any]:
    state = world.device_states[device_id]
    return {
        "device_id": device_id,
        "power_on": state.power_on,
        "mode": state.mode,
        "level": state.level,
        "target_temperature_c": state.target_temperature_c,
        "health": state.health.value,
        "water_level_pct": state.water_level_pct,
        "filter_life_pct": state.filter_life_pct,
        "settings": copy.deepcopy(state.settings),
    }


def capture_fixture(engine) -> dict[str, Any]:
    scenario = engine.scenario
    world = scenario.get_typed_app(BuildingWorldApp)
    printing = scenario.get_typed_app(PrintingApp)
    world_snapshot = world.snapshot()
    return {
        "captured_at": scenario.building_runtime.current_time.isoformat(),
        "devices": [
            device_projection(world, key) for key in sorted(world.device_states)
        ],
        "rooms": [
            {"room_id": key, "occupancy_count": value.occupancy_count}
            for key, value in sorted(world.room_states.items())
        ],
        "schedule": copy.deepcopy(world_snapshot["schedule"]),
        "reservations": copy.deepcopy(world_snapshot["reservations"]),
        "print_requests": [
            copy.deepcopy(printing.requests[key]) for key in sorted(printing.requests)
        ],
        "print_jobs": [normalized_print_job(printing.jobs[key]) for key in sorted(printing.jobs)],
        "id_counters": dict(sorted(world._id_counters.items())),
    }


def normalized_print_job(job: dict[str, Any]) -> dict[str, Any]:
    """Remove TimeManager wall-clock microsecond drift from source fixtures."""

    result = copy.deepcopy(job)
    if result.get("submitted_at"):
        submitted = datetime.fromisoformat(result["submitted_at"])
        result["submitted_at"] = submitted.replace(microsecond=0).isoformat()
    for key in ("started_at_timestamp", "ready_at_timestamp"):
        if key in result:
            result[key] = float(round(float(result[key])))
    return result


def action_projection(engine, action: dict[str, Any]) -> Any:
    world = engine.scenario.get_typed_app(BuildingWorldApp)
    printing = engine.scenario.get_typed_app(PrintingApp)
    if action["method"] == "submit_print_job":
        return {
            "jobs": copy.deepcopy(printing.jobs),
            "requests": copy.deepcopy(printing.requests),
            "id_counters": copy.deepcopy(world._id_counters),
        }
    device_id = action.get("args", {}).get("device_id")
    if device_id is None:
        device_id = action.get("args", {}).get("printer_id")
    return None if device_id is None else device_projection(world, device_id)


def replay_source_boundaries(
    scenario_class, report: dict[str, Any], steps: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Capture each candidate just before its first source action."""

    engine, tools = fresh_engine(scenario_class)
    candidates = {item["id"]: item for item in report["candidates"]}
    action_owner = {
        action_id: candidate_id
        for candidate_id, candidate in candidates.items()
        for action_id in candidate["source_action_ids"]
    }
    positions = {step["id"]: step["position"] for step in steps}
    first_action = {
        candidate_id: min(candidate["source_action_ids"], key=positions.__getitem__)
        for candidate_id, candidate in candidates.items()
    }
    captures = {
        candidate_id: {"initial_state": None, "actions": []}
        for candidate_id in candidates
    }
    for step in steps:
        candidate_id = action_owner.get(step["id"])
        if candidate_id is not None and first_action[candidate_id] == step["id"]:
            fixture = capture_fixture(engine)
            fixture["captured_at"] = (
                engine.scenario.spec.start_at
                + timedelta(minutes=float(candidates[candidate_id]["start_minute"]))
            ).isoformat()
            captures[candidate_id]["initial_state"] = fixture
        before = action_projection(engine, step) if candidate_id is not None else None
        if step["tool"]:
            invoke(tools, step["tool"], step["args"])
        if candidate_id is not None:
            after = action_projection(engine, step)
            captures[candidate_id]["actions"].append(
                {
                    "action_id": step["id"],
                    "tool": step["tool"],
                    "method": step["method"],
                    "args": copy.deepcopy(step["args"]),
                    "source_position": step["position"],
                    "source_minute": step["minute"],
                    "meaningful_state_transition": before != after,
                }
            )
    missing = [
        candidate_id
        for candidate_id, capture in captures.items()
        if capture["initial_state"] is None
        or len(capture["actions"])
        != len(candidates[candidate_id]["source_action_ids"])
    ]
    if missing:
        raise RuntimeError(f"incomplete source boundary capture: {missing}")
    return captures


def action_target(action: dict[str, Any]) -> tuple[str, str]:
    if action["method"] == "submit_print_job":
        return "print_request", action["action_id"]
    args = action["args"]
    target = args.get("device_id", args.get("printer_id", action["action_id"]))
    return "device", str(target)


def consolidate_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for action in actions:
        grouped[action_target(action)].append(action)
    result = []
    for group in grouped.values():
        final = copy.deepcopy(max(group, key=lambda item: item["source_position"]))
        final["source_action_ids"] = [item["action_id"] for item in group]
        result.append(final)
    return sorted(result, key=lambda item: item["source_position"])


def format_action(action: dict[str, Any]) -> str:
    args, method = action["args"], action["method"]
    device = args.get("device_id", args.get("printer_id", ""))
    if method == "set_hvac":
        return (
            f"关闭 {device} 空调"
            if not args["power_on"]
            else f"将 {device} 空调设为 {args['mode']}、{args['target_temperature_c']:g}°C、风量 {args['fan_level']}"
        )
    if method in {"set_ventilation", "set_humidifier", "set_air_purifier"}:
        name = {
            "set_ventilation": "新风机",
            "set_humidifier": "加湿器",
            "set_air_purifier": "空气净化器",
        }[method]
        setting = f"开启、档位 {args['level']}" if args["power_on"] else "关闭"
        return f"将 {device} {name}设为{setting}"
    if method == "set_lighting":
        return (
            f"关闭 {device} 照明"
            if not args["power_on"]
            else f"将 {device} 照明设为亮度 {args['brightness_pct']}%、色温 {args['color_temperature_k']}K、{args['scene']} 场景"
        )
    if method == "set_projector":
        setting = f"开启、输入 {args['input_source']}" if args["power_on"] else "关闭"
        return f"将 {device} 投影仪设为{setting}"
    if method == "set_audio_system":
        if not args["power_on"]:
            return f"关闭 {device} 音频系统"
        microphone = "启用" if args["microphone_enabled"] else "禁用"
        return f"将 {device} 音频系统设为音量 {args['volume_pct']}%、{microphone}麦克风"
    if method == "set_printer_power":
        return f"{'开启' if args['power_on'] else '关闭'} {device} 打印机"
    if method == "submit_print_job":
        return (
            f"向 {device} 提交《{args['document_name']}》："
            f"{args['copies']} 份×{args['pages_per_copy']} 页，优先级 {args['priority']}"
        )
    raise ValueError(f"unsupported source method: {method}")


def step_id(index: int, action: dict[str, Any]) -> str:
    target = re.sub(r"[^a-z0-9]+", "_", action_target(action)[1].lower()).strip("_")
    return f"apply_{index:02d}_{action['method']}_{target}"[:110]


def oracle_steps(
    candidate: dict[str, Any], actions: list[dict[str, Any]], fixture: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str], int]:
    family = candidate["behavior_family"]
    if family == "printing_fulfillment":
        steps = [
            {
                "id": "inspect_public_print_requests",
                "tool": "PrintingApp__get_print_requests",
                "args": {"status": "all"},
            },
            {
                "id": "inspect_printer_state",
                "tool": "BuildingWorldApp__get_building_overview",
                "args": {},
            },
        ]
    else:
        steps = [
            {
                "id": "inspect_initial_devices",
                "tool": "BuildingWorldApp__get_building_overview",
                "args": {},
            }
        ]
    if family == "power_constrained_adjustment":
        steps.append(
            {
                "id": "inspect_initial_power_budget",
                "tool": "BuildingOperationsApp__get_power_budget_status",
                "args": {},
            }
        )
    for index, action in enumerate(actions, 1):
        steps.append(
            {
                "id": step_id(index, action),
                "tool": action["tool"],
                "args": copy.deepcopy(action["args"]),
                "source_action_ids": list(action["source_action_ids"]),
            }
        )
    if family == "printing_fulfillment":
        request_ids = set(candidate["objects"])
        requests = [
            item for item in fixture["print_requests"] if item["request_id"] in request_ids
        ]
        if len(requests) != len(request_ids):
            raise RuntimeError(f"{candidate['id']}: request absent at source boundary")
        start_at = datetime.fromisoformat(fixture["captured_at"])
        ready_at = max(datetime.fromisoformat(item["ready_by"]) for item in requests)
        wait_seconds = max(1, math.ceil((ready_at - start_at).total_seconds()) + 1)
        steps.extend(
            [
                {
                    "id": "wait_for_print_completion",
                    "tool": "SystemApp__advance_time",
                    "args": {"seconds": wait_seconds, "stop_on_event": False},
                },
                {
                    "id": "confirm_print_requests",
                    "tool": "PrintingApp__get_print_requests",
                    "args": {"status": "all"},
                },
            ]
        )
        return steps, ["PrintingApp__get_print_requests"], wait_seconds + 300
    steps.append(
        {
            "id": "confirm_target_configuration",
            "tool": "BuildingWorldApp__get_building_overview",
            "args": {},
        }
    )
    confirmations = ["BuildingWorldApp__get_building_overview"]
    duration = 900
    if family == "power_constrained_adjustment":
        steps.append(
            {
                "id": "confirm_power_budget",
                "tool": "BuildingOperationsApp__get_power_budget_status",
                "args": {},
            }
        )
        confirmations.append("BuildingOperationsApp__get_power_budget_status")
        duration = 1200
    return steps, confirmations, duration


def capability_for_action(candidate: dict[str, Any], action: dict[str, Any]) -> str:
    family = candidate["behavior_family"]
    if family == "room_recovery":
        return "meeting_room_recovery"
    if family == "power_constrained_adjustment":
        return "power_constrained_adjustment"
    if family == "printing_fulfillment":
        return "printer_control"
    try:
        return METHOD_CAPABILITIES[action["method"]]
    except KeyError as exc:
        raise RuntimeError(f"unmapped Building source action: {action}") from exc


def diagnostic_file(report: dict[str, Any]) -> str:
    return (
        "workflow_exports/building_l3_to_l1_diagnostic/"
        f"l3_{report['number']:02d}_{report['slug']}.json"
    )


def source_variant(
    report: dict[str, Any],
    candidate: dict[str, Any],
    capture: dict[str, Any],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "kind": "diagnostic_candidate",
        "l3_scenario_id": report["source_l3_id"],
        "candidate_id": candidate["id"],
        "diagnostic_file": diagnostic_file(report),
        "source_fingerprint": report["source_fingerprint"],
        "action_ids": [action["action_id"] for action in actions],
        "evidence_ids": list(candidate.get("source_evidence_ids", [])),
        "captured_at": capture["initial_state"]["captured_at"],
        "trigger_labels": list(candidate.get("trigger_labels", [])),
        "objects": list(candidate.get("objects", [])),
        "action_parameters": [
            {
                "action_id": action["action_id"],
                "tool": action["tool"],
                "args": copy.deepcopy(action["args"]),
                "meaningful_state_transition": action["meaningful_state_transition"],
            }
            for action in actions
        ],
    }


def capability_class_name(capability_id: str) -> str:
    return "ScenarioBuildingKechuangL1" + "".join(
        part.capitalize() for part in capability_id.split("_")
    )


def capability_task(
    capability_id: str,
    candidate: dict[str, Any],
    actions: list[dict[str, Any]],
    power_limit: float | None,
) -> dict[str, str]:
    definition = CAPABILITY_DEFINITIONS[capability_id]
    clauses = "；".join(format_action(action) for action in actions)
    if capability_id == "printer_control":
        objects = "、".join(candidate.get("objects", [])) or "当前打印需求"
        return {
            "goal": definition["goal"],
            "prompt": (
                f"当前代表性输入包含 {objects}。先查询公开需求、打印机和队列状态，"
                f"再执行：{clauses}。等待相关作业完成后查询确认请求关联、文档、数量、"
                "优先级和截止时间，不得重复提交，最后报告。房间、时间、队列和材料参数"
                "均为本能力的输入变体，不产生新的 L1。"
            ),
            "completion_report": "打印需求已按输入完成并查询确认，未产生重复作业。",
        }
    if capability_id == "meeting_room_recovery":
        return {
            "goal": definition["goal"],
            "prompt": (
                f"当前代表性会议已经结束。先检查房间、设备和后续承诺，再执行：{clauses}。"
                "查询确认恢复结果且不得损坏其它会议或预约，最后报告。具体房间、结束时间、"
                "继承状态和恢复参数是同一 L1 的输入变体。"
            ),
            "completion_report": "会议结束后的房间状态已恢复并确认，后续承诺未受影响。",
        }
    if capability_id == "power_constrained_adjustment":
        return {
            "goal": definition["goal"],
            "prompt": (
                f"当前代表性共享功率上限为 {power_limit:g}W。先查询各房间设备与功率预算，"
                f"再联合执行：{clauses}。完成后查询目标配置与功率预算，只有服务配置正确且"
                "未超限时才报告。具体房间、负载、参数和时刻是同一 L1 的输入变体。"
            ),
            "completion_report": "共享功率约束下的服务调整已完成，目标配置和预算均已确认。",
        }
    return {
        "goal": definition["goal"],
        "prompt": (
            f"当前代表性需求要求：{clauses}。先查询目标对象及现态，完成调节后再次查询确认，"
            "不得改变无关对象，最后报告。设备、房间、时间、初态和目标参数是同一能力的"
            "输入变体，不产生新的 L1。"
        ),
        "completion_report": f"{definition['title']}已按输入完成并查询确认。",
    }


def representative_sort_key(capability_id: str, option: dict[str, Any]) -> tuple:
    action_count = len(consolidate_actions(option["actions"]))
    if capability_id in {
        "printer_control",
        "meeting_room_recovery",
        "power_constrained_adjustment",
    }:
        action_count = -action_count
    return (
        action_count,
        option["report"]["number"],
        min(action["source_position"] for action in option["actions"]),
    )


def build_capability_contract(
    capability_id: str,
    option: dict[str, Any],
    variants: list[dict[str, Any]],
    obligation_refs: list[dict[str, str]],
) -> dict[str, Any]:
    report = option["report"]
    candidate = option["candidate"]
    source_actions = option["actions"]
    actions = consolidate_actions(source_actions)
    fixture = copy.deepcopy(option["capture"]["initial_state"])
    start_at = fixture["captured_at"]
    power_limit = (
        float(candidate["power_limit_w"])
        if capability_id == "power_constrained_adjustment"
        else None
    )
    if power_limit is not None:
        fixture["operations"] = {
            "power_limit_w": power_limit,
            "target_temperature_c": 24.0,
            "max_co2_ppm": 1000.0,
            "max_pm25_ug_m3": 50.0,
        }
    oracle_candidate = copy.deepcopy(candidate)
    oracle_candidate["behavior_family"] = {
        "printer_control": "printing_fulfillment",
        "meeting_room_recovery": "room_recovery",
        "power_constrained_adjustment": "power_constrained_adjustment",
    }.get(capability_id, "object_control")
    steps, confirmations, duration = oracle_steps(oracle_candidate, actions, fixture)
    fixture.pop("captured_at")
    definition = CAPABILITY_DEFINITIONS[capability_id]
    scenario_id = CAPABILITY_SCENARIO_IDS[capability_id]
    action_ids = [action["action_id"] for action in source_actions]
    validation: dict[str, Any] = {
        "kind": "printing" if capability_id == "printer_control" else "device_state",
        "confirmation_tools": confirmations,
        "preserve_untargeted_devices": True,
        "meaningful_source_action_ids": [
            action["action_id"]
            for action in source_actions
            if action["meaningful_state_transition"]
        ],
    }
    if validation["kind"] == "printing":
        validation["request_ids"] = list(candidate["objects"])
    if power_limit is not None:
        validation["power_limit_w"] = power_limit
    return {
        "scenario_id": scenario_id,
        "class_name": capability_class_name(capability_id),
        "review_status": "accepted_l1",
        "review": {
            "decision": "accepted_l1",
            "reviewed_at": REVIEWED_AT,
            "basis": [
                "以同一主对象或同一业务目标的完整闭环定义 L1 身份",
                "时间、房间、初态、参数和执行分支只作为覆盖变体",
                "代表性实例保留查询、执行、等待（如需要）和结果确认",
            ],
            "limitation": (
                "代表性 Oracle 验证一组具体输入；coverage 保存全部源变体。"
                "对象控制只声明设备配置，除非合同另有物理稳定判据。"
            ),
        },
        "behavior_family": capability_id,
        "capability_kind": definition["family"],
        "start_at": start_at,
        "duration_seconds": duration,
        "source": {
            "kind": "diagnostic_representative",
            "l3_scenario_id": report["source_l3_id"],
            "candidate_id": candidate["id"],
            "candidate_status_at_diagnosis": candidate["status"],
            "contract_variant": capability_id,
            "diagnostic_file": diagnostic_file(report),
            "source_fingerprint": report["source_fingerprint"],
            "action_ids": action_ids,
            "evidence_ids": list(candidate.get("source_evidence_ids", [])),
        },
        "coverage": {
            "identity_rule": "same_business_goal_and_primary_object",
            "varied_dimensions": list(definition["dimensions"]),
            "merged_variant_count": len(variants),
            "source_l3_ids": sorted({item["l3_scenario_id"] for item in variants}),
            "source_variants": sorted(
                variants,
                key=lambda item: (
                    item["l3_scenario_id"],
                    item.get("candidate_id", ""),
                    item["action_ids"],
                ),
            ),
            "obligation_refs": sorted(
                obligation_refs,
                key=lambda item: (item["source_l3_id"], item["obligation_id"]),
            ),
        },
        "task": capability_task(capability_id, candidate, actions, power_limit),
        "initial_state": fixture,
        "oracle_steps": steps,
        "validation": validation,
    }


def _supplemental_base_fixture() -> dict[str, Any]:
    scenario = ScenarioBuildingKechuangRoomBooking()
    scenario.initiate_scenario()
    fixture = capture_fixture(SimpleNamespace(scenario=scenario))
    fixture.pop("captured_at")
    return fixture


def _supplemental_contract(
    capability_id: str,
    fixture: dict[str, Any],
    oracle_steps_value: list[dict[str, Any]],
    validation: dict[str, Any],
    task: dict[str, str],
    obligation_refs: list[dict[str, str]],
) -> dict[str, Any]:
    definition = CAPABILITY_DEFINITIONS[capability_id]
    scenario_id = CAPABILITY_SCENARIO_IDS[capability_id]
    action_ids = sorted(
        {
            source_id
            for step in oracle_steps_value
            for source_id in step.get("source_action_ids", [])
        }
    )
    return {
        "scenario_id": scenario_id,
        "class_name": capability_class_name(capability_id),
        "review_status": "accepted_l1",
        "review": {
            "decision": "accepted_l1",
            "reviewed_at": REVIEWED_AT,
            "basis": [
                "用户明确将该端到端业务闭环定义为 L1",
                "20 个 L3 中的重复义务作为输入变体合并到一个能力",
                "补充夹具通过真实日程和资源工具验证正例与负例",
            ],
            "limitation": "源 L3 将该事实预置或自动应用；补充夹具验证能力，不伪称源 Oracle 含这些 Agent 动作。",
        },
        "behavior_family": capability_id,
        "capability_kind": definition["family"],
        "start_at": "2026-09-09T09:00:00+08:00",
        "duration_seconds": 900,
        "source": {
            "kind": "supplemental_fixture",
            "fixture_scenario_id": "scenario_building_kechuang_room_booking",
            "contract_variant": capability_id,
            "action_ids": action_ids,
        },
        "coverage": {
            "identity_rule": "same_business_goal_and_primary_object",
            "varied_dimensions": list(definition["dimensions"]),
            "merged_variant_count": 0,
            "source_l3_ids": sorted(
                {item["source_l3_id"] for item in obligation_refs}
            ),
            "source_variants": [],
            "obligation_refs": sorted(
                obligation_refs,
                key=lambda item: (item["source_l3_id"], item["obligation_id"]),
            ),
        },
        "task": task,
        "initial_state": fixture,
        "oracle_steps": oracle_steps_value,
        "validation": validation,
    }


def build_supplemental_contracts(
    obligation_refs_by_capability: dict[str, list[dict[str, str]]]
) -> list[dict[str, Any]]:
    start_at = "2026-09-10T14:00:00+08:00"
    end_at = "2026-09-10T15:00:00+08:00"
    expected_booking = {
        "room_id": "k1316",
        "organizer_id": "student-01",
        "participant_ids": ["professor-01"],
        "start_at": start_at,
        "end_at": end_at,
        "expected_attendees": 6,
        "required_capabilities": ["projector"],
    }
    booking_steps = [
        {"id": "check_person_presence", "tool": "OccupancyApp__get_person_presence", "args": {"person_id": "professor-01"}},
        {"id": "check_person_schedule", "tool": "ScheduleApp__get_person_schedule", "args": {"person_id": "professor-01", "start_at": start_at, "end_at": end_at}},
        {"id": "find_available_room", "tool": "RoomApp__find_available_rooms", "args": {"start_at": start_at, "end_at": end_at, "min_capacity": 6, "required_capabilities": ["projector"]}},
        {"id": "create_meeting", "tool": "ScheduleApp__create_meeting", "args": {"request_id": "request-001", **expected_booking, "title": "Student consultation"}, "source_action_ids": ["supplemental_create_meeting"]},
        {"id": "confirm_person_schedule", "tool": "ScheduleApp__get_person_schedule", "args": {"person_id": "professor-01", "start_at": start_at, "end_at": end_at}},
        {"id": "confirm_room_schedule", "tool": "ScheduleApp__get_room_schedule", "args": {"room_id": "k1316", "start_at": start_at, "end_at": end_at}},
        {"id": "confirm_reservation", "tool": "ResourceAllocationApp__get_active_reservations", "args": {"room_id": "k1316"}},
    ]
    booking = _supplemental_contract(
        "meeting_room_booking",
        _supplemental_base_fixture(),
        booking_steps,
        {
            "kind": "meeting_booking",
            "confirmation_tools": [
                "ScheduleApp__get_person_schedule",
                "ScheduleApp__get_room_schedule",
                "ResourceAllocationApp__get_active_reservations",
            ],
            "meaningful_source_action_ids": ["supplemental_create_meeting"],
            "expected_meeting": expected_booking,
        },
        {
            "goal": CAPABILITY_DEFINITIONS["meeting_room_booking"]["goal"],
            "prompt": (
                ScenarioBuildingKechuangRoomBooking.scenario_input
                + "\n创建会议后，必须重新查询教授日程、最终房间日程和该房间的有效预约，"
                "确认会议与预约一致后再报告结果。"
                "\n房间、时间、人数、设备需求和冲突状态是同一预约能力的输入变体。"
            ),
            "completion_report": "会议室预约已创建，并已查询会议与有效资源预约确认。",
        },
        obligation_refs_by_capability["meeting_room_booking"],
    )

    change_fixture = _supplemental_base_fixture()
    old_meeting = change_fixture["schedule"][0]
    old_meeting["reservation_id"] = "reservation-0001"
    change_fixture["reservations"] = [
        {
            "reservation_id": "reservation-0001",
            "resource_id": old_meeting["room_id"],
            "owner_id": "existing-request-001",
            "start_at": old_meeting["start_at"],
            "end_at": old_meeting["end_at"],
            "status": "active",
        }
    ]
    change_fixture["id_counters"]["reservation"] = 1
    expected_change = {
        "room_id": "k1316",
        "organizer_id": "staff-01",
        "participant_ids": ["professor-01"],
        "start_at": start_at,
        "end_at": end_at,
        "expected_attendees": 6,
        "required_capabilities": ["projector"],
    }
    change_steps = [
        {"id": "inspect_old_meeting", "tool": "ScheduleApp__get_room_schedule", "args": {"room_id": "k1315", "start_at": start_at, "end_at": end_at}},
        {"id": "cancel_old_meeting", "tool": "ScheduleApp__cancel_meeting", "args": {"meeting_id": "existing-001", "requester_id": "staff-01", "reason": "attendance reduced; move to smaller suitable room"}, "source_action_ids": ["supplemental_cancel_old_meeting"]},
        {"id": "find_replacement_room", "tool": "RoomApp__find_available_rooms", "args": {"start_at": start_at, "end_at": end_at, "min_capacity": 6, "required_capabilities": ["projector"]}},
        {"id": "create_replacement_meeting", "tool": "ScheduleApp__create_meeting", "args": {"request_id": "change-request-001", **expected_change, "title": "Rescheduled consultation"}, "source_action_ids": ["supplemental_create_replacement_meeting"]},
        {"id": "confirm_changed_person_schedule", "tool": "ScheduleApp__get_person_schedule", "args": {"person_id": "professor-01", "start_at": start_at, "end_at": end_at}},
        {"id": "confirm_changed_room_schedule", "tool": "ScheduleApp__get_room_schedule", "args": {"room_id": "k1316", "start_at": start_at, "end_at": end_at}},
        {"id": "confirm_changed_reservation", "tool": "ResourceAllocationApp__get_active_reservations", "args": {"room_id": "k1316"}},
    ]
    change = _supplemental_contract(
        "meeting_requirement_change",
        change_fixture,
        change_steps,
        {
            "kind": "meeting_change",
            "confirmation_tools": [
                "ScheduleApp__get_person_schedule",
                "ScheduleApp__get_room_schedule",
                "ResourceAllocationApp__get_active_reservations",
            ],
            "meaningful_source_action_ids": [
                "supplemental_cancel_old_meeting",
                "supplemental_create_replacement_meeting",
            ],
            "old_meeting_id": "existing-001",
            "old_reservation_id": "reservation-0001",
            "expected_meeting": expected_change,
        },
        {
            "goal": CAPABILITY_DEFINITIONS["meeting_requirement_change"]["goal"],
            "prompt": (
                "staff-01 需要变更现有会议 existing-001：人数由 10 人减少为 6 人，"
                "新时间为 2026-09-10 14:00 至 15:00，参与者包括 professor-01，"
                "且仍需要投影设备。请先查询旧会议和冲突并确认 staff-01 的权限，"
                "再取消旧会议、释放旧预约，并选择容量和设备均合适的较小可预约房间，"
                "创建替代会议。创建后必须重新查询 professor-01 的日程、最终房间日程"
                "和该房间的有效预约，确认替代会议与预约一致后再报告结果。"
                "变更类型、时刻、人员和目标房间均为输入变体。"
            ),
            "completion_report": "会议需求变更已处理，旧承诺已释放，替代会议和预约已确认。",
        },
        obligation_refs_by_capability["meeting_requirement_change"],
    )
    return [booking, change]


def build_full_review(
    source_manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    del source_manifest  # Rebuild from current L3 evidence; old L1 instances are not seeds.
    reports: list[dict[str, Any]] = []
    candidate_records: list[dict[str, Any]] = []
    action_records: list[dict[str, Any]] = []
    context_action_records: list[dict[str, Any]] = []
    obligation_records: list[dict[str, Any]] = []
    options_by_capability: dict[str, list[dict[str, Any]]] = defaultdict(list)
    variants_by_capability: dict[str, list[dict[str, Any]]] = defaultdict(list)
    obligation_refs_by_capability: dict[str, list[dict[str, str]]] = defaultdict(list)
    candidate_outputs_by_l3: dict[str, dict[str, list[str]]] = {}

    for scenario_class in L3_SCENARIO_CLASSES:
        report, steps = analyze_source(scenario_class)
        reports.append(report)
        captures = replay_source_boundaries(scenario_class, report, steps)
        candidate_outputs: dict[str, list[str]] = {}
        for candidate in report["candidates"]:
            capture = captures[candidate["id"]]
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for action in capture["actions"]:
                grouped[capability_for_action(candidate, action)].append(action)
            scenario_ids = sorted(CAPABILITY_SCENARIO_IDS[key] for key in grouped)
            candidate_outputs[candidate["id"]] = scenario_ids
            for capability_id, actions in grouped.items():
                variants_by_capability[capability_id].append(
                    source_variant(report, candidate, capture, actions)
                )
                if any(action["meaningful_state_transition"] for action in actions):
                    options_by_capability[capability_id].append(
                        {
                            "report": report,
                            "candidate": candidate,
                            "capture": capture,
                            "actions": actions,
                        }
                    )
                for action in actions:
                    action_records.append(
                        {
                            "source_l3_id": report["source_l3_id"],
                            "candidate_id": candidate["id"],
                            "action_id": action["action_id"],
                            "tool": action["tool"],
                            "meaningful_state_transition": action[
                                "meaningful_state_transition"
                            ],
                            "disposition": "covered_as_capability_variant",
                            "scenario_id": CAPABILITY_SCENARIO_IDS[capability_id],
                        }
                    )
            candidate_records.append(
                {
                    "source_l3_id": report["source_l3_id"],
                    "candidate_id": candidate["id"],
                    "behavior_family": candidate["behavior_family"],
                    "trigger_labels": list(candidate.get("trigger_labels", [])),
                    "objects": list(candidate.get("objects", [])),
                    "source_action_ids": list(candidate["source_action_ids"]),
                    "meaningful_source_action_ids": [
                        action["action_id"]
                        for action in capture["actions"]
                        if action["meaningful_state_transition"]
                    ],
                    "disposition": (
                        "split_and_merged_into_capability_l1s"
                        if len(scenario_ids) > 1
                        else "merged_into_capability_l1"
                    ),
                    "generated_scenario_ids": scenario_ids,
                    "reason": (
                        "按主 object 或完整业务目标归并；时间、房间、初态、参数和阶段仅为变体"
                    ),
                    "captured_at": capture["initial_state"]["captured_at"],
                }
            )
        candidate_outputs_by_l3[report["source_l3_id"]] = candidate_outputs
        for context in report.get("l3_context", []):
            source_id = context.get("source_id")
            if not source_id:
                continue
            scenario_id = CAPABILITY_SCENARIO_IDS["printer_control"]
            context_action_records.append(
                {
                    "source_l3_id": report["source_l3_id"],
                    "action_id": source_id,
                    "disposition": "covered_as_capability_variant",
                    "scenario_id": scenario_id,
                    "reason": "共享打印机收尾仍属于同一打印机控制能力，不另建 L1",
                }
            )
            variants_by_capability["printer_control"].append(
                {
                    "kind": "diagnostic_context",
                    "l3_scenario_id": report["source_l3_id"],
                    "diagnostic_file": diagnostic_file(report),
                    "source_fingerprint": report["source_fingerprint"],
                    "action_ids": [source_id],
                    "evidence_ids": [],
                    "trigger_labels": ["shared_resource_closeout"],
                    "objects": ["shared_printer"],
                    "action_parameters": [],
                }
            )

    for report in reports:
        candidate_outputs = candidate_outputs_by_l3[report["source_l3_id"]]
        for obligation in report["business_obligations"]:
            generated = sorted(
                {
                    scenario_id
                    for candidate_id in obligation.get("candidate_ids", [])
                    for scenario_id in candidate_outputs.get(candidate_id, [])
                }
            )
            kind = obligation["kind"]
            if kind == "room_booking":
                generated = [CAPABILITY_SCENARIO_IDS["meeting_room_booking"]]
            elif kind == "change_response":
                generated = [CAPABILITY_SCENARIO_IDS["meeting_requirement_change"]]
            elif kind == "meeting_recovery":
                generated = [CAPABILITY_SCENARIO_IDS["meeting_room_recovery"]]
            elif kind == "print":
                generated = [CAPABILITY_SCENARIO_IDS["printer_control"]]
            elif kind == "shared_constraint":
                generated = [CAPABILITY_SCENARIO_IDS["power_constrained_adjustment"]]

            if generated:
                disposition = "covered_by_capability_l1"
                reason = "该义务是同一可复用能力的来源变体，不因时间、房间或参数另建 L1"
            elif kind == "occupancy_or_stage":
                disposition = "l3_context_trigger"
                reason = "人数和阶段变化是 L1 的触发或输入，不是独立交付目标"
            elif kind == "research_requirement":
                disposition = "l3_evaluation_objective"
                reason = "全天聚合研究指标保留为 L3 验收，不按采样时刻生成 L1"
            else:
                disposition = "satisfied_by_existing_capability_state"
                reason = "该边界无需新操作，但仍复用同一能力和既有状态，不新增 L1"
            obligation_record = {
                "source_l3_id": report["source_l3_id"],
                "obligation_id": obligation["id"],
                "kind": kind,
                "candidate_ids": list(obligation.get("candidate_ids", [])),
                "disposition": disposition,
                "scenario_ids": generated,
                "reason": reason,
            }
            obligation_records.append(obligation_record)
            for scenario_id in generated:
                capability_id = scenario_id.removeprefix(
                    "scenario_building_kechuang_l1_"
                )
                obligation_refs_by_capability[capability_id].append(
                    {
                        "source_l3_id": report["source_l3_id"],
                        "obligation_id": obligation["id"],
                    }
                )

    source_capabilities = [
        key
        for key in CAPABILITY_DEFINITIONS
        if key not in {"meeting_room_booking", "meeting_requirement_change"}
    ]
    contracts: list[dict[str, Any]] = []
    for capability_id in source_capabilities:
        options = options_by_capability[capability_id]
        if not options:
            raise RuntimeError(f"no meaningful representative for {capability_id}")
        representative = min(
            options,
            key=lambda item: representative_sort_key(capability_id, item),
        )
        contracts.append(
            build_capability_contract(
                capability_id,
                representative,
                variants_by_capability[capability_id],
                obligation_refs_by_capability[capability_id],
            )
        )
    contracts.extend(build_supplemental_contracts(obligation_refs_by_capability))
    contracts.sort(key=lambda item: item["scenario_id"])

    fingerprints = {report["source_fingerprint"] for report in reports}
    if len(fingerprints) != 1:
        raise RuntimeError(f"source fingerprints differ: {sorted(fingerprints)}")
    fingerprint = next(iter(fingerprints))
    if len(reports) != 20 or len(candidate_records) != 252:
        raise RuntimeError(
            f"expected 20 L3/252 candidates, got {len(reports)}/{len(candidate_records)}"
        )
    candidate_counts = Counter(item["disposition"] for item in candidate_records)
    action_counts = Counter(item["disposition"] for item in action_records)
    obligation_counts = Counter(item["disposition"] for item in obligation_records)
    variant_count = sum(
        len(contract["coverage"]["source_variants"]) for contract in contracts
    )
    if len(contracts) != len(CAPABILITY_DEFINITIONS):
        raise RuntimeError(
            f"expected {len(CAPABILITY_DEFINITIONS)} capability L1s, got {len(contracts)}"
        )

    manifest = {
        "schema_version": 2,
        "created_at": REVIEWED_AT,
        "scope": {
            "kind": "cross_l3_capability_catalog",
            "source_l3_total": 20,
            "source_l3_reviewed": list(range(1, 21)),
            "accepted_l1_count": len(contracts),
            "fully_split": True,
            "candidate_disposition_count": len(candidate_records),
            "candidate_disposition_complete": True,
            "source_candidate_action_count": len(action_records),
            "source_context_action_count": len(context_action_records),
            "source_action_coverage_complete": True,
            "source_variant_count": variant_count,
            "note": "12 个能力承接全部来源变体；时间、房间、初态、参数和阶段不再制造新 L1。",
        },
        "policy": {
            "unit": "one_reusable_business_capability",
            "identity_key": ["business_goal", "primary_object_or_object_set"],
            "variant_dimensions": [
                "time",
                "room",
                "initial_state",
                "target_parameters",
                "execution_branch",
            ],
            "fixed_count_limit": None,
            "generator_accepts_only": "accepted_l1",
            "requires_independent_representative_fixture": True,
            "requires_positive_and_negative_controls": True,
            "requires_source_action_coverage": True,
        },
        "review_notes": [
            {
                "decision": "capability_consolidation_complete",
                "reviewed_at": REVIEWED_AT,
                "candidate_total": len(candidate_records),
                "accepted_l1_count": len(contracts),
                "source_fingerprint": fingerprint,
                "review_ledger": "workflow_exports/building_l3_to_l1_formal/review_ledger.json",
                "obligation_ledger": "workflow_exports/building_l3_to_l1_formal/obligation_ledger.json",
            },
            {
                "decision": "supplemental_runtime_evidence",
                "capabilities": [
                    "meeting_room_booking",
                    "meeting_requirement_change",
                ],
                "note": "源 L3 预置或自动应用日程事实；正式能力用真实 Schedule/Room/ResourceAllocation 工具的补充夹具验证。",
            },
        ],
        "contracts": contracts,
    }
    review_ledger = {
        "schema_version": 2,
        "reviewed_at": REVIEWED_AT,
        "source_fingerprint": fingerprint,
        "policy": {
            "identity": "同一业务目标与主 object/对象集合只形成一个 L1",
            "consolidation": "时间、房间、初态、参数、轮次和阶段保留为覆盖变体",
            "action_ownership": "每个源写动作映射到且仅映射到一个能力 L1",
        },
        "summary": {
            "source_l3_count": len(reports),
            "candidate_total": len(candidate_records),
            "candidate_dispositions": dict(sorted(candidate_counts.items())),
            "accepted_l1_count": len(contracts),
            "capability_ids": [item["scenario_id"] for item in contracts],
            "candidate_disposition_ratio": 1.0,
            "candidate_disposition_complete": True,
            "candidate_action_total": len(action_records),
            "candidate_action_dispositions": dict(sorted(action_counts.items())),
            "l3_context_action_total": len(context_action_records),
            "source_action_coverage_ratio": 1.0,
        },
        "candidates": candidate_records,
        "actions": action_records,
        "l3_context_actions": context_action_records,
    }
    obligation_ledger = {
        "schema_version": 2,
        "reviewed_at": REVIEWED_AT,
        "source_fingerprint": fingerprint,
        "summary": {
            "obligation_total": len(obligation_records),
            "dispositions": dict(sorted(obligation_counts.items())),
            "obligation_disposition_ratio": 1.0,
            "unresolved_count": 0,
        },
        "obligations": obligation_records,
    }
    return manifest, review_ledger, obligation_ledger


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument(
        "--apply", action="store_true", help="write manifest and formal audit ledgers"
    )
    args = parser.parse_args(argv)
    source_manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest, review_ledger, obligation_ledger = build_full_review(source_manifest)
    validate_manifest(manifest)
    validate_source_links(manifest, ROOT)
    if args.apply:
        write_json(args.manifest, manifest)
        write_json(REVIEW_LEDGER_PATH, review_ledger)
        write_json(OBLIGATION_LEDGER_PATH, obligation_ledger)
    preview = {
        "mode": "apply" if args.apply else "preview",
        "source_l3_count": review_ledger["summary"]["source_l3_count"],
        "candidate_total": review_ledger["summary"]["candidate_total"],
        "candidate_dispositions": review_ledger["summary"][
            "candidate_dispositions"
        ],
        "accepted_l1_count": review_ledger["summary"]["accepted_l1_count"],
        "candidate_action_total": review_ledger["summary"][
            "candidate_action_total"
        ],
        "l3_context_action_total": review_ledger["summary"][
            "l3_context_action_total"
        ],
        "obligation_total": obligation_ledger["summary"]["obligation_total"],
        "obligation_dispositions": obligation_ledger["summary"]["dispositions"],
    }
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
