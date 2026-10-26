"""Compare one complete, manifest-matched Building L3 fair A/B round."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from statistics import median
from typing import Any


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_batch(
    root: Path, expected_mode: str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = _read_json(root / "experiment_manifest.json")
    if manifest.get("protocol") not in {
        "building_l3_fair_ab_v1",
        "building_l3_fair_ab_v2",
        "building_l3_fair_ab_v3",
        "building_l3_fair_ab_v4",
        "building_l3_fair_ab_v5",
    }:
        raise ValueError(f"{root}: not a fair Building L3 batch")
    if manifest.get("mode") != expected_mode:
        raise ValueError(f"{root}: expected mode {expected_mode!r}")
    scenario_ids = manifest.get("scenario_ids")
    if not isinstance(scenario_ids, list) or len(scenario_ids) != 20:
        raise ValueError(f"{root}: the first-round comparison requires all 20 scenes")
    rows = _read_json(root / "summary.json")
    if len(rows) != len(scenario_ids):
        raise ValueError(f"{root}: summary is incomplete")
    by_id = {row["scenario_id"]: row for row in rows}
    if len(by_id) != len(rows) or set(by_id) != set(scenario_ids):
        raise ValueError(f"{root}: summary does not match manifest scenarios")
    return manifest, by_id


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    successes = sum(row["validation_success"] is True for row in rows)
    tokens = [row.get("total_tokens") for row in rows]
    seconds = [row.get("wall_time_seconds") for row in rows]
    tool_calls = [row.get("tool_calls") for row in rows]
    complete_usage = all(
        value is not None for value in (*tokens, *seconds, *tool_calls)
    )
    result = {
        "passed": successes,
        "attempts": len(rows),
        "complete_usage": complete_usage,
    }
    if complete_usage:
        token_sum = sum(int(value) for value in tokens)
        result.update(
            {
                "total_tokens": token_sum,
                "tokens_per_success": token_sum / successes if successes else None,
                "total_wall_seconds": sum(float(value) for value in seconds),
                "median_wall_seconds": median(float(value) for value in seconds),
                # Nearest-rank percentile; the 19th of 20 observed latencies.
                "p95_wall_seconds": sorted(float(value) for value in seconds)[
                    (95 * len(seconds) + 99) // 100 - 1
                ],
                "total_tool_calls": sum(int(value) for value in tool_calls),
            }
        )
    return result


def _scene_record(root: Path, row: dict[str, Any]) -> dict[str, Any]:
    """Keep the summary row and traceable raw evidence together."""
    scenario_id = row["scenario_id"]
    scene_root = root / scenario_id
    report_path = scene_root / f"{scenario_id}.run_report.json"
    record = {
        "status": row.get("status"),
        "stop_reason": row.get("stop_reason"),
        "validation_success": row["validation_success"] is True,
        "total_tokens": row.get("total_tokens"),
        "wall_time_seconds": row.get("wall_time_seconds"),
        "tool_calls": row.get("tool_calls"),
        "llm_calls": row.get("llm_calls"),
        "device_plan_action_count": row.get("device_plan_action_count"),
        "reconciled_wait_event_count": row.get("reconciled_wait_event_count"),
        "comfort_violation_ratio": row.get("comfort_violation_ratio"),
        "peak_power_w": row.get("peak_power_w"),
        "unoccupied_energy_kwh": row.get("unoccupied_energy_kwh"),
        "reused": row.get("reused"),
        "report_path": str(report_path),
        "workflow_path": str(scene_root / f"{scenario_id}.agent_workflow.json"),
        "runtime_log_path": str(scene_root / f"{scenario_id}.runtime_log.jsonl"),
        "room_traces_path": str(scene_root / f"{scenario_id}.multi_agent_traces.json"),
        "device_plan_path": str(scene_root / f"{scenario_id}.device_plan_actions.json"),
        "raw_report_available": report_path.is_file(),
        "validation_message": None,
        "failure_reasons": [],
        "room_run_count": None,
    }
    if report_path.is_file():
        report = _read_json(report_path)
        if report.get("scenario_id") != scenario_id:
            raise ValueError(f"{report_path}: scenario ID differs from summary")
        validation = report.get("evaluation", {}).get("validation", {})
        if (validation.get("success") is True) != record["validation_success"]:
            raise ValueError(f"{report_path}: validation differs from summary")
        raw_metrics = report.get("metrics", {})
        for field in ("device_plan_action_count", "reconciled_wait_event_count"):
            if raw_metrics.get(field) is not None:
                record[field] = raw_metrics[field]
        if (
            row.get("total_tokens") is not None
            and raw_metrics.get("token_usage", {}).get("total_tokens")
            != row["total_tokens"]
        ):
            raise ValueError(f"{report_path}: tokens differ from summary")
        record["validation_message"] = validation.get("message")
        metadata = validation.get("metadata") or {}
        record["failure_reasons"] = metadata.get("failure_reasons") or []
        record["room_run_count"] = (report.get("controller_telemetry") or {}).get(
            "room_run_count"
        )
    return record


def compare(single_root: Path, multi_root: Path) -> dict[str, Any]:
    single_manifest, single = _load_batch(single_root, "single")
    multi_manifest, multi = _load_batch(multi_root, "multi")
    if {k: v for k, v in single_manifest.items() if k != "mode"} != {
        k: v for k, v in multi_manifest.items() if k != "mode"
    }:
        raise ValueError(
            "single/multi manifests differ in code, scenarios or run settings"
        )
    scenario_ids = single_manifest["scenario_ids"]
    paired = [
        {
            "scenario_id": scenario_id,
            "single_pass": single[scenario_id]["validation_success"] is True,
            "multi_pass": multi[scenario_id]["validation_success"] is True,
            "single_record": _scene_record(single_root, single[scenario_id]),
            "multi_record": _scene_record(multi_root, multi[scenario_id]),
        }
        for scenario_id in scenario_ids
    ]
    return {
        "protocol": single_manifest["protocol"],
        "source_sha256": single_manifest["source_sha256"],
        "primary_max_tool_calls": single_manifest.get("max_tool_calls"),
        "single_root": str(single_root),
        "multi_root": str(multi_root),
        "single": _metrics([single[scenario_id] for scenario_id in scenario_ids]),
        "multi": _metrics([multi[scenario_id] for scenario_id in scenario_ids]),
        "single_only": [
            row["scenario_id"]
            for row in paired
            if row["single_pass"] and not row["multi_pass"]
        ],
        "multi_only": [
            row["scenario_id"]
            for row in paired
            if row["multi_pass"] and not row["single_pass"]
        ],
        "paired": paired,
    }


def _format_number(value: float | None) -> str:
    return "unknown" if value is None else f"{value:,.1f}"


def _format_decimal(value: float | None, places: int) -> str:
    return "未知" if value is None else f"{value:,.{places}f}"


def _format_int(value: int | None) -> str:
    return "未知" if value is None else f"{value:,}"


def _format_ratio(numerator: float | None, denominator: float | None) -> str:
    if numerator is None or not denominator:
        return "未知"
    return f"{numerator / denominator:.2f}×"


def _artifact_link(label: str, path: str, output_root: Path | None) -> str:
    target = Path(path)
    if not target.is_file():
        return f"{label}（缺失）"
    if output_root is not None:
        target = Path(os.path.relpath(target.resolve(), output_root.resolve()))
    return f"[{label}]({target.as_posix()})"


def _failure_text(record: dict[str, Any]) -> str:
    if record["validation_success"]:
        return "通过"
    reasons = record["failure_reasons"]
    if not reasons:
        return record["validation_message"] or "未通过；原始报告不可用"
    parts = []
    for reason in reasons:
        code = reason.get("code", "unknown")
        details = reason.get("details") or {}
        if code == "environment_within_limits":
            ratio = details.get("comfort_violation_ratio")
            limit = details.get("max_comfort_violation_ratio")
            if ratio is not None and limit is not None:
                parts.append(f"{code}：舒适违例率 {ratio:.4f} > 上限 {limit:.4f}")
                continue
        if code == "all_devices_off" and details.get("device_ids"):
            parts.append(f"{code}：仍开启 {', '.join(details['device_ids'])}")
            continue
        parts.append(f"{code}：{reason.get('message', '见原始报告')}")
    return "；".join(parts)


def render_markdown(result: dict[str, Any], output_root: Path | None = None) -> str:
    single = result["single"]
    multi = result["multi"]
    paired = result["paired"]
    multi_lower_tokens = sum(
        pair["multi_record"]["total_tokens"] < pair["single_record"]["total_tokens"]
        for pair in paired
        if pair["multi_record"]["total_tokens"] is not None
        and pair["single_record"]["total_tokens"] is not None
    )
    multi_faster = sum(
        pair["multi_record"]["wall_time_seconds"]
        < pair["single_record"]["wall_time_seconds"]
        for pair in paired
        if pair["multi_record"]["wall_time_seconds"] is not None
        and pair["single_record"]["wall_time_seconds"] is not None
    )
    lines = [
        "# Building L3 公平 A/B：逐场景透明对照（单轮）",
        "",
        f"协议：`{result['protocol']}`；代码 SHA-256：`{result['source_sha256']}`。",
        "同一场景配对比较，共 20 对；以下所有消耗均包含失败尝试，未用旧版实验数据混算。",
        "“通过”指场景验证器通过，不等于 Agent 正常结束；Token 为总输入+输出，耗时为单场景实测 wall time；工具调用和 LLM 调用包含多 Agent 的房间调用。",
        "多/单比值小于 1 表示多 Agent 使用更少资源或更快；耗时受模型服务负载影响，此单轮不能证明架构因果优劣。",
        "原始输入："
        + " · ".join(
            (
                _artifact_link("单 Agent manifest", str(Path(result["single_root"]) / "experiment_manifest.json"), output_root),
                _artifact_link("单 Agent summary", str(Path(result["single_root"]) / "summary.json"), output_root),
                _artifact_link("多 Agent manifest", str(Path(result["multi_root"]) / "experiment_manifest.json"), output_root),
                _artifact_link("多 Agent summary", str(Path(result["multi_root"]) / "summary.json"), output_root),
            )
        )
        + "。",
        "",
        "## 总览",
        "",
        "| 指标 | 单 Agent | 多 Agent |",
        "|---|---:|---:|",
        f"| 验证通过 | {single['passed']}/20 | {multi['passed']}/20 |",
    ]
    for label, key, formatter in (
        ("总 Token（含失败）", "total_tokens", _format_int),
        ("每次通过摊销 Token（总 Token / 通过数）", "tokens_per_success", _format_number),
        ("场景耗时合计（秒）", "total_wall_seconds", _format_number),
        ("场景耗时中位数（秒）", "median_wall_seconds", _format_number),
        ("场景耗时 P95（秒）", "p95_wall_seconds", _format_number),
        ("工具调用合计", "total_tool_calls", _format_int),
    ):
        lines.append(
            f"| {label} | {formatter(single.get(key))} | {formatter(multi.get(key))} |"
        )
    lines.extend(
        [
            "",
            f"仅单 Agent 通过：{len(result['single_only'])}；仅多 Agent 通过：{len(result['multi_only'])}。",
            f"逐场景看，多 Agent 在 {multi_lower_tokens}/20 场使用更少 Token，在 {multi_faster}/20 场耗时更短；平均数会受到极端场景影响。",
            "",
            "## 20 个场景速览",
            "",
            "| 场景 | 验证 单/多 | Token 单/多 | 多/单 | 耗时秒 单/多 | 多/单 | 工具调用 单/多 |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for index, pair in enumerate(paired, start=1):
        s = pair["single_record"]
        m = pair["multi_record"]
        pass_text = "/".join("通过" if record["validation_success"] else "失败" for record in (s, m))
        lines.append(
            f"| [场景 {index:02}](#scene-{index:02}) | {pass_text} | "
            f"{_format_int(s['total_tokens'])} / {_format_int(m['total_tokens'])} | "
            f"{_format_ratio(m['total_tokens'], s['total_tokens'])} | "
            f"{_format_number(s['wall_time_seconds'])} / {_format_number(m['wall_time_seconds'])} | "
            f"{_format_ratio(m['wall_time_seconds'], s['wall_time_seconds'])} | "
            f"{_format_int(s['tool_calls'])} / {_format_int(m['tool_calls'])} |"
        )
    lines.extend(["", "## 逐场景原始记录", "", "每节均可直达两侧原始报告、Agent 工作流和运行日志；多 Agent 另附房间代理轨迹。物理指标是最终场景审计值，不应脱离该场景的阈值单独排序。", ""])
    for index, pair in enumerate(paired, start=1):
        scenario_id = pair["scenario_id"]
        s = pair["single_record"]
        m = pair["multi_record"]
        label = scenario_id.removeprefix(f"scenario_building_kechuang_l3_{index:02}_")
        lines.extend(
            [
                f'<a id="scene-{index:02}"></a>',
                f"### 场景 {index:02} · `{label}`",
                "",
                "| 模式 | 验证 | 运行状态/停止原因 | Token | 耗时 s | 工具 | LLM | 房间任务 | 舒适违例率 | 峰值功率 W | 空置能耗 kWh |",
                "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for mode_label, record in (("单", s), ("多", m)):
            lines.append(
                f"| {mode_label} | {'通过' if record['validation_success'] else '失败'} | "
                f"{record['status'] or '未知'} / {record['stop_reason'] or '未知'} | "
                f"{_format_int(record['total_tokens'])} | {_format_number(record['wall_time_seconds'])} | "
                f"{_format_int(record['tool_calls'])} | {_format_int(record['llm_calls'])} | "
                f"{_format_int(record['room_run_count']) if mode_label == '多' else '—'} | "
                f"{_format_decimal(record['comfort_violation_ratio'], 4)} | "
                f"{_format_decimal(record['peak_power_w'], 1)} | "
                f"{_format_decimal(record['unoccupied_energy_kwh'], 2)} |"
            )
        lines.extend(
            [
                "",
                f"验证详情：单 Agent — {_failure_text(s)}；多 Agent — {_failure_text(m)}。",
                f"资源对比：多/单 Token {_format_ratio(m['total_tokens'], s['total_tokens'])}，耗时 {_format_ratio(m['wall_time_seconds'], s['wall_time_seconds'])}。这描述本次轨迹，不单独说明原因。",
            ]
        )
        if s.get("device_plan_action_count") is not None or m.get("device_plan_action_count") is not None:
            lines.append(
                f"计划执行透明记录：批内设备动作 单/多 {_format_int(s.get('device_plan_action_count'))} / {_format_int(m.get('device_plan_action_count'))}；"
                f"已验证事件继续等待 单/多 {_format_int(s.get('reconciled_wait_event_count'))} / {_format_int(m.get('reconciled_wait_event_count'))}。"
                "这些是工具内部动作/事件计数，不是额外模型调用，也不能与外层工具数简单相加。"
            )
        for mode_label, record in (("单", s), ("多", m)):
            evidence = [
                _artifact_link("报告", record["report_path"], output_root),
                _artifact_link("工作流", record["workflow_path"], output_root),
                _artifact_link("运行日志", record["runtime_log_path"], output_root),
            ]
            if mode_label == "多":
                evidence.append(
                    _artifact_link("房间轨迹", record["room_traces_path"], output_root)
                )
            if Path(record["device_plan_path"]).is_file():
                evidence.append(_artifact_link("批内设备动作", record["device_plan_path"], output_root))
            lines.append(f"{mode_label} Agent 证据：{' · '.join(evidence)}。")
        lines.extend(["", "---", ""])
    lines.extend(
        [
            "## 阅读边界",
            "",
            "本报告逐场景展示 1 次配对实跑的结果。未通过的场景也计入总消耗；不能只筛选通过场景宣称整体优势。",
            "`工具调用`为运行报告口径：多 Agent 包括主管与房间代理的调用；单 Agent 没有房间任务。每场景可从报告继续追溯到工作流、运行日志与多 Agent 房间轨迹。",
            (
                f"主 Agent 工具调用上限为 {result['primary_max_tool_calls']}；表内多 Agent 工具数还包括房间代理，因此可超过该上限。"
                if result.get("primary_max_tool_calls") is not None
                else ""
            ),
            "如果原始报告缺失，其验证详情仅能依赖汇总表；此时不应把“未知”当成 0。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--single-root", required=True, type=Path)
    parser.add_argument("--multi-root", required=True, type=Path)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    result = compare(args.single_root, args.multi_root)
    report = render_markdown(result, args.output_root)
    if args.output_root is not None:
        args.output_root.mkdir(parents=True, exist_ok=True)
        (args.output_root / "comparison.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (args.output_root / "comparison.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
