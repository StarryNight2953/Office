"""Audit Building business-complete L1 proposals and probe multiple behaviors.

This is a diagnostic prototype, not a registered-scenario generator. No count
quota or automatic semantic deduplication is applied. See the Chinese standard
in smartoffice/scenarios/building_kechuang/l3-to-l1-split-standard.zh.md.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from smartoffice.agents.agent.toolset_builder import build_toolset
from smartoffice.apps.building_world import PrintingApp
from smartoffice.controllers.engine import Engine
from smartoffice.scenarios.building_kechuang.l3.scenarios import L3_SCENARIO_CLASSES

DEFAULT_OUTPUT = PROJECT_ROOT / "workflow_exports/building_l3_to_l1_diagnostic"
ROOM_RE = re.compile(r"\bk(?:1324|1315|1316)(?=_|\b)")
GROUP_RE = re.compile(r"^reconcile_(\d+)_")
ENV_METHODS = {"set_hvac", "set_ventilation", "set_humidifier", "set_air_purifier"}
EQUIPMENT_METHODS = {"set_lighting", "set_projector", "set_audio_system"}
GOALS = {
    "environment_control": "使本次负载/环境变化下的目标区域达到声明的环境条件",
    "lighting_service": "使本次照明需求对应的灯组、亮度、色温和场景配置生效并核验",
    "meeting_equipment": "完成当前活动/阶段的演示设备配置并核验",
    "room_recovery": "将已结束活动的房间恢复为允许的空闲/周转状态",
    "power_constrained_adjustment": "在本次负载变化下兼顾共享功率上限与服务目标",
    "printing_fulfillment": "按时完成本轮公开材料请求并逐项确认",
}


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    for relative in (
        "smartoffice/scenarios/building_kechuang/l3/catalog.py",
        "smartoffice/scenarios/building_kechuang/l3/specs.py",
        "smartoffice/scenarios/building_kechuang/l3/base.py",
        "smartoffice/apps/building_world/printing_app.py",
        "smartoffice/apps/building_world/hvac_app.py",
        "smartoffice/apps/building_world/lighting_app.py",
        "smartoffice/apps/building_world/schedule_app.py",
        "smartoffice/apps/building_world/operations_app.py",
        "smartoffice/apps/building_world/runtime.py",
    ):
        digest.update(relative.encode())
        digest.update((PROJECT_ROOT / relative).read_bytes())
    return digest.hexdigest()


def workflow_steps(workflow) -> list[dict[str, Any]]:
    """Validate the reference order and retain IDs, arguments and tool types."""
    records = []
    seen: set[str] = set()
    elapsed = 0.0
    for step_id, step in workflow.dag.items():
        dependencies = [dep for dep in step.depends_on if dep is not None]
        if any(dep not in seen for dep in dependencies):
            raise ValueError(f"non-topological source workflow at {step_id}")
        tool = step.tool_name or ""
        app, _, method = tool.partition("__")
        args = dict(step.tool_args or {})
        records.append({
            "id": step_id, "position": len(records), "tool": tool,
            "app": app, "method": method, "args": args,
            "op_type": str(step.op_type), "minute": elapsed,
        })
        if tool == "SystemApp__advance_time":
            elapsed += (args.get("seconds", 0) / 60 + args.get("minutes", 0)
                        + args.get("hours", 0) * 60 + args.get("days", 0) * 1440)
        seen.add(step_id)
    return records


def is_action(step: dict) -> bool:
    return step["op_type"] == "WRITE" and step["app"] not in {
        "SystemApp", "AgentUserInterface", "",
    }


def rooms_of(steps: list[dict]) -> list[str]:
    return sorted({match for step in steps for value in step["args"].values()
                   if isinstance(value, str) for match in ROOM_RE.findall(value)})


def print_clusters(spec) -> list[list[int]]:
    """Conservative grouping of same-release-slot jobs on the shared printer.

    Jobs from different slots still require the captured existing queue. The
    probe measures that queue instead of assuming that it is empty.
    """
    groups: dict[int, list[int]] = defaultdict(list)
    for index, batch in enumerate(spec.print_batches, 1):
        groups[batch.submit_minute].append(index)
    return [groups[minute] for minute in sorted(groups)]


def analyze_source(scenario_class) -> tuple[dict, list[dict]]:
    source = scenario_class()
    oracle = Engine(None, source).build_oracle_workflow(run_oracle=False)
    steps = workflow_steps(oracle)
    spec = source.spec
    by_id = {step["id"]: step for step in steps}
    groups: list[dict] = []
    checkpoints = source._workflow_checkpoints()
    current = None
    for step in steps:
        match = GROUP_RE.match(step["id"])
        if match:
            index = int(match[1])
            checkpoint = checkpoints[index - 1]
            current = {"index": index, "minute": checkpoint.minute,
                       "labels": list(checkpoint.labels), "steps": []}
            groups.append(current)
        if current is not None:
            current["steps"].append(step)

    proposals: list[dict] = []
    ownership: dict[str, str] = {}
    context = []
    issues = []

    def add(kind: str, key: str, actions: list[dict], evidence: list[dict],
            minute: float, end: float, trigger: list[str], objects: list[str],
            review: list[str], **extra) -> dict:
        candidate_id = f"L1-{spec.number:02d}-{key}"
        ordered = sorted(actions, key=lambda step: step["position"])
        for action in ordered:
            if action["id"] in ownership:
                raise ValueError(f"double action ownership: {action['id']}")
            ownership[action["id"]] = candidate_id
        item = {
            "id": candidate_id, "behavior_family": kind,
            "status": "needs_review" if review else "proposed",
            "goal": GOALS[kind], "objects": objects,
            "start_minute": minute, "source_evidence_end_minute": end,
            "trigger_labels": trigger,
            "source_action_ids": [step["id"] for step in ordered],
            "source_evidence_ids": [step["id"] for step in evidence],
            "source_actions": ordered,
            "review_reasons": review,
            "background_dependencies": [
                "保留源时刻的物理、传感器、房间设备、预约与未来事件",
                "保留其它任务的资源占用；不可默认背景负载为零",
            ],
            "runtime_validated": False,
            **extra,
        }
        proposals.append(item)
        return item

    for cluster_index, indices in enumerate(print_clusters(spec), 1):
        actions, evidence, missing = [], [], []
        for index in indices:
            submit = f"submit_print_batch_{index:02d}"
            verify = f"verify_print_deadline_{index:02d}"
            power = f"power_printer_for_batch_{index:02d}"
            for name in (submit, verify):
                if name not in by_id:
                    missing.append(f"missing_source_step:{name}")
            for name, expected in ((submit, "PrintingApp__submit_print_job"),
                                   (verify, "PrintingApp__get_print_job")):
                if name in by_id and by_id[name]["tool"] != expected:
                    missing.append(f"source_tool_mismatch:{name}")
            if submit in by_id and verify in by_id:
                if by_id[submit]["position"] >= by_id[verify]["position"]:
                    missing.append(f"source_order_mismatch:{submit}")
            if submit in by_id:
                actions.append(by_id[submit])
            if power in by_id:
                actions.append(by_id[power])
            if verify in by_id:
                evidence.append(by_id[verify])
        batches = [spec.print_batches[index - 1] for index in indices]
        start = min(batch.submit_minute for batch in batches)
        end = max(batch.ready_by_minute for batch in batches)
        evidence.extend(step for step in steps if start <= step["minute"] <= end
                        and (step["method"] in {"get_print_requests", "get_commitment_status"}
                             or step["app"] == "SystemApp"))
        add("printing_fulfillment", f"print-{cluster_index:02d}", actions, evidence,
            start, end, ["public_print_request"],
            [f"print-request-{spec.number:02d}-{index:02d}" for index in indices],
            missing,
            batch_indices=indices,
            requests=[asdict(batch) for batch in batches],
            trigger_visible_since_minutes=[batch.reveal_minute for batch in batches],
            success_predicates=["correct_linked_jobs", "all_completed", "on_time",
                                "no_duplicate_jobs", "completion_observed"],
            additional_evidence_required=["当前设备/已有队列", "用提交返回值获得 job_id"],
            grouping_reason="同轮提交共享打印机，联合核验；其它已排队作业作为背景保留")

    for group in groups:
        group_steps = group["steps"]
        index, minute = group["index"], group["minute"]
        evidence = [step for step in group_steps if not is_action(step)
                    and step["app"] != "AgentUserInterface"
                    and not step["id"].startswith("advance_to_checkpoint")]
        # All response/stability feedback belongs with the control decision.
        room_actions = [step for step in group_steps if is_action(step)
                        and step["method"] in ENV_METHODS | EQUIPMENT_METHODS]
        if not room_actions:
            context.append({"kind": "monitor_or_external_event", "minute": minute,
                            "labels": group["labels"],
                            "reason": "无新的房间控制写动作；需结合既有状态判断是否已满足义务"})
            continue
        end = max(step["minute"] for step in group_steps
                  if not step["id"].startswith("advance_to_checkpoint"))
        if spec.power_limit_w is not None:
            add("power_constrained_adjustment", f"power-g{index:02d}",
                room_actions, evidence, minute, end, group["labels"],
                rooms_of(room_actions), [
                    "联合源片段须确认是一次局部预算目标，不能吞并全天承诺",
                    "需独立验证局部服务目标、总功率与背景负载",
                ], power_limit_w=spec.power_limit_w)
            continue
        buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for step in room_actions:
            rooms = rooms_of([step])
            if len(rooms) != 1:
                issues.append(f"cannot_identify_room:{step['id']}")
                continue
            kind = ("room_recovery" if "_shutdown_" in step["id"]
                    else "environment_control" if step["method"] in ENV_METHODS
                    else "lighting_service" if step["method"] == "set_lighting"
                    else "meeting_equipment")
            buckets[(kind, rooms[0])].append(step)
        for (kind, room), actions in buckets.items():
            review = {
                "environment_control": ["校准局部环境阈值、稳定观察窗并做独立对照",
                                        "检查相邻片段是否仍属同一未完成反馈闭环"],
                "meeting_equipment": ["需补充设备模式/参数的动作后核验，传感器复测不能代替"],
                "lighting_service": ["需补充灯组/亮度/色温/场景核验，并复核与演示任务是否必须合并"],
                "room_recovery": ["需核验后续承诺及允许空闲配置；不能默认所有设备均应关机"],
            }[kind]
            add(kind, f"{kind}-g{index:02d}-{room}", actions, evidence,
                minute, end, group["labels"], [room], review,
                boundary_alternatives=(
                    ["若目标仅是启动空调，可改用 hvac_activation 合同；必须重新分配其它动作，不能与完整环境任务重复计数"]
                    if kind == "environment_control" else []))

    for step in steps:
        if step["id"] == "shutdown_shared_printer" and is_action(step):
            ownership[step["id"]] = "l3_context:shared_printer_closeout"
            context.append({"kind": "shared_printer_closeout", "source_id": step["id"],
                            "reason": "所有打印承诺完成后的公共收尾，不拆成单工具 L1"})
    all_actions = [step["id"] for step in steps if is_action(step)]
    unmapped = [step for step in all_actions if step not in ownership]
    issues.extend(f"unmapped_action:{step}" for step in unmapped)
    for item in proposals:
        if not item["source_action_ids"]:
            issues.append(f"candidate_without_action:{item['id']}")
        for missing in item["review_reasons"]:
            if missing.startswith(("missing_source_step:", "source_tool_mismatch:",
                                   "source_order_mismatch:")):
                issues.append(missing)

    obligations = business_obligations(spec, proposals)
    result = {
        "source_l3_id": spec.scenario_id, "number": spec.number, "slug": spec.slug,
        "title": spec.title, "source_fingerprint": source_fingerprint(),
        "standard_version": "1.0", "analysis_mode": "static_proposals_not_scenarios",
        "oracle_steps": len(steps), "candidates": proposals,
        "counts_by_family": dict(Counter(item["behavior_family"] for item in proposals)),
        "counts_by_status": dict(Counter(item["status"] for item in proposals)),
        "business_obligations": obligations, "l3_context": context,
        "action_ownership": ownership, "unmapped_action_ids": unmapped,
        "static_errors": issues, "static_integrity_passed": not issues,
        "action_assignment_ratio": len(ownership) / len(all_actions) if all_actions else 1.0,
        "fully_split_and_validated": False,
        "obligations_needing_review": sum(item["status"] == "needs_review" for item in obligations),
    }
    return result, steps


def business_obligations(spec, proposals: list[dict]) -> list[dict]:
    """Independent Spec inventory so action coverage cannot hide missing intent."""
    items = []

    def record(key, kind, minute, room=None, deadline=None, **extra):
        linked = [item["id"] for item in proposals
                  if (room is None or room in item["objects"])
                  and item["start_minute"] <= (deadline if deadline is not None else minute)
                  and item["source_evidence_end_minute"] >= minute]
        items.append({"id": key, "kind": kind, "minute": minute, "room": room,
                      "candidate_ids": linked, "status": "needs_review",
                      "reason": ("时间/对象关联仅为线索，需验证状态和具体目标覆盖" if linked
                                 else "源动作无直接关联；核对既有状态、合法不操作或源实现缺口"),
                      **extra})

    for index, batch in enumerate(spec.print_batches, 1):
        owner = next((item for item in proposals if index in item.get("batch_indices", [])), None)
        items.append({"id": f"print-request-{spec.number:02d}-{index:02d}", "kind": "print",
                      "minute": batch.submit_minute, "deadline": batch.ready_by_minute,
                      "candidate_ids": [owner["id"]] if owner else [],
                      "status": "proposed" if owner else "needs_review",
                      "reason": "需求已进入完整打印合同；独立试验结果另列" if owner else "未追踪需求"})
    for phase in spec.phases:
        for room, count in phase.counts:
            record(f"phase-{phase.minute}-{room}", "occupancy_or_stage", phase.minute,
                   room, episode=phase.episode, occupancy=count)
    for meeting in spec.meetings:
        items.append({"id": f"booking-{meeting.activity_id}", "kind": "room_booking",
                      "minute": 0, "candidate_ids": [], "status": "l3_context",
                      "reason": "当前 L3 在初始化时预置会议与预约，Oracle 无 create_meeting；不能冒充已拆出的预约行为"})
        record(f"prepare-{meeting.activity_id}", "meeting_preparation",
               meeting.start_minute - spec.preparation_lead_minutes, meeting.room_id,
               meeting.start_minute, capabilities=list(meeting.capabilities))
        record(f"recover-{meeting.activity_id}", "meeting_recovery", meeting.end_minute,
               meeting.room_id)
    if spec.interaction:
        record(f"interaction-{spec.interaction.subject_id}", "change_response",
               spec.interaction.minute, environment_responsibility=(
                   "当前处理器自动更新会议/预约；Agent 责任限于可证实的资源响应，不能冒充主动选房"))
    if spec.power_limit_w is not None:
        record("shared-power", "shared_constraint", 0, deadline=spec.duration_minutes,
               power_limit_w=spec.power_limit_w)
    for metric in spec.primary_metrics:
        record(f"research-{metric}", "research_requirement", 0,
               deadline=spec.duration_minutes, metric=metric,
               metric_warning="指标名称尚不是本次 L1 的数值验收器")
    return items


def fresh_engine(scenario_class):
    scenario = scenario_class()
    scenario.setup()
    engine = Engine(None, scenario)
    _, _, tool_map = build_toolset(list(scenario.apps or []))
    return engine, tool_map


def invoke(tool_map, name: str, args: dict) -> Any:
    value = tool_map[name](**args)
    if isinstance(value, dict) and (value.get("error") or value.get("status") in {
        "failed", "rejected", "error",
    }):
        raise RuntimeError(f"{name}: {value}")
    return value


def print_verdict(requests: list[dict], jobs: list[dict], observed: set[str]) -> dict:
    """Business-state assertions; no Oracle node names participate in scoring."""
    details = []
    for request in requests:
        matching = [job for job in jobs if job.get("request_id") == request["request_id"]]
        job = matching[0] if len(matching) == 1 else None
        same_document = [item for item in jobs if item.get("document_name") == request["document_name"]]
        valid = bool(job) and all(job.get(key) == request[key] for key in (
            "document_name", "copies", "pages_per_copy", "priority", "requested_by",
        ))
        details.append({
            "request_id": request["request_id"],
            "one_correct_job": bool(valid and request.get("job_id") == job["job_id"]),
            "no_duplicate_document": len(same_document) == 1,
            "not_before_submit_window": bool(job and datetime.fromisoformat(job["submitted_at"])
                                              >= datetime.fromisoformat(request["submit_at"])),
            "completed": bool(job and job["status"] == "completed"
                              and request["status"] == "completed"),
            "on_time": bool(job and float(job["ready_at_timestamp"])
                            <= datetime.fromisoformat(request["ready_by"]).timestamp()),
            "completion_observed": bool(job and job["job_id"] in observed),
        })
    keys = ("one_correct_job", "no_duplicate_document", "not_before_submit_window",
            "completed", "on_time", "completion_observed")
    return {"success": bool(details) and all(all(row[key] for key in keys) for row in details),
            "requests": details}


def probe_printing(scenario_class, report: dict, steps: list[dict]) -> list[dict]:
    """Fork each printing round from the same full in-memory source state."""
    results = []
    for candidate in report["candidates"]:
        if candidate["behavior_family"] != "printing_fulfillment":
            continue
        engine, tools = fresh_engine(scenario_class)
        scenario = engine.scenario
        printing = scenario.get_typed_app(PrintingApp)
        boundary = min(step["position"] for step in candidate["source_actions"])
        prefix_calls = 0
        for step in steps[:boundary]:
            if step["tool"]:
                invoke(tools, step["tool"], step["args"])
                prefix_calls += 1
        snapshot = copy.deepcopy(scenario.building_runtime.snapshot())
        printing_snapshot = copy.deepcopy((printing.requests, printing.jobs))
        source_clock = engine.time_manager.time()
        initial_requests = [copy.deepcopy(printing.requests[key]) for key in candidate["objects"]]
        if any(item["status"] != "pending" for item in initial_requests):
            raise ValueError("print boundary must precede the target submission")
        outcomes = {}
        for mode in ("correct", "do_nothing", "wrong_quantity", "submit_only", "no_confirmation"):
            scenario.building_runtime.restore(copy.deepcopy(snapshot))
            printing.requests, printing.jobs = copy.deepcopy(printing_snapshot)
            engine.time_manager.reset(start_time=source_clock)
            trace, observed = [], set()

            def call(name, **kwargs):
                result = invoke(tools, name, kwargs)
                trace.append({"tool": name, "args": kwargs, "result": copy.deepcopy(result)})
                return result

            public = call("PrintingApp__get_print_requests", status="all")
            visible = {item["request_id"]: item for item in public["requests"]}
            wanted = [visible[key] for key in candidate["objects"]]
            overview = call("BuildingWorldApp__get_building_overview")
            call("BuildingOperationsApp__get_commitment_status")
            # All background jobs are kept and queried. The source queue is not
            # cleared, including completed work from an earlier printing round.
            background_ids = {item["job_id"] for item in public["requests"] if item["job_id"]}
            for job_id in sorted(background_ids):
                call("PrintingApp__get_print_job", job_id=job_id)
            device = next(item for item in overview["devices"]
                          if item["device_id"] == "k1315_printer_01")
            if device["health"] != "online":
                raise ValueError("printing prototype requires an online source printer")
            job_ids = []
            ready_at = source_clock
            if mode != "do_nothing":
                if not device["power_on"]:
                    call("PrintingApp__set_printer_power", device_id=device["device_id"], power_on=True)
                for request in sorted(wanted, key=lambda item: (-item["priority"], item["ready_by"])):
                    before = engine.time_manager.time()
                    result = call("PrintingApp__submit_print_job",
                                  printer_id=device["device_id"], document_name=request["document_name"],
                                  copies=request["copies"] + (1 if mode == "wrong_quantity" else 0),
                                  pages_per_copy=request["pages_per_copy"],
                                  requested_by=request["requested_by"], priority=request["priority"])
                    job_ids.append(result["job_id"])
                    # Estimate comes from the tool response, not hidden job state.
                    ready_at = max(ready_at, before + result["queued_seconds"]
                                   + result["estimated_duration_seconds"] + 1)
            if mode == "do_nothing":
                # Use the same business completion horizon as the correct arm.
                ready_at = outcomes["correct"]["observation_timestamp"]
            if mode != "submit_only":
                seconds = max(1, math.ceil(ready_at - engine.time_manager.time()))
                call("SystemApp__advance_time", seconds=seconds, stop_on_event=False)
            if mode not in {"submit_only", "no_confirmation"}:
                for job_id in job_ids:
                    job = call("PrintingApp__get_print_job", job_id=job_id)
                    if job.get("status") == "completed":
                        observed.add(job_id)
            verdict = print_verdict([printing.requests[key] for key in candidate["objects"]],
                                    list(printing.jobs.values()), observed)
            outcomes[mode] = {**verdict, "trace": trace,
                              "observation_timestamp": engine.time_manager.time(),
                              "target_jobs": [copy.deepcopy(printing.jobs[key]) for key in job_ids]}
        passed = outcomes["correct"]["success"] and all(
            not value["success"] for key, value in outcomes.items() if key != "correct")
        results.append({
            "candidate_id": candidate["id"], "source_l3_id": report["source_l3_id"],
            "source_fingerprint": report["source_fingerprint"],
            "source_prefix_calls": prefix_calls, "capture_before": steps[boundary]["id"],
            "capture_sim_time": snapshot["current_time"].isoformat(),
            "initial_requests": initial_requests,
            "background_job_ids": sorted(printing_snapshot[1]),
            "prototype_contrast_passed": passed, "outcomes": outcomes,
            "limitations": ["内存状态分支，尚未验证磁盘存档或独立 Scenario 注册",
                            "确定性 advance_time(stop_on_event=False)，非真实 Agent 中断/调度验证",
                            "只验证打印目标，不宣称背景活动的全天指标均满足"],
        })
    return results


def markdown_report(report: dict) -> str:
    lines = [f"# L3-{report['number']:02d} {report['title']}", "",
             "本文件是候选诊断，不是生成场景列表或最终验收结论。", "",
             f"- 源动作归属率：{report['action_assignment_ratio']:.1%}",
             f"- 结构检查通过：{report['static_integrity_passed']}",
             f"- 义务待审核：{report['obligations_needing_review']}",
             f"- 完成充分拆分与验收：{report['fully_split_and_validated']}", "",
             "| 候选 | 类型 | 起始–源核验分钟 | 对象 | 源动作数 | 状态 | 待审核 |",
             "|---|---|---:|---|---:|---|---|"]
    for item in report["candidates"]:
        lines.append(f"| {item['id']} | {item['behavior_family']} | "
                     f"{item['start_minute']:g}–{item['source_evidence_end_minute']:g} | "
                     f"{', '.join(item['objects'])} | {len(item['source_action_ids'])} | "
                     f"{item['status']} | {'；'.join(item['review_reasons']) or '需独立运行确认'} |")
    lines += ["", "## 业务义务清单", "", "| 义务 | 类型 | 状态 | 关联候选数 | 说明 |",
              "|---|---|---|---:|---|"]
    for item in report["business_obligations"]:
        lines.append(f"| {item['id']} | {item['kind']} | {item['status']} | "
                     f"{len(item['candidate_ids'])} | {item['reason']} |")
    lines += ["", "精确工具 ID、参数、共享依赖和公共步骤见同名 JSON。", ""]
    return "\n".join(lines)


def summary_markdown(reports: list[dict], probes: list[dict], behavior_probes: list[dict]) -> str:
    lines = ["# Building L3→L1 拆分诊断", "",
             "L1 按完整业务闭环提案，无数量上限；尚未生成注册场景。", "",
             "| L3 | 候选 | proposed | needs_review | 动作归属率 | 结构检查 |",
             "|---|---:|---:|---:|---:|---|"]
    for item in reports:
        name = f"l3_{item['number']:02d}_{item['slug']}.md"
        counts = item["counts_by_status"]
        lines.append(f"| [L3-{item['number']:02d}]({name}) | {len(item['candidates'])} | "
                     f"{counts.get('proposed', 0)} | {counts.get('needs_review', 0)} | "
                     f"{item['action_assignment_ratio']:.1%} | {item['static_integrity_passed']} |")
    lines += ["", f"打印原型对照：{sum(item['prototype_contrast_passed'] for item in probes)}/{len(probes)} 通过。",
              f"其它业务原型对照：{sum(item['prototype_contrast_passed'] for item in behavior_probes)}/{len(behavior_probes)} 通过。",
              "未启用 --probe-printing 或所选场景无打印时，0/0 表示未执行。",
              "源动作归属 100% 不等于业务目标达成；每份报告中的待审核项仍须处理。",
              "业务原型的局部合同与限制见 behavior_probes.json；预约为已有 Building 夹具的补充试验，未伪称从 20 个 L3 提取。",
              "独立存档/注册、完整跨任务约束和真实 Agent 运行仍待验收。", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", nargs="+", type=int, default=list(range(1, 21)))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--probe-printing", action="store_true")
    parser.add_argument("--probe-behaviors", action="store_true",
                        help="probe available L3-06/08/16 control families and supplemental room booking")
    parser.add_argument("--force", action="store_true", help="overwrite this diagnostic's named report files")
    args = parser.parse_args(argv)
    if any(number not in range(1, 21) for number in args.scenarios):
        parser.error("--scenarios must contain numbers in 1..20")
    numbers = sorted(set(args.scenarios))
    output = args.output_dir.resolve()
    names = ["README.md", "summary.json", "printing_probes.json", "behavior_probes.json"]
    for number in numbers:
        spec = L3_SCENARIO_CLASSES[number - 1].spec
        names += [f"l3_{number:02d}_{spec.slug}{suffix}" for suffix in (".json", ".md")]
    collisions = [name for name in names if (output / name).exists()]
    if collisions and not args.force:
        parser.error(f"reports already exist at {output}; choose another --output-dir or use --force")
    reports, probes = [], []
    analyzed = {}
    for number in numbers:
        scenario_class = L3_SCENARIO_CLASSES[number - 1]
        report, steps = analyze_source(scenario_class)
        analyzed[number] = report, steps
        reports.append(report)
        if args.probe_printing:
            probes.extend(probe_printing(scenario_class, report, steps))
        print(f"L3-{number:02d}: proposed_L1={len(report['candidates'])} "
              f"action_coverage={report['action_assignment_ratio']:.0%} "
              f"static_ok={report['static_integrity_passed']} "
              f"needs_review={report['counts_by_status'].get('needs_review', 0)}")
    behavior_probes = []
    if args.probe_behaviors:
        from scripts.building_l1_behavior_probes import probe_additional_behaviors
        behavior_probes = probe_additional_behaviors(analyzed)
    output.mkdir(parents=True, exist_ok=True)
    def write_json(name, payload):
        (output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for report in reports:
        stem = f"l3_{report['number']:02d}_{report['slug']}"
        write_json(stem + ".json", report)
        (output / (stem + ".md")).write_text(markdown_report(report), encoding="utf-8")
    write_json("summary.json", [{key: value for key, value in report.items() if key not in {
        "candidates", "business_obligations", "l3_context", "action_ownership",
    }} | {"candidate_count": len(report["candidates"])} for report in reports])
    write_json("printing_probes.json", probes)
    write_json("behavior_probes.json", behavior_probes)
    (output / "README.md").write_text(summary_markdown(reports, probes, behavior_probes), encoding="utf-8")
    print(f"Reports: {output}")
    print(f"Printing contrasts: {sum(item['prototype_contrast_passed'] for item in probes)}/{len(probes)}")
    print(f"Other behavior contrasts: {sum(item['prototype_contrast_passed'] for item in behavior_probes)}/{len(behavior_probes)}")
    return 0 if all(item["static_integrity_passed"] for item in reports) and all(
        item["prototype_contrast_passed"] for item in probes + behavior_probes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
