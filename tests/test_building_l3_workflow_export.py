"""Regression coverage for the human-review workflow export bundle."""

import json

from smartoffice.scenarios.building_kechuang.export_l3_workflows import (
    export_l3_workflows,
)


def test_export_l3_workflows_creates_complete_review_bundle(tmp_path) -> None:
    index_path = export_l3_workflows(tmp_path)
    summaries = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))

    assert index_path == tmp_path / "README.md"
    assert len(summaries) == 20
    assert all(item["success"] is True for item in summaries)
    assert len(list(tmp_path.glob("l3_*.md"))) == 20
    assert len(list(tmp_path.glob("l3_*.json"))) == 20
    assert "人工审核结论" in (tmp_path / summaries[0]["markdown"]).read_text(
        encoding="utf-8"
    )

    first_markdown = tmp_path / summaries[0]["markdown"]
    reviewed = first_markdown.read_text(encoding="utf-8").replace(
        "> 在此填写需要修改的时间、人数、设备、约束或决策逻辑。",
        "> 人工意见应在重新导出后保留。",
    )
    first_markdown.write_text(reviewed, encoding="utf-8")
    export_l3_workflows(tmp_path)
    assert "人工意见应在重新导出后保留" in first_markdown.read_text(
        encoding="utf-8"
    )
