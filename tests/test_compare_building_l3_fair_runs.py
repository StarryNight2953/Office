"""The fair comparison rejects incomplete or mismatched batches."""

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts" / "compare_building_l3_fair_runs.py"
    spec = importlib.util.spec_from_file_location("compare_building_l3_fair_runs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_batch(
    root, mode, *, source="same-code", successes=(), protocol="building_l3_fair_ab_v1"
):
    root.mkdir()
    ids = [f"scene_{index:02}" for index in range(1, 21)]
    manifest = {
        "protocol": protocol,
        "source_sha256": source,
        "scenario_ids": ids,
        "mode": mode,
        "model": "served-model",
    }
    rows = [
        {
            "scenario_id": scene_id,
            "validation_success": index in successes,
            "total_tokens": 100,
            "wall_time_seconds": 2.0,
            "tool_calls": 3,
        }
        for index, scene_id in enumerate(ids, start=1)
    ]
    (root / "experiment_manifest.json").write_text(json.dumps(manifest))
    (root / "summary.json").write_text(json.dumps(rows))


def test_compare_reports_paired_quality_and_total_resource_use(tmp_path) -> None:
    compare = _module()
    single = tmp_path / "single"
    multi = tmp_path / "multi"
    _write_batch(single, "single", successes={1, 2})
    _write_batch(multi, "multi", successes={2, 3, 4})

    result = compare.compare(single, multi)

    assert result["single"]["passed"] == 2
    assert result["multi"]["passed"] == 3
    assert result["single"]["total_tokens"] == 2_000
    assert result["multi"]["tokens_per_success"] == pytest.approx(2_000 / 3)
    assert result["single_only"] == ["scene_01"]
    assert result["multi_only"] == ["scene_03", "scene_04"]
    markdown = compare.render_markdown(result)
    assert "逐场景透明对照" in markdown
    assert markdown.count("### 场景 ") == 20
    assert "[场景 01](#scene-01)" in markdown
    assert "scene_20" in markdown
    assert "100 / 100" in markdown


def test_v5_transparent_report_exposes_batch_actions_and_wait_continuations(tmp_path):
    compare = _module()
    single, multi = tmp_path / "single", tmp_path / "multi"
    for root, mode in ((single, "single"), (multi, "multi")):
        _write_batch(root, mode, protocol="building_l3_fair_ab_v5")
        path = root / "summary.json"
        rows = json.loads(path.read_text())
        for row in rows:
            row.update(device_plan_action_count=5, reconciled_wait_event_count=2)
        path.write_text(json.dumps(rows))
    result = compare.compare(single, multi)
    assert result["paired"][0]["single_record"]["device_plan_action_count"] == 5
    rendered = compare.render_markdown(result)
    assert "计划执行透明记录" in rendered
    assert "5 / 5" in rendered and "2 / 2" in rendered


def test_compare_includes_raw_failure_reason_and_trace_links(tmp_path) -> None:
    compare = _module()
    single = tmp_path / "single"
    multi = tmp_path / "multi"
    _write_batch(single, "single", successes=set())
    _write_batch(multi, "multi", successes={1})
    scene_root = single / "scene_01"
    scene_root.mkdir()
    multi_scene_root = multi / "scene_01"
    multi_scene_root.mkdir()
    (multi_scene_root / "scene_01.multi_agent_traces.json").write_text("{}")
    (scene_root / "scene_01.run_report.json").write_text(
        json.dumps(
            {
                "scenario_id": "scene_01",
                "metrics": {"token_usage": {"total_tokens": 100}},
                "evaluation": {
                    "validation": {
                        "success": False,
                        "message": "failed invariants: environment_within_limits",
                        "metadata": {
                            "failure_reasons": [
                                {
                                    "code": "environment_within_limits",
                                    "details": {
                                        "comfort_violation_ratio": 0.96,
                                        "max_comfort_violation_ratio": 0.9,
                                    },
                                }
                            ]
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = compare.compare(single, multi)
    markdown = compare.render_markdown(result, tmp_path / "report")

    assert result["paired"][0]["single_record"]["raw_report_available"] is True
    assert "舒适违例率 0.9600 > 上限 0.9000" in markdown
    assert "[报告](../single/scene_01/scene_01.run_report.json)" in markdown
    assert "[房间轨迹](../multi/scene_01/scene_01.multi_agent_traces.json)" in markdown


def test_compare_rejects_mismatched_code_and_incomplete_batch(tmp_path) -> None:
    compare = _module()
    single = tmp_path / "single"
    multi = tmp_path / "multi"
    _write_batch(single, "single")
    _write_batch(multi, "multi", source="different-code")

    with pytest.raises(ValueError, match="manifests differ"):
        compare.compare(single, multi)

    manifest_path = multi / "experiment_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source_sha256"] = "same-code"
    manifest_path.write_text(json.dumps(manifest))
    rows_path = multi / "summary.json"
    rows = json.loads(rows_path.read_text())
    rows_path.write_text(json.dumps(rows[:-1]))
    with pytest.raises(ValueError, match="summary is incomplete"):
        compare.compare(single, multi)


@pytest.mark.parametrize("protocol", ["building_l3_fair_ab_v2", "building_l3_fair_ab_v3", "building_l3_fair_ab_v4", "building_l3_fair_ab_v5"])
def test_compare_accepts_versioned_but_does_not_mix_protocols(tmp_path, protocol) -> None:
    compare = _module()
    single = tmp_path / "single"
    multi = tmp_path / "multi"
    _write_batch(single, "single", protocol=protocol)
    _write_batch(multi, "multi", protocol=protocol)

    assert compare.compare(single, multi)["protocol"] == protocol

    manifest_path = multi / "experiment_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["protocol"] = "building_l3_fair_ab_v1"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifests differ"):
        compare.compare(single, multi)
