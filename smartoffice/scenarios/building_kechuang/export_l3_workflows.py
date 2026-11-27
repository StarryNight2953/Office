"""Export all Kechuang L3 reference workflows for human and machine review."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from smartoffice.controllers.engine import Engine
from smartoffice.scenarios.building_kechuang.l3.scenarios import L3_SCENARIO_CLASSES

DEFAULT_OUTPUT_DIR = Path("workflow_exports/building_kechuang_l3_review")
REVIEW_START = "<!-- REVIEW_NOTES_START -->"
REVIEW_END = "<!-- REVIEW_NOTES_END -->"


def export_l3_workflows(output_dir: Path = DEFAULT_OUTPUT_DIR) -> Path:
    """Build, replay and export all twenty reference workflows."""

    output_dir.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []

    for scenario_class in L3_SCENARIO_CLASSES:
        spec = scenario_class.spec
        build_engine = Engine(None, scenario_class())
        oracle = build_engine.build_oracle_workflow(run_oracle=False)
        replay_engine = Engine(None, scenario_class())
        replayed = replay_engine.replay_workflow(oracle)
        report = replay_engine.evaluation_report(replayed)

        basename = f"l3_{spec.number:02d}_{spec.slug}"
        markdown_path = output_dir / f"{basename}.md"
        json_path = output_dir / f"{basename}.json"
        payload = {
            "scenario_id": spec.scenario_id,
            "spec": _jsonable(spec),
            "oracle_workflow": oracle.to_dict(),
            "evaluation_report": report,
        }
        json_path.write_text(
            json.dumps(_jsonable(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        review_block = _preserved_review_block(markdown_path)
        markdown_path.write_text(
            _scenario_markdown(
                spec,
                oracle,
                report,
                json_path.name,
                review_block,
            ),
            encoding="utf-8",
        )
        summaries.append(
            {
                "number": spec.number,
                "scenario_id": spec.scenario_id,
                "title": spec.title,
                "family": spec.family,
                "rooms": list(spec.rooms),
                "steps": len(oracle.dag),
                "success": report["validation"]["success"],
                "comfort_violation_ratio": report["validation"]["metadata"][
                    "occupied_comfort_violation_ratio"
                ],
                "markdown": markdown_path.name,
                "json": json_path.name,
            }
        )

    (output_dir / "summary.json").write_text(
        json.dumps(_jsonable(summaries), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    index_path = output_dir / "README.md"
    index_path.write_text(_index_markdown(summaries), encoding="utf-8")
    return index_path


def _scenario_markdown(
    spec,
    workflow,
    report: dict[str, Any],
    json_name: str,
    review_block: str,
) -> str:
    metadata = report["validation"]["metadata"]
    tool_counts = Counter(
        step.tool_name or "no_tool" for step in workflow.dag.values()
    )
    lines = [
        f"# L3-{spec.number:02d} {spec.title}",
        "",
        f"- Scenario ID: `{spec.scenario_id}`",
        f"- 场景族: `{spec.family}`",
        f"- 研究问题: {spec.research_question}",
        f"- 房间: {', '.join(spec.rooms)}",
        f"- 日期与周期: {spec.start_at.isoformat()}，{spec.duration_minutes} 分钟",
        f"- Oracle 节点: {len(workflow.dag)}",
        f"- 完整机器数据: [{json_name}]({json_name})",
        "",
        review_block,
        "",
        "## 场景输入与控制目标",
        "",
        "| 项目 | 值 |",
        "|---|---|",
        f"| HVAC | `{spec.hvac_mode}`，目标 {spec.target_temperature_c:.1f} °C |",
        (
            "| 初始室内 | "
            f"{spec.initial_indoor_temperature_c:.1f} °C，"
            f"{spec.initial_indoor_humidity_pct:.1f}% RH |"
        ),
        f"| 会前准备 | {spec.preparation_lead_minutes} 分钟 |",
        f"| 加湿/净化/演示 | {spec.use_humidifier} / {spec.use_purifier} / {spec.use_presentation} |",
        f"| 功率上限 | {_display(spec.power_limit_w, 'W')} |",
        f"| Primary Metrics | {', '.join(spec.primary_metrics)} |",
        (
            "| 环境上限 | "
            f"CO₂ {spec.evaluation.max_peak_co2_ppm:.0f} ppm；"
            f"PM2.5 {spec.evaluation.max_peak_pm25_ug_m3:.1f} µg/m³；"
            "有人舒适违例比例 "
            f"{spec.evaluation.max_occupied_comfort_violation_ratio:.2f} |"
        ),
        f"| 无人能耗预算 | {_display(spec.evaluation.max_unoccupied_energy_kwh, 'kWh')} |",
        "",
        "### 动态室外边界",
        "",
        "| 分钟 | 温度 °C | RH % | PM2.5 µg/m³ | 太阳辐照 W/m² |",
        "|---:|---:|---:|---:|---:|",
    ]
    lines.extend(
        f"| {point.minute} | {point.temperature_c:.1f} | {point.humidity_pct:.1f} | "
        f"{point.pm25_ug_m3:.1f} | {point.solar_w_m2:.1f} |"
        for point in spec.outdoor.points
    )
    lines.extend(
        [
            "",
            "## 业务时间线",
            "",
            "### 占用阶段",
            "",
            "| 分钟 | Episode | 各房间人数 |",
            "|---:|---|---|",
        ]
    )
    lines.extend(
        f"| {phase.minute} | `{phase.episode}` | "
        f"{', '.join(f'{room}={count}' for room, count in phase.counts)} |"
        for phase in spec.phases
    )
    lines.extend(["", "### 会议", ""])
    if spec.meetings:
        lines.extend(
            [
                "| Meeting ID | 房间 | 开始–结束 | 人数 | 能力 | 标题 |",
                "|---|---|---:|---:|---|---|",
            ]
        )
        lines.extend(
            f"| `{meeting.activity_id}` | {meeting.room_id} | "
            f"{meeting.start_minute}–{meeting.end_minute} | {meeting.attendees} | "
            f"{', '.join(meeting.capabilities)} | {meeting.title} |"
            for meeting in spec.meetings
        )
    else:
        lines.append("无预约会议。")
    lines.extend(["", "### 运行中互动", ""])
    if spec.interaction is None:
        lines.append("无计划修改事件。")
    else:
        lines.extend(
            [
                f"- 分钟: {spec.interaction.minute}",
                f"- 类型: `{spec.interaction.event_type.value}`",
                f"- 对象: `{spec.interaction.subject_id}`",
                f"- 描述: {spec.interaction.description}",
                f"- Payload: `{_compact(dict(spec.interaction.payload), 500)}`",
            ]
        )
    lines.extend(["", "### 打印批次", ""])
    if spec.print_batches:
        lines.extend(
            [
                "| 文档 | 份数 × 每份页数 | 提交 | Deadline | 优先级 |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        lines.extend(
            f"| `{batch.document_name}` | {batch.copies} × {batch.pages_per_copy} | "
            f"{batch.submit_minute} | {batch.ready_by_minute} | {batch.priority} |"
            for batch in spec.print_batches
        )
    else:
        lines.append("无打印任务。")

    lines.extend(
        [
            "",
            "## Oracle 回放结果",
            "",
            "| 指标 | 结果 |",
            "|---|---:|",
            f"| 最终验证 | `{report['validation']['success']}` |",
            f"| Physics steps | {metadata['physics_steps']} |",
            f"| 有人舒适违例比例 | {metadata['occupied_comfort_violation_ratio']:.4f} |",
            f"| 有人峰值 CO₂ | {metadata['peak_occupied_co2_ppm']:.1f} ppm |",
            f"| 有人峰值 PM2.5 | {metadata['peak_occupied_pm25_ug_m3']:.1f} µg/m³ |",
            f"| 峰值功率 | {metadata['peak_power_w']:.1f} W |",
            f"| 无人能耗 | {metadata['unoccupied_energy_kwh']:.4f} kWh |",
            f"| 会议均已准备 | `{metadata['meetings_prepared']}` |",
            f"| 打印均按时 | `{metadata['print_deadlines_met']}` |",
            "",
            "### 工具调用统计",
            "",
            "| Tool | 次数 |",
            "|---|---:|",
        ]
    )
    lines.extend(
        f"| `{tool}` | {count} |" for tool, count in sorted(tool_counts.items())
    )
    lines.extend(
        [
            "",
            "## 逐步工作流",
            "",
            "下表是 Oracle DAG 的拓扑顺序。完整参数和回放结果请查看同名 JSON。",
            "",
            "| # | 模拟时间 | 步骤 ID | 类别 | Tool | 参数摘要 | 依赖 |",
            "|---:|---|---|---|---|---|---|",
        ]
    )
    for index, (step_id, step) in enumerate(workflow.dag.items(), start=1):
        lines.append(
            f"| {index} | {_escape(step.time or '')} | `{_escape(step_id)}` | "
            f"{_step_category(step_id, step.tool_name)} | "
            f"`{_escape(step.tool_name or '')}` | "
            f"`{_escape(_compact(step.tool_args or {}, 180))}` | "
            f"{_escape(', '.join(step.depends_on))} |"
        )
    lines.append("")
    return "\n".join(lines)


def _index_markdown(summaries: list[dict[str, Any]]) -> str:
    steps = [int(item["steps"]) for item in summaries]
    lines = [
        "# Kechuang Building L3 工作流审核包",
        "",
        "本目录由 `python -m smartoffice.scenarios.building_kechuang.export_l3_workflows` 生成。",
        "请从下表进入每个 Markdown，完成场景合理性检查；JSON 用于程序分析和版本比较。",
        "",
        f"共 {len(summaries)} 个场景，DAG 节点范围 {min(steps)}–{max(steps)}。",
        "",
        "| 编号 | 场景 | 场景族 | 房间 | 节点 | 回放 | 舒适违例比例 | 数据 |",
        "|---:|---|---|---|---:|---|---:|---|",
    ]
    for item in summaries:
        lines.append(
            f"| {item['number']:02d} | [{item['title']}]({item['markdown']}) | "
            f"{item['family']} | {', '.join(item['rooms'])} | {item['steps']} | "
            f"{item['success']} | {item['comfort_violation_ratio']:.4f} | "
            f"[JSON]({item['json']}) |"
        )
    lines.extend(
        [
            "",
            "## 建议审核顺序",
            "",
            "1. 先检查业务事实：房间、容量、人数、预约和互动时机。",
            "2. 再检查世界边界：季节、初态、天气及约束是否符合预期实验。",
            "3. 检查工具工作流：Agent 在当时是否已拥有采取该动作所需的信息。",
            "4. 检查验收：错误策略是否也可能轻易通过。",
            "5. 把具体修改意见写进每个 Markdown 的“人工审核结论”。",
            "",
        ]
    )
    return "\n".join(lines)


def _preserved_review_block(markdown_path: Path) -> str:
    """Keep reviewer checkboxes and notes across regenerated exports."""

    if markdown_path.exists():
        existing = markdown_path.read_text(encoding="utf-8")
        start = existing.find(REVIEW_START)
        end = existing.find(REVIEW_END)
        if 0 <= start < end:
            return existing[start : end + len(REVIEW_END)]
    return f"""{REVIEW_START}
## 人工审核结论

- [ ] 房间用途、容量与预约关系合理
- [ ] 人数变化和活动时间线合理
- [ ] 季节、室外边界和室内初态合理
- [ ] 用户互动发生时机与重规划范围合理
- [ ] 设备、打印和会议工具需求合理
- [ ] 功率、能耗和环境约束具有实际决策作用
- [ ] 工作流不存在未来信息泄漏或不必要重复动作
- [ ] 成功条件足以区分正确策略和错误策略
- [ ] 场景可接受；或已在下方记录修改意见

审核意见：

> 在此填写需要修改的时间、人数、设备、约束或决策逻辑。
{REVIEW_END}"""


def _step_category(step_id: str, tool_name: str | None) -> str:
    if step_id == "briefing":
        return "任务输入"
    if step_id.startswith("advance_"):
        return "时间推进"
    if step_id.startswith("reconcile_") or "agenda" in step_id:
        return "事件对账"
    if step_id.startswith(("observe_", "monitor_")):
        return "状态观测"
    if step_id.startswith(("wait_", "verify_")):
        return "响应验证"
    if "print" in step_id:
        return "打印"
    if "shutdown" in step_id:
        return "清场"
    if "prepare" in step_id or "boost" in step_id or "support" in step_id:
        return "设备控制"
    if tool_name and "send_message" in tool_name:
        return "报告"
    return "其他"


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _compact(value: Any, limit: int) -> str:
    text = json.dumps(_jsonable(value), ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _display(value: float | None, unit: str) -> str:
    return "未设置" if value is None else f"{value:.2f} {unit}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"export directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args()
    index_path = export_l3_workflows(args.output_dir)
    print(index_path)


if __name__ == "__main__":
    main()
