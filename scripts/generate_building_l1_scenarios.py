"""Generate native Building L1 Scenario modules from reviewed manifest entries."""

from __future__ import annotations

import argparse
import importlib
import json
import py_compile
import re
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smartoffice.controllers.engine import Engine
from smartoffice.scenarios.building_kechuang.l1.manifest import (
    MANIFEST_PATH,
    ManifestError,
    accepted_contracts,
    load_manifest,
    validate_source_links,
)
from smartoffice.scenarios.registry import get_scenario_class

DEFAULT_OUTPUT_DIR = (
    ROOT / "smartoffice" / "scenarios" / "building_kechuang" / "l1" / "generated"
)
L3_SELECTOR = re.compile(r"^L3-(\d{1,2})$", re.IGNORECASE)
GENERATED_MARKER = '"""Generated from l1/manifest.json; do not edit this module directly."""'


def render_contract(contract: dict[str, Any]) -> str:
    scenario_id = contract["scenario_id"]
    class_name = contract["class_name"]
    return f'''"""Generated from l1/manifest.json; do not edit this module directly."""

from smartoffice.scenarios.building_kechuang.l1.base import (
    ManifestDrivenBuildingL1Scenario,
    contract_timestamp,
)
from smartoffice.scenarios.building_kechuang.l1.manifest import load_contract
from smartoffice.scenarios.registry import register_scenario

CONTRACT = load_contract("{scenario_id}")


@register_scenario("{scenario_id}")
class {class_name}(ManifestDrivenBuildingL1Scenario):
    contract_id = "{scenario_id}"
    start_time: float | None = contract_timestamp(CONTRACT)
    duration: float | None = float(CONTRACT["duration_seconds"])
    time_increment_in_seconds: int = 60
    scenario_input: str = CONTRACT["task"]["prompt"]
'''


def resolve_selectors(
    manifest: dict[str, Any], selectors: Iterable[str] | None
) -> set[str] | None:
    if not selectors:
        return None
    tokens = {
        token.strip()
        for value in selectors
        for token in value.split(",")
        if token.strip()
    }
    by_id = {item["scenario_id"]: item for item in manifest["contracts"]}
    selected: set[str] = set()
    unknown: list[str] = []
    for token in sorted(tokens):
        if token in by_id:
            selected.add(token)
            continue
        match = L3_SELECTOR.fullmatch(token)
        if match is None:
            unknown.append(token)
            continue
        marker = f"_l3_{int(match.group(1)):02d}_"
        matches = [
            item["scenario_id"]
            for item in manifest["contracts"]
            if any(
                marker in source.get("l3_scenario_id", "")
                for source in item.get("coverage", {}).get("source_variants", [])
            )
            or any(
                marker in obligation.get("source_l3_id", "")
                for obligation in item.get("coverage", {}).get("obligation_refs", [])
            )
        ]
        if not matches:
            unknown.append(token)
        selected.update(matches)
    if unknown:
        raise ManifestError(f"unknown selectors: {unknown}")
    return selected


def plan_generation(
    contracts: list[dict[str, Any]], output_dir: Path
) -> dict[Path, str]:
    return {
        output_dir / f"{contract['scenario_id']}.py": render_contract(contract)
        for contract in contracts
    }


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def apply_generation(
    expected: dict[Path, str], *, check: bool, force: bool
) -> dict[str, list[str]]:
    missing: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    conflicts: list[str] = []
    for path, content in expected.items():
        if not path.exists():
            missing.append(_display_path(path))
            continue
        if path.read_text(encoding="utf-8") == content:
            unchanged.append(_display_path(path))
        elif force:
            changed.append(_display_path(path))
        else:
            conflicts.append(_display_path(path))

    if check:
        if missing or conflicts or changed:
            raise RuntimeError(
                "generated files are not current: "
                f"missing={missing}, conflicts={conflicts}, changed={changed}"
            )
        return {
            "created": [],
            "updated": [],
            "unchanged": unchanged,
            "conflicts": [],
        }
    if conflicts:
        raise RuntimeError(
            "refusing to overwrite modified generated files without --force: "
            + ", ".join(conflicts)
        )

    for path, content in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
    return {
        "created": missing,
        "updated": changed,
        "unchanged": unchanged,
        "conflicts": [],
    }


def find_stale_modules(output_dir: Path, expected: dict[Path, str]) -> list[str]:
    if not output_dir.exists():
        return []
    expected_paths = {path.resolve() for path in expected}
    return sorted(
        _display_path(path)
        for path in output_dir.glob("scenario_building_kechuang_l1_*.py")
        if path.resolve() not in expected_paths
    )


def prune_stale_modules(output_dir: Path, expected: dict[Path, str]) -> list[str]:
    """Delete only obsolete files that still carry the generator marker."""

    if not output_dir.exists():
        return []
    output_root = output_dir.resolve()
    expected_paths = {path.resolve() for path in expected}
    removed: list[str] = []
    for path in output_dir.glob("scenario_building_kechuang_l1_*.py"):
        resolved = path.resolve()
        resolved.relative_to(output_root)
        if resolved in expected_paths:
            continue
        content = path.read_text(encoding="utf-8")
        if not content.startswith(GENERATED_MARKER):
            raise RuntimeError(
                f"refusing to prune non-generated module: {_display_path(path)}"
            )
        path.unlink()
        removed.append(_display_path(path))
    return sorted(removed)


def validate_generated(
    contracts: list[dict[str, Any]], expected: dict[Path, str], output_dir: Path
) -> list[dict[str, Any]]:
    for path in expected:
        py_compile.compile(str(path), doraise=True)
    results: list[dict[str, Any]] = []
    if output_dir.resolve() != DEFAULT_OUTPUT_DIR.resolve():
        return [
            {
                "scenario_id": contract["scenario_id"],
                "compiled": True,
                "registered": None,
                "oracle_replay_success": None,
            }
            for contract in contracts
        ]

    importlib.invalidate_caches()
    for contract in contracts:
        scenario_id = contract["scenario_id"]
        importlib.import_module(
            "smartoffice.scenarios.building_kechuang.l1.generated." + scenario_id
        )
        scenario_class = get_scenario_class(scenario_id)
        if scenario_class.__name__ != contract["class_name"]:
            raise RuntimeError(f"registry class mismatch for {scenario_id}")
        build_engine = Engine(None, scenario_class())
        oracle = build_engine.build_oracle_workflow(run_oracle=False)
        replay_engine = Engine(None, scenario_class())
        replayed = replay_engine.replay_workflow(oracle)
        report = replay_engine.evaluation_report(replayed)
        success = bool(report["validation"]["success"])
        if not success:
            raise RuntimeError(
                f"oracle replay failed for {scenario_id}: "
                f"{report['validation']['rationale']}"
            )
        results.append(
            {
                "scenario_id": scenario_id,
                "compiled": True,
                "registered": True,
                "oracle_steps": len(oracle.dag),
                "oracle_replay_success": True,
            }
        )
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate only reviewed accepted_l1 Building scenarios; L3 selectors such "
            "as L3-06 and exact scenario IDs are supported."
        )
    )
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--scenarios",
        action="append",
        help="comma-separated exact L1 IDs or source selectors such as L3-06",
    )
    parser.add_argument("--check", action="store_true", help="check without writing")
    parser.add_argument(
        "--force", action="store_true", help="overwrite differing generated modules"
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="remove obsolete modules only when they still carry the generator marker",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="compile and, for the default package, replay each Oracle and evaluate it",
    )
    parser.add_argument("--report", type=Path, help="optional JSON generation report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check and (args.force or args.prune):
        raise ManifestError("--check cannot be combined with --force or --prune")
    manifest = load_manifest(args.manifest)
    validate_source_links(manifest, ROOT)
    selected_ids = resolve_selectors(manifest, args.scenarios)
    contracts = accepted_contracts(manifest, selected_ids)
    if not contracts:
        raise ManifestError("selection contains no accepted_l1 contracts")
    output_dir = args.output_dir.resolve()
    expected = plan_generation(contracts, output_dir)
    stale = find_stale_modules(output_dir, expected) if selected_ids is None else []
    pruned: list[str] = []
    if stale and args.prune:
        pruned = prune_stale_modules(output_dir, expected)
        stale = find_stale_modules(output_dir, expected)
    if stale:
        raise RuntimeError(
            "stale generated modules require explicit review; no files were deleted: "
            + ", ".join(stale)
        )
    changes = apply_generation(expected, check=args.check, force=args.force)
    validation = (
        validate_generated(contracts, expected, output_dir) if args.validate else []
    )
    report = {
        "manifest": str(args.manifest.resolve()),
        "output_dir": str(output_dir),
        "mode": "check" if args.check else "generate",
        "selected_scenario_ids": [item["scenario_id"] for item in contracts],
        "generated_count": len(contracts),
        "changes": changes,
        "pruned_modules": pruned,
        "stale_modules": stale,
        "validation": validation,
        "scope": manifest["scope"],
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report is not None and not args.check:
        report_path = args.report.resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ManifestError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
